import json
from datetime import UTC, date, datetime
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
    assert ctx.review_completed
    for notice in ctx.notices:
        assert "@author_one" in notice and "@chief_one" in notice and "@deputy_one" in notice
        assert "&lt;標題&gt;" in notice
        assert "資料夾：https://drive.google.com/drive/folders/folder" in notice


async def test_review_proceeds_without_chiefs_in_roster(tools):
    tools.roster.get.return_value = Roster([Member(1, "author_one"), Member(3, "", "Deputy", position="副召")])
    ctx = context()
    await tools.review(Review(targets=["1"]), ctx)
    tools.gl.update.assert_awaited_once()
    assert ctx.review_completed
    assert "@author_one" in ctx.notices[0] and "未能標註：Deputy" in ctx.notices[0]


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


@pytest.mark.parametrize("username", ["AUTHOR_ONE", "outside_user", None])
async def test_create_does_not_lookup_or_add_creator(tools, username):
    args, ctx = await prepare_create(tools, username=username)
    tools.roster.get.return_value.members[0].telegram_id = ctx.user_id
    tools.gl.user_username.side_effect = RemoteError("GitLab", 503)
    await tools.create(args, ctx)
    tools.gl.user_username.assert_not_awaited()
    tools.members_for_ids.assert_awaited_once_with([2], roster=tools.roster.get.return_value, user_text=ctx.text)
    payload = tools.workflow.create.call_args.args[0]
    assert "requester" not in payload
    assert payload["assignee_ids"] == [2]


@pytest.mark.parametrize("word,day", [("今天", 6), ("明天", 7), ("後天", 8)])
async def test_create_accepts_relative_deadline_without_confirmation(tools, word, day):
    args, ctx = await prepare_create(tools)
    ctx.reference_time = datetime(2026, 10, 5, 16, 30, tzinfo=UTC)
    ctx.text = f"小石開卡 test，{word}到期"
    args.due_date = f"2026-10-{day:02d}"
    await tools.create(args, ctx)
    assert tools.workflow.create.call_args.args[0]["due_date"] == args.due_date


@pytest.mark.parametrize("correct", [True, False])
async def test_update_validates_relative_deadline_before_gitlab_write(tools, correct):
    from editorial_bot.agent.tools.editorial import Update

    ctx = context()
    ctx.reference_time = datetime(2026, 12, 31, 10, tzinfo=UTC)
    ctx.text = "小石，把 #1 到期日改成後天"
    issue = await tools.gl.resolve("1")
    tools.gl.get = AsyncMock(return_value=issue)
    tools.gl.update.return_value = issue
    args = Update(iid=1, due_date=date(2027, 1, 2 if correct else 3))
    if correct:
        await tools.update(args, ctx)
        assert tools.gl.update.call_args.kwargs["due_date"] == "2027-01-02"
    else:
        with pytest.raises(ValueError, match="到期日"):
            await tools.update(args, ctx)
        tools.gl.update.assert_not_awaited()


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
    assert ctx.review_completed is document
    tools.gl.update.assert_awaited_once_with(1, add_labels=["Status::Review"])
    saved = await tools.store.one("SELECT result FROM operations WHERE kind='review'")
    assert json.loads(saved["result"])["notification"] == ctx.notices[0]


async def test_review_folder_link_uses_persisted_resource_before_description(tools):
    await tools.store.execute("INSERT INTO resources VALUES (1,'saved-folder','doc','created')")
    ctx = context()
    await tools.review(Review(targets=["1"]), ctx)
    assert "資料夾：https://drive.google.com/drive/folders/saved-folder" in ctx.notices[0]
    assert "https://drive.google.com/drive/folders/folder" not in ctx.notices[0]
    packet = await tools.store.one("SELECT notice FROM review_packets WHERE issue_iid=1")
    assert packet["notice"] == ctx.notices[0]


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


@pytest.mark.parametrize("document_card", [None, False, True])
async def test_create_card_type_only_selects_default_label(tools, document_card):
    args, ctx = await prepare_create(tools)
    if document_card is not None:
        args.document_card = document_card
    await tools.create(args, ctx)
    payload = tools.workflow.create.call_args.args[0]
    assert payload["labels"] == ["社群文案" if document_card is not False else "編輯組專案", "Status::Inbox"]
    assert "document" not in payload


@pytest.mark.parametrize(
    "name,method", [("create_card_folder", "add_folder"), ("create_card_document", "add_document")]
)
async def test_resource_tools_with_card_number_use_card_and_reject_folder_id(tools, name, method):
    from pydantic import ValidationError

    tools.workflow = SimpleNamespace(**{method: AsyncMock(return_value={"iid": 682})})
    tool = next(t for t in tools.build() if t.name == name)
    ctx = context()
    await tool.run(tool.args_model(iid=682), ctx)
    getattr(tools.workflow, method).assert_awaited_once_with(682, ctx)
    with pytest.raises(ValidationError):
        tool.args_model(iid=0)
    with pytest.raises(ValidationError):
        tool.args_model(iid=682, folder_id="arbitrary")


