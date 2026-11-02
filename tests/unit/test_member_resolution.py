from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from editorial_bot.agent.core import Agent
from editorial_bot.agent.tools.base import ToolContext, ToolRegistry
from editorial_bot.agent.tools.editorial import Create, EditorialTools, Person, Resume, Update
from editorial_bot.services.gitlab import RemoteError
from editorial_bot.services.sheets_roster import RosterService
from editorial_bot.storage.db import Store


def sheet(gitlab_id="7036398656", nickname="章魚", username="octopus"):
    return [["Telegram ID", "gitlab_id", "nickname", "default", "note"], [username, gitlab_id, nickname, "yes"]]


@pytest.fixture
def tools():
    google = SimpleNamespace(roster_values=AsyncMock(return_value=sheet()))
    gl = SimpleNamespace(user_username=AsyncMock(return_value="octopus"))
    settings = SimpleNamespace(
        tz="Asia/Taipei",
        default_task_label="編輯組專案",
        initial_status="Status::Inbox",
        review_status="Status::Review",
    )
    workflow = SimpleNamespace(create=AsyncMock(return_value={"iid": 9}))
    return EditorialTools(gl, google, RosterService(google), None, workflow, None, settings)


def context():
    return ToolContext(-1, None, 7, "octopus", "小石開卡 製作 IG 精選動態封面 指定章魚 不要建文件 DDL 2026-10-11")


async def test_resolve_nickname_rereads_sheet_after_id_change(tools):
    await tools.roster.get()
    tools.google.roster_values.return_value = sheet("23265503")
    result = await tools.resolve_member(Person(query="章魚"), context())
    assert result["gitlab_id"] == 23265503
    assert tools.google.roster_values.await_count == 2


async def test_resolve_duplicate_nickname_returns_candidates(tools):
    tools.google.roster_values.return_value = [*sheet("10"), ["another", "11", "章魚", "no"]]
    result = await tools.resolve_member(Person(query="章魚"), context())
    assert result["needs_clarification"] is True
    assert {m["gitlab_id"] for m in result["matches"]} == {10, 11}


async def test_create_accepts_corrected_id_before_cache_expires(tools):
    await tools.roster.get()
    tools.google.roster_values.return_value = sheet("23265503")
    await tools.create(
        Create(title="製作 IG 精選動態封面", due_date="2026-10-11", document_card=False, assignee_ids=[23265503]),
        context(),
    )
    payload = tools.workflow.create.call_args.args[0]
    assert payload["assignee_ids"] == [23265503]
    assert payload["document_card"] is False
    tools.gl.user_username.assert_awaited_once_with(23265503)


async def test_default_assignee_is_refreshed_before_creation(tools):
    await tools.roster.get()
    tools.google.roster_values.return_value = sheet("23265503")
    await tools.create(Create(title="test", due_date="2026-10-11", document_card=False), context())
    assert tools.workflow.create.call_args.args[0]["assignee_ids"] == [23265503]


async def test_self_resolution_does_not_match_someone_elses_nickname(tools):
    tools.google.roster_values.return_value = sheet("10", nickname="octopus", username="another")
    with pytest.raises(ValueError, match="查無"):
        await tools.resolve_member(Person(query="我"), context())


async def test_requester_note_refreshes_identity_and_never_uses_nickname_as_username(tools):
    agent = Agent(None, ToolRegistry([]), None, tools.roster)
    await tools.roster.get()
    tools.google.roster_values.return_value = sheet("23265503")
    assert "gitlab_id=23265503" in await agent._requester_note(context())
    tools.google.roster_values.return_value = sheet("10", nickname="octopus", username="another")
    note = await agent._requester_note(context())
    assert "gitlab_id=10" not in note
    assert "resolve_member" in note


async def test_explicit_gitlab_correction_does_not_require_editing_roster_first(tools):
    tools.gl.user_username = AsyncMock(return_value="octopus")
    ctx = context()
    ctx.text += "\nGitLab ID 更正為 23265503"
    member = await tools.resolve_member(Person(query="23265503"), ctx)
    assert member["gitlab_id"] == 23265503
    assert member["gitlab_username"] == "octopus"
    assert member["telegram_username"] is None
    await tools.create(
        Create(title="製作 IG 精選動態封面", due_date="2026-10-11", document_card=False, assignee_ids=[23265503]), ctx
    )
    assert tools.workflow.create.call_args.args[0]["assignee_ids"] == [23265503]


@pytest.mark.parametrize("text", ["指定章魚", "ID 123265503", "@account_23265503"])
async def test_unlisted_id_without_exact_user_input_is_not_guessed(tools, text):
    ctx = context()
    ctx.text = text
    with pytest.raises(ValueError, match="使用者"):
        await tools.resolve_member(Person(query="23265503"), ctx)
    tools.gl.user_username.assert_not_awaited()


@pytest.mark.parametrize("error", [None, RemoteError("GitLab", 503)])
async def test_explicit_id_must_be_verified_and_outage_is_not_roster_absence(tools, error):
    tools.gl.user_username = AsyncMock(return_value=None, side_effect=error)
    ctx = context()
    ctx.text += "\n23265503"
    with pytest.raises(RemoteError if error else ValueError) as caught:
        await tools.create(
            Create(title="test", due_date="2026-10-11", document_card=False, assignee_ids=[23265503]), ctx
        )
    assert "不在編輯組名冊" not in str(caught.value)
    tools.workflow.create.assert_not_awaited()


async def test_assignment_update_uses_fresh_roster(tools, tmp_path):
    tools.store = await Store.open(str(tmp_path / "db.sqlite3"))
    try:
        await tools.roster.get()
        tools.google.roster_values.return_value = sheet(str(23265503))
        tools.gl.get = AsyncMock(return_value={"iid": 9, "labels": []})
        tools.gl.update = AsyncMock(return_value={"iid": 9, "labels": [], "web_url": "https://gitlab.test/issues/9"})
        await tools.update(Update(iid=9, set_assignee_ids=[23265503]), context())
        assert tools.gl.update.call_args.kwargs["assignee_ids"] == [23265503]
        tools.gl.user_username.assert_awaited_once_with(23265503)
    finally:
        await tools.store.close()


async def test_resume_keeps_previously_validated_explicit_assignee(tools, tmp_path):
    tools.store = await Store.open(str(tmp_path / "db.sqlite3"))
    try:
        ctx = context()
        key = "a" * 24
        payload = {"title": "test", "due_date": "2026-10-11", "assignee_ids": [23265503], "document_card": False}
        await tools.store.operation(key, "original", ctx.chat_id, ctx.user_id, "create_card", payload)
        ctx.text = f"接續操作 {key}"
        await tools.resume(Resume(operation_id=key), ctx)
        tools.gl.user_username.assert_awaited_once_with(23265503)
        assert tools.workflow.create.call_args.args[0]["assignee_ids"] == [23265503]
    finally:
        await tools.store.close()


async def test_old_id_is_rejected_after_roster_change_unless_user_explicitly_supplied_it(tools):
    await tools.roster.get()
    tools.google.roster_values.return_value = sheet("23265503")
    with pytest.raises(ValueError, match="使用者未明確提供"):
        await tools.create(
            Create(title="test", due_date="2026-10-11", document_card=False, assignee_ids=[7036398656]), context()
        )
    tools.gl.user_username.assert_not_awaited()
    tools.workflow.create.assert_not_awaited()
