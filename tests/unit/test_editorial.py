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
    settings = SimpleNamespace(review_status="Status::Review", default_document_label="社群文案")
    instance = EditorialTools(
        gl, None, SimpleNamespace(get=AsyncMock(return_value=roster)), None, None, store, settings
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