@pytest.mark.parametrize(
    "kind,method,payload",
    [
        ("add_card_document", "add_document", {"iid": 682}),
        ("add_card_folder", "add_folder", {"iid": 682}),
        ("create_folder", "create_folder", {"title": "test", "due_date": "2026-10-05"}),
        ("create_document", "create_document", {"title": "test", "due_date": "2026-10-05", "folder_id": None}),
    ],
)
@pytest.mark.parametrize("owner", ["same", "other_user", "other_chat"])
async def test_resume_resource_dispatches_only_original_owner_operation(tools, owner, kind, method, payload):
    from editorial_bot.agent.tools.editorial import Resume

    ctx = context()
    key = "a" * 24
    await tools.store.operation(key, "first", ctx.chat_id, ctx.user_id, kind, payload)
    tools.workflow = SimpleNamespace(
        add_document=AsyncMock(return_value={"iid": 682}),
        add_folder=AsyncMock(return_value={"iid": 682}),
        create_folder=AsyncMock(return_value={"folder_id": "folder"}),
        create_document=AsyncMock(return_value={"folder_id": None}),
        create=AsyncMock(),
    )
    if owner == "other_user":
        ctx.user_id += 1
    elif owner == "other_chat":
        ctx.chat_id -= 1
    if owner == "same":
        await tools.resume(Resume(operation_id=key), ctx)
        getattr(tools.workflow, method).assert_awaited_once_with(payload.get("iid", payload), ctx, resume_id=key)
    else:
        with pytest.raises(ValueError, match="找不到"):
            await tools.resume(Resume(operation_id=key), ctx)
        getattr(tools.workflow, method).assert_not_awaited()
    tools.workflow.create.assert_not_awaited()


@pytest.mark.parametrize(
    "arguments,expected",
    [
        ({}, [673, 674]),
        ({"include_review": True}, [672, 673, 674]),
        ({"status": "Review"}, [672]),
        ({"label_filters": ["Status::Review"]}, [672]),
        ({"open_only": False}, [672, 673, 674, 675]),
        ({"due_before": "2026-10-05", "assignee_id": 1}, [673]),
    ],
)
async def test_unfinished_search_excludes_review_but_explicit_review_remains_searchable(tools, arguments, expected):
    from editorial_bot.agent.tools.editorial import Search

    issues = [
        {"iid": 672, "labels": ["Status::Review"], "state": "opened", "due_date": "2026-10-04"},
        {"iid": 673, "labels": ["Status::Doing"], "state": "opened", "due_date": "2026-10-05"},
        {"iid": 674, "labels": ["Status::Report"], "state": "opened", "due_date": "2026-10-06"},
        {"iid": 675, "labels": [], "state": "closed", "due_date": "2026-10-04"},
    ]

    async def search(**params):
        return [
            issue
            for issue in issues
            if (params["state"] == "all" or issue["state"] == params["state"])
            and (not params.get("labels") or params["labels"] in issue["labels"])
        ]

    tools.gl.search = AsyncMock(side_effect=search)
    tools.gl.labels = AsyncMock(return_value=[{"name": "Status::Review"}, {"name": "Status::Doing"}])
    result = await tools.search(Search(**arguments), context())
    assert [issue["iid"] for issue in result] == expected
    if arguments.get("assignee_id"):
        assert tools.gl.search.call_args.kwargs["assignee_id"] == 1


async def test_only_review_cards_means_no_unfinished_work(tools):
    from editorial_bot.agent.tools.editorial import Search

    tools.gl.search = AsyncMock(return_value=[{"iid": 672, "labels": [tools.settings.review_status]}])
    assert await tools.search(Search(assignee_id=1), context()) == []
    tools.gl.update.assert_not_awaited()


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


async def test_partial_review_failure_does_not_allow_empty_reply(tools):
    ctx = context()
    tools.gl.update.side_effect = [None, RemoteError("GitLab", 503)]
    results = await tools.review(Review(targets=["1", "2"]), ctx)
    assert len(ctx.notices) == 1
    assert "error" in results[1]
    assert not ctx.review_completed


@pytest.mark.parametrize(
    "name,method,extra",
    [("create_card_folder", "create_folder", {}), ("create_card_document", "create_document", {"folder_id": "folder"})],
)
async def test_resource_tools_without_card_send_title_and_date_to_standalone_workflow(tools, name, method, extra):
    from zoneinfo import ZoneInfo

    from pydantic import ValidationError

    tools.workflow = SimpleNamespace(**{method: AsyncMock(return_value={"folder_id": "folder"})})
    tool = next(t for t in tools.build() if t.name == name)
    due = f"{datetime.now(ZoneInfo('Asia/Taipei')).year}-09-25"
    ctx = ToolContext(-1, 3, 7, "author_one", "小石，只要資料夾跟文案 0925 test，不開卡", event_id="standalone")
    await tool.run(tool.args_model(title="test", due_date=due, **extra), ctx)
    expected = {"title": "test", "due_date": due, **({"folder_id": extra.get("folder_id")} if extra else {})}
    getattr(tools.workflow, method).assert_awaited_once_with(expected, ctx)
    # The date must come from the user's own words, exactly as for create_card.
    ctx.text = "小石，只要資料夾 test"
    with pytest.raises(ValueError, match="到期日"):
        await tool.run(tool.args_model(title="test", due_date=due), ctx)
    getattr(tools.workflow, method).assert_awaited_once()
    for bad in ({}, {"title": "test"}, {"due_date": due}, {"title": "test", "due_date": "09/25"}):
        with pytest.raises(ValidationError):
            tool.args_model(**bad)
