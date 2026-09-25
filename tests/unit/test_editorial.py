import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from editorial_bot.agent.tools.base import ToolContext
from editorial_bot.agent.tools.editorial import Comment, EditorialTools, Empty, Forget, Review
from editorial_bot.services.gitlab import RemoteError
from editorial_bot.services.sheets_roster import Member, Roster
from editorial_bot.storage.db import Store


@pytest.fixture
async def tools(tmp_path):
    store = await Store.open(str(tmp_path / "db.sqlite3"))
    roster = Roster(
        [Member(1, "author_one"), Member(2, "chief_one", position="總召"), Member(3, "deputy_one", position="副召")]
    )
    issue = {
        "iid": 1,
        "title": "測試 <標題>",
        "state": "opened",
        "labels": ["社群文案"],
        "web_url": "https://gitlab.com/p/-/issues/1",
        "assignees": [{"id": 1}],
        "description": "https://docs.google.com/document/d/doc/edit\nhttps://drive.google.com/drive/folders/folder",
    }
    gl = SimpleNamespace(
        resolve=AsyncMock(
            side_effect=lambda t: {**issue, "iid": int(t), "web_url": f"https://gitlab.com/p/-/issues/{t}"}
        ),
        validate_labels=AsyncMock(),
        update=AsyncMock(),
        notes=AsyncMock(return_value=[]),
        comment=AsyncMock(),
    )
    settings = SimpleNamespace(review_status="Status::Review", default_document_label="社群文案", tz="Asia/Taipei")
    instance = EditorialTools(
        gl, None, SimpleNamespace(get=AsyncMock(return_value=roster)), None, None, store, settings
    )
    instance.reviews.documents = SimpleNamespace(
        export_pdf=AsyncMock(return_value=b"%PDF-1.7\nreview"), sign=AsyncMock()
    )
    yield instance
    await store.close()


def context():
    return ToolContext(-1, 3, 7, "author_one", "review 1 2", event_id="event")


async def test_review_all_targets_tags_author_and_both_chiefs_once(tools):
    ctx = context()
    args = Review(targets=["1", "2", "1"])
    await tools.review(args, ctx)
    await tools.review(args, ctx)
    assert tools.gl.update.await_count == 2
    assert len(ctx.notices) == 2
    for notice in ctx.notices:
        assert "@author_one" in notice and "@chief_one" in notice and "@deputy_one" in notice
        assert "&lt;標題&gt;" in notice and "/folders/folder" in notice


async def test_ambiguous_review_target_stops_entire_batch(tools):
    tools.gl.resolve.side_effect = [{"iid": 1}, ValueError("multiple matches")]
    with pytest.raises(ValueError):
        await tools.review(Review(targets=["1", "not unique"]), context())
    tools.gl.update.assert_not_awaited()


async def test_tag_every_member_including_requester(tools):
    ctx = context()
    result = await tools.tag(Empty(), ctx)
    assert result["mentioned"] == 3
    assert "@author_one" in ctx.notices[0]


async def test_unknown_comment_timeout_does_not_duplicate(tools):
    tools.gl.comment.side_effect = RemoteError("GitLab", None, uncertain=True)
    args, ctx = Comment(iid=1, body="測試"), context()
    with pytest.raises(RemoteError):
        await tools.comment(args, ctx)
    with pytest.raises(ValueError, match="結果仍不明"):
        await tools.comment(args, ctx)
    tools.gl.comment.assert_awaited_once()
    row = await tools.store.one("SELECT steps FROM operations")
    assert json.loads(row["steps"])["attempted"]


async def test_forget_cannot_cross_groups(tools):
    memory_id = await tools.store.add_memory(-2, 7, "writer", "其他群記憶")
    with pytest.raises(ValueError):
        await tools.forget(Forget(memory_id=memory_id), context())
    assert await tools.store.one("SELECT id FROM group_memories WHERE id=?", (memory_id,))


async def prepare_create(tools, *, username="author_one", user_id=7):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from editorial_bot.agent.tools.editorial import Create

    tools.settings.tz = "Asia/Taipei"
    tools.settings.default_task_label = "編輯組專案"
    tools.settings.initial_status = "Status::Inbox"
    tools.members_for_ids = AsyncMock()
    tools.workflow = SimpleNamespace(create=AsyncMock(return_value={"iid": 9}))
    tools.gl.user_username = AsyncMock(return_value="gitlab_author")
    args = Create(title="test", due_date=f"{datetime.now(ZoneInfo('Asia/Taipei')).year}-09-25", assignee_ids=[2])
    return args, ToolContext(-1, 3, user_id, username, "小石開卡 0925 test", event_id="create")


async def test_creator_mentions_gitlab_account_from_roster_not_assignee(tools):
    args, ctx = await prepare_create(tools, username="AUTHOR_ONE")
    await tools.create(args, ctx)
    tools.gl.user_username.assert_awaited_once_with(1)
    payload = tools.workflow.create.call_args.args[0]
    assert payload["requester"] == "@gitlab_author"
    assert payload["assignee_ids"] == [2]


async def test_creator_prefers_numeric_telegram_identity(tools):
    args, ctx = await prepare_create(tools, username=None)
    tools.roster.get.return_value.members[0].telegram_id = ctx.user_id
    await tools.create(args, ctx)
    tools.gl.user_username.assert_awaited_once_with(1)
    assert tools.workflow.create.call_args.args[0]["requester"] == "@gitlab_author"


