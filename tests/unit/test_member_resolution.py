from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from editorial_bot.agent.core import Agent
from editorial_bot.agent.tools.base import ToolContext, ToolRegistry
from editorial_bot.agent.tools.editorial import Create, EditorialTools, Person
from editorial_bot.services.sheets_roster import RosterService


def sheet(gitlab_id="7036398656", nickname="章魚", username="octopus"):
    return [["Telegram ID", "gitlab_id", "nickname", "default", "note"], [username, gitlab_id, nickname, "yes"]]


@pytest.fixture
def tools():
    google = SimpleNamespace(roster_values=AsyncMock(return_value=sheet()))
    gl = SimpleNamespace(client=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(is_error=False))))
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
        Create(title="製作 IG 精選動態封面", due_date="2026-10-11", document=False, assignee_ids=[23265503]), context()
    )
    payload = tools.workflow.create.call_args.args[0]
    assert payload["assignee_ids"] == [23265503]
    assert payload["document"] is False
    tools.gl.client.get.assert_awaited_once_with("/users/23265503")


async def test_default_assignee_is_refreshed_before_creation(tools):
    await tools.roster.get()
    tools.google.roster_values.return_value = sheet("23265503")
    await tools.create(Create(title="test", due_date="2026-10-11", document=False), context())
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