@pytest.mark.parametrize("missing", ["roster", "gitlab", "telegram"])
async def test_creator_telegram_fallback_does_not_mention_unrelated_gitlab_user(tools, missing):
    username = "outside_user" if missing == "roster" else None if missing == "telegram" else "author_one"
    args, ctx = await prepare_create(tools, username=username)
    tools.gl.user_username.return_value = None
    await tools.create(args, ctx)
    expected = f"Telegram：`@{username}`" if username else "Telegram ID：7"
    assert tools.workflow.create.call_args.args[0]["requester"] == expected
    if missing != "gitlab":
        tools.gl.user_username.assert_not_awaited()


async def test_creator_lookup_outage_does_not_silently_fall_back_or_create(tools):
    args, ctx = await prepare_create(tools)
    tools.gl.user_username.side_effect = RemoteError("GitLab", 503)
    with pytest.raises(RemoteError):
        await tools.create(args, ctx)
    tools.workflow.create.assert_not_awaited()


@pytest.mark.parametrize("document,folder", [(True, True), (True, False), (False, True), (False, False)])
async def test_review_labels_each_available_link_on_its_own_line(tools, document, folder):
    issue = await tools.gl.resolve("1")
    doc_url = "https://docs.google.com/document/d/doc/edit"
    folder_url = "https://drive.google.com/drive/folders/folder"
    issue["description"] = "\n".join(([doc_url] if document else []) + ([folder_url] if folder else []))
    issue["labels"] = ["社群文案"] if document else ["編輯組專案"]
    tools.gl.resolve.side_effect = None
    tools.gl.resolve.return_value = issue
    ctx = context()
    await tools.review(Review(targets=["1"]), ctx)
    expected = []
    if document:
        expected.append(f"文案：{doc_url}")
    if folder:
        expected.append(f"資料夾：{folder_url}")
    expected.append(f"卡片：{issue['web_url']}")
    assert ctx.notices[0].splitlines()[1:] == expected
    tools.gl.update.assert_awaited_once_with(1, add_labels=["Status::Review"])
    saved = await tools.store.one("SELECT result FROM operations WHERE kind='review'")
    assert json.loads(saved["result"])["notification"] == ctx.notices[0]


async def test_review_pdf_export_failure_stops_status_updates_for_whole_batch(tools):
    tools.reviews.documents.export_pdf.side_effect = [b"%PDF-1.7\nfirst", ValueError("PDF 匯出失敗")]
    with pytest.raises(ValueError, match="PDF"):
        await tools.review(Review(targets=["1", "2"]), context())
    tools.gl.update.assert_not_awaited()


async def test_review_receipt_records_pdf_packet_and_retry_reuses_snapshot(tools):
    args, ctx = Review(targets=["1"]), context()
    await tools.review(args, ctx)
    await tools.review(args, ctx)
    receipt = json.loads((await tools.store.one("SELECT result FROM operations WHERE kind='review'"))["result"])
    packet = await tools.store.one("SELECT * FROM review_packets WHERE id=?", (receipt["review_id"],))
    assert packet["document_id"] == "doc" and packet["pdf"].startswith(b"%PDF-")
    assert packet["notice"] == ctx.notices[0]
    tools.reviews.documents.export_pdf.assert_awaited_once()
    tools.gl.update.assert_awaited_once()


@pytest.mark.parametrize("document", [None, False, True])
async def test_create_mode_defaults_to_card_only_and_selects_matching_labels(tools, document):
    args, ctx = await prepare_create(tools)
    if document is not None:
        args.document = document
    await tools.create(args, ctx)
    payload = tools.workflow.create.call_args.args[0]
    assert payload["document"] is (document is True)
    assert payload["labels"] == ["社群文案" if document else "編輯組專案", "Status::Inbox"]


async def test_card_only_review_does_not_infer_document_from_title_or_label(tools):
    issue = await tools.gl.resolve("1")
    issue.update(title="文案規劃", description="僅開卡")
    tools.gl.resolve.side_effect = None
    tools.gl.resolve.return_value = issue
    await tools.store.execute("INSERT INTO resources VALUES (1,NULL,NULL,'card-only')")
    ctx = context()
    await tools.review(Review(targets=["1"]), ctx)
    assert ctx.notices[0].splitlines()[1:] == [f"卡片：{issue['web_url']}"]
    tools.reviews.documents.export_pdf.assert_not_awaited()
    tools.gl.update.assert_awaited_once_with(1, add_labels=["Status::Review"])


async def test_card_only_description_update_preserves_marker_without_empty_resource_links(tools):
    from editorial_bot.agent.tools.editorial import Update

    marker = "<!-- editorial-operation:abc123 -->"
    tools.gl.get = AsyncMock(return_value={"labels": [], "description": "舊內容\n" + marker})
    tools.gl.update.return_value = {"labels": [], "web_url": "https://gitlab.com/p/-/issues/1"}
    await tools.store.execute("INSERT INTO resources VALUES (1,NULL,NULL,'abc123')")
    await tools.update(Update(iid=1, description="新內容"), context())
    description = tools.gl.update.call_args.kwargs["description"]
    assert description == "新內容\n\n" + marker
    assert "editorial-resources" not in description and "None" not in description
