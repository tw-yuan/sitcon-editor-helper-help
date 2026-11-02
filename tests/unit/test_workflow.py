import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from editorial_bot.agent.tools.base import ToolContext
from editorial_bot.services.gitlab import RemoteError
from editorial_bot.services.workflow import CardWorkflow, operation_key
from editorial_bot.storage.db import Store


@pytest.fixture
async def setup(tmp_path):
    store = await Store.open(str(tmp_path / "db.sqlite3"))
    google = SimpleNamespace(
        preflight=AsyncMock(),
        find_resource=AsyncMock(return_value=None),
        create_folder=AsyncMock(return_value={"id": "folder"}),
        copy_template=AsyncMock(return_value={"id": "doc"}),
        fill_document=AsyncMock(),
    )
    issue = {
        "iid": 2,
        "title": "測試",
        "web_url": "https://gitlab.com/p/-/issues/2",
        "labels": ["社群文案", "Status::Inbox"],
        "assignees": [{"id": 1}],
    }
    gitlab = SimpleNamespace(
        validate_labels=AsyncMock(side_effect=lambda names, **kw: names),
        search=AsyncMock(return_value=[]),
        create=AsyncMock(return_value=issue),
        get=AsyncMock(return_value=issue),
    )
    workflow = CardWorkflow(gitlab, google, store, SimpleNamespace(drive_root_folder_id="root"))
    ctx = ToolContext(-1, 5, 7, "writer", "開卡 10/15", event_id="event1")
    payload = {
        "title": "測試",
        "due_date": "2026-10-15",
        "document": True,
        "labels": issue["labels"],
        "assignee_ids": [1],
    }
    yield workflow, google, gitlab, store, ctx, payload
    await store.close()


@pytest.fixture
def new_card(setup):
    """GitLab stub whose issue mirrors create() and later description updates, as the three-step flow needs."""
    workflow, google, gitlab, store, ctx, payload = setup
    issue = gitlab.get.return_value
    issue["due_date"] = payload["due_date"]

    async def create(fields):
        issue.update(description=fields["description"])
        return dict(issue)

    async def update(iid, **fields):
        issue.update(fields)
        return dict(issue)

    gitlab.create.side_effect = create
    gitlab.update = AsyncMock(side_effect=update)
    google.metadata = AsyncMock(
        return_value={"id": "folder", "mimeType": "application/vnd.google-apps.folder", "parents": ["root"]}
    )
    return workflow, google, gitlab, store, ctx, payload, issue


async def test_default_flow_card_then_folder_then_document_links_everything(new_card):
    workflow, google, gitlab, store, ctx, payload, issue = new_card
    card = await workflow.create(payload, ctx)
    assert card["folder_url"] is None and card["document_url"] is None
    folder = await workflow.add_folder(card["iid"], ctx)
    assert folder["folder_url"].endswith("/folder") and folder["document_url"] is None
    google.create_folder.assert_awaited_once_with("1015_測試", folder["operation_id"])
    doc = await workflow.add_document(card["iid"], ctx)
    assert doc["folder_url"].endswith("/folder") and doc["document_url"].endswith("/doc/edit")
    assert "note" not in doc
    google.copy_template.assert_awaited_once_with("folder", "1015_測試", doc["operation_id"])
    google.fill_document.assert_awaited_once_with("doc", "folder", "測試", "2026-10-15", issue["web_url"])
    description = issue["description"]
    assert description.count("資料夾：https://drive.google.com/drive/folders/folder") == 1
    assert description.count("文案：https://docs.google.com/document/d/doc/edit") == 1
    assert "\n\n資料夾：" in description and "\n\n文案：" in description
    assert "editorial-operation:" in description
    resource = await store.one("SELECT * FROM resources WHERE issue_iid=2")
    assert resource["folder_id"] == "folder" and resource["document_id"] == "doc"
    gitlab.create.assert_awaited_once()
    assert gitlab.update.await_count == 2
    # Repeating any step returns the receipt without new writes.
    ctx.event_id = "again"
    assert await workflow.add_folder(2, ctx) == folder and await workflow.add_document(2, ctx) == doc
    google.create_folder.assert_awaited_once()
    google.copy_template.assert_awaited_once()


async def test_document_without_folder_falls_back_to_root_and_reports_it(new_card):
    workflow, google, _gitlab, store, ctx, payload, issue = new_card
    card = await workflow.create(payload, ctx)
    doc = await workflow.add_document(card["iid"], ctx)
    assert doc["folder_url"] is None and doc["document_url"].endswith("/doc/edit")
    assert "根目錄" in doc["note"]
    google.create_folder.assert_not_awaited()
    google.copy_template.assert_awaited_once_with("root", "1015_測試", doc["operation_id"])
    google.fill_document.assert_awaited_once_with("doc", "root", "測試", "2026-10-15", issue["web_url"])
    assert (
        "資料夾：" not in issue["description"]
        and "文案：https://docs.google.com/document/d/doc/edit" in issue["description"]
    )
    resource = await store.one("SELECT * FROM resources WHERE issue_iid=2")
    assert resource["folder_id"] is None and resource["document_id"] == "doc"
    # A folder added afterwards keeps the document mapping and only appends its own link.
    folder = await workflow.add_folder(2, ctx)
    assert folder["document_url"].endswith("/doc/edit") and folder["folder_url"].endswith("/folder")
    resource = await store.one("SELECT * FROM resources WHERE issue_iid=2")
    assert resource["folder_id"] == "folder" and resource["document_id"] == "doc"
    assert issue["description"].count("文案：") == 1 and issue["description"].count("資料夾：") == 1


async def test_document_waits_for_unfinished_folder_instead_of_falling_back_to_root(new_card):
    workflow, google, gitlab, store, ctx, payload, _issue = new_card
    await workflow.create(payload, ctx)
    gitlab.update.side_effect = RemoteError("GitLab", 503)
    with pytest.raises(ValueError, match="操作"):
        await workflow.add_folder(2, ctx)
    with pytest.raises(ValueError, match="資料夾操作"):
        await workflow.add_document(2, ctx)
    google.copy_template.assert_not_awaited()
    assert await store.one("SELECT 1 FROM operations WHERE kind='add_card_document'") is None


async def test_folder_resume_after_document_failure_does_not_duplicate_resources(new_card):
    workflow, google, _gitlab, store, ctx, payload, issue = new_card
    await workflow.create(payload, ctx)
    await workflow.add_folder(2, ctx)
    google.fill_document.side_effect = RemoteError("Google", 503, uncertain=True)
    with pytest.raises(ValueError, match="操作"):
        await workflow.add_document(2, ctx)
    operation = await store.one("SELECT * FROM operations WHERE kind='add_card_document'")
    google.fill_document.side_effect = None
    ctx.event_id = "resume-event"
    result = await workflow.add_document(2, ctx, resume_id=operation["id"])
    assert result["folder_url"].endswith("/folder") and result["document_url"].endswith("/doc/edit")
    google.create_folder.assert_awaited_once()
    google.copy_template.assert_awaited_once()
    assert result["folder_url"] in issue["description"] and result["document_url"] in issue["description"]
    assert await workflow.add_document(2, ctx, resume_id=operation["id"]) == result


async def test_ambiguous_folder_write_is_not_repeated_if_resource_not_found(new_card):
    workflow, google, _gitlab, store, ctx, payload, _issue = new_card
    await workflow.create(payload, ctx)
    google.create_folder.side_effect = RemoteError("Google", None, uncertain=True)
    with pytest.raises(ValueError):
        await workflow.add_folder(2, ctx)
    operation = await store.one("SELECT id FROM operations WHERE kind='add_card_folder'")
    with pytest.raises(ValueError, match="結果仍不明"):
        await workflow.add_folder(2, ctx, resume_id=operation["id"])
    google.create_folder.assert_awaited_once()
    google.find_resource.side_effect = [{"id": "folder"}]
    result = await workflow.add_folder(2, ctx, resume_id=operation["id"])
    assert result["folder_url"].endswith("/folder")
    google.create_folder.assert_awaited_once()


async def test_task_card_only_creates_gitlab_issue_even_if_drive_is_unavailable(setup):
    workflow, google, gitlab, store, ctx, payload = setup
    for method in vars(google).values():
        method.side_effect = AssertionError("僅開卡不得存取 Drive 或 Docs")
    result = await workflow.create(payload, ctx)
    assert result["folder_url"] is None and result["document_url"] is None
    assert result["issue_url"].endswith("/2")
    gitlab.create.assert_awaited_once()
    for method in vars(google).values():
        method.assert_not_awaited()
    description = gitlab.create.call_args.args[0]["description"]
    assert "資料夾：" not in description and "文案：" not in description
    assert "editorial-resources" not in description
    assert "建立者：" not in description and "editorial-operation:" in description
    resource = await store.one("SELECT * FROM resources WHERE issue_iid=2")
    assert resource["folder_id"] is None and resource["document_id"] is None
    assert await workflow.create(payload, ctx) == result
    gitlab.create.assert_awaited_once()


async def test_invalid_labels_stop_before_google_write(setup):
    workflow, google, gitlab, _store, ctx, payload = setup
    gitlab.validate_labels.side_effect = ValueError("unknown label")
    with pytest.raises(ValueError):
        await workflow.create(payload, ctx)
    google.create_folder.assert_not_awaited()


async def test_resource_links_render_as_separate_paragraphs(setup):
    workflow, _google, gitlab, store, ctx, payload = setup
    # Historical payloads may still include requester / document flags; they must not be rendered or acted on.
    payload.update(document=True, requester="@gitlab_writer", description="原有說明")
    await workflow.create(payload, ctx)
    description = gitlab.create.call_args.args[0]["description"]
    assert "建立者：" not in description and "@gitlab_writer" not in description
    assert "\n\n<!-- editorial-operation:" in description
    assert (await store.one("SELECT user_id FROM operations"))["user_id"] == ctx.user_id
    assert "資料夾：" not in description and "文案：" not in description
    assert description.startswith("原有說明\n\n")


async def test_task_card_unknown_gitlab_write_resumes_without_google_or_duplicate_issue(setup):
    workflow, google, gitlab, store, ctx, payload = setup
    gitlab.create.side_effect = RemoteError("GitLab", None, uncertain=True)
    with pytest.raises(ValueError, match="操作"):
        await workflow.create(payload, ctx)
    op = await store.one("SELECT * FROM operations")
    assert json.loads(op["steps"])["pending"] == "issue"
    with pytest.raises(ValueError, match="結果仍不明"):
        await workflow.create(payload, ctx, resume_id=op["id"])
    gitlab.search.return_value = [
        {**gitlab.get.return_value, "description": f"<!-- editorial-operation:{op['id']} -->"}
    ]
    result = await workflow.create(payload, ctx, resume_id=op["id"])
    assert result["folder_url"] is None and result["document_url"] is None
    gitlab.create.assert_awaited_once()
    for method in vars(google).values():
        method.assert_not_awaited()


async def test_legacy_task_resume_preserves_existing_folder_without_google_calls(setup):
    workflow, google, gitlab, store, ctx, payload = setup
    payload["requester"] = "@legacy_writer"
    op = await store.operation("legacy-task", ctx.event_id, ctx.chat_id, ctx.user_id, "create_card", payload)
    await store.checkpoint(op["id"], {"folder": {"id": "old-folder"}}, "error")
    result = await workflow.create(payload, ctx, resume_id=op["id"])
    assert result["folder_url"].endswith("/old-folder")
    assert result["folder_url"] in gitlab.create.call_args.args[0]["description"]
    assert "建立者：" not in gitlab.create.call_args.args[0]["description"]
    assert (await store.one("SELECT folder_id FROM resources"))["folder_id"] == "old-folder"
    for method in vars(google).values():
        method.assert_not_awaited()


async def test_legacy_task_with_unknown_folder_write_does_not_start_new_steps(setup):
    workflow, google, gitlab, store, ctx, payload = setup
    op = await store.operation("legacy-unknown", ctx.event_id, ctx.chat_id, ctx.user_id, "create_card", payload)
    await store.checkpoint(op["id"], {"pending": "folder"}, "uncertain")
    with pytest.raises(ValueError, match="人工查核"):
        await workflow.create(payload, ctx, resume_id=op["id"])
    gitlab.create.assert_not_awaited()
    assert json.loads((await store.one("SELECT steps FROM operations"))["steps"])["pending"] == "folder"
    for method in vars(google).values():
        method.assert_not_awaited()


@pytest.fixture
async def existing_card(setup):
    workflow, google, gitlab, store, ctx, _payload = setup
    issue = gitlab.get.return_value
    issue.update(due_date="2026-10-05", description="原說明\n\n建立者：@writer\n\n<!-- editorial-operation:old -->")

    async def update(iid, **fields):
        assert iid == 2
        assert set(fields) == {"description"}
        issue.update(fields)
        return dict(issue)

    gitlab.update = AsyncMock(side_effect=update)
    google.metadata = AsyncMock(
        return_value={"id": "folder", "mimeType": "application/vnd.google-apps.folder", "parents": ["root"]}
    )
    await store.execute("INSERT INTO resources VALUES (2,NULL,NULL,'old-card-only')")
    return workflow, google, gitlab, store, ctx, issue


async def test_add_folder_and_document_preserve_existing_card_and_deduplicate_across_events(existing_card):
    workflow, google, gitlab, store, ctx, issue = existing_card
    original = dict(issue)
    folder = await workflow.add_folder(2, ctx)
    result = await workflow.add_document(2, ctx)
    assert result["iid"] == 2 and result["document_url"].endswith("/doc/edit")
    assert issue["description"].startswith(original["description"])
    assert result["folder_url"] in issue["description"] and result["document_url"] in issue["description"]
    for field in ("title", "due_date", "labels", "assignees"):
        assert issue[field] == original[field]
    google.create_folder.assert_awaited_once_with("1005_測試", folder["operation_id"])
    google.fill_document.assert_awaited_once_with("doc", "folder", "測試", "2026-10-05", issue["web_url"])
    resource = await store.one("SELECT * FROM resources WHERE issue_iid=2")
    assert resource["folder_id"] == "folder" and resource["document_id"] == "doc"
    ctx.event_id = "new-request"
    assert await workflow.add_folder(2, ctx) == folder
    assert await workflow.add_document(2, ctx) == result
    gitlab.create.assert_not_awaited()
    google.create_folder.assert_awaited_once()
    google.copy_template.assert_awaited_once()
    assert gitlab.update.await_count == 2


async def test_pre_split_document_operation_resumes_in_the_folder_it_created(existing_card):
    workflow, google, _gitlab, store, ctx, issue = existing_card
    key = operation_key("issue:2", "add_card_document", {"iid": 2})
    payload = {"iid": 2, "title": "測試", "due_date": "2026-10-05", "folder_id": None}
    await store.operation(key, "old", ctx.chat_id, ctx.user_id, "add_card_document", payload)
    await store.checkpoint(key, {"folder": {"id": "old-folder"}}, "error")
    result = await workflow.add_document(2, ctx, resume_id=key)
    google.create_folder.assert_not_awaited()
    google.copy_template.assert_awaited_once_with("old-folder", "1005_測試", key)
    google.fill_document.assert_awaited_once_with("doc", "old-folder", "測試", "2026-10-05", issue["web_url"])
    assert result["folder_url"].endswith("/old-folder") and result["document_url"].endswith("/doc/edit")
    assert result["folder_url"] in issue["description"] and result["document_url"] in issue["description"]
    resource = await store.one("SELECT * FROM resources WHERE issue_iid=2")
    assert resource["folder_id"] == "old-folder" and resource["document_id"] == "doc"


@pytest.mark.parametrize("failure", ["copy_template", "fill_document", "update"])
async def test_add_document_resumes_after_failure_without_duplicate_resources(existing_card, failure):
    workflow, google, gitlab, store, ctx, _issue = existing_card
    method = getattr(gitlab if failure == "update" else google, failure)
    normal = method.side_effect
    method.side_effect = RemoteError("service", 403)
    with pytest.raises(ValueError, match="操作"):
        await workflow.add_document(2, ctx)
    op = await store.one("SELECT * FROM operations WHERE kind='add_card_document'")
    assert op["state"] == "error"
    method.side_effect = normal
    ctx.event_id = "resume"
    result = await workflow.add_document(2, ctx, resume_id=op["id"])
    assert result["operation_id"] == op["id"]
    google.create_folder.assert_not_awaited()
    assert google.copy_template.await_count == (2 if failure == "copy_template" else 1)
    assert google.copy_template.call_args.args[0] == "root"
    gitlab.create.assert_not_awaited()


async def test_add_document_unknown_write_recovers_only_when_resource_found(existing_card):
    workflow, google, gitlab, store, ctx, _issue = existing_card
    google.copy_template.side_effect = RemoteError("Google", None, uncertain=True)
    with pytest.raises(ValueError, match="操作"):
        await workflow.add_document(2, ctx)
    op = await store.one("SELECT * FROM operations")
    with pytest.raises(ValueError, match="結果仍不明"):
        await workflow.add_document(2, ctx, resume_id=op["id"])
    google.copy_template.assert_awaited_once()
    google.find_resource.return_value = {"id": "doc"}
    await workflow.add_document(2, ctx, resume_id=op["id"])
    google.copy_template.assert_awaited_once()
    gitlab.create.assert_not_awaited()


async def test_add_document_lost_update_response_reads_links_before_retry(existing_card):
    workflow, google, gitlab, _store, ctx, issue = existing_card

    async def uncertain_update(iid, **fields):
        issue.update(fields)
        raise RemoteError("GitLab", None, uncertain=True)

    gitlab.update.side_effect = uncertain_update
    with pytest.raises(ValueError):
        await workflow.add_document(2, ctx)
    result = await workflow.add_document(2, ctx)
    assert result["document_url"] in issue["description"]
    gitlab.update.assert_awaited_once()
    google.copy_template.assert_awaited_once()


@pytest.mark.parametrize("source", ["description", "storage"])
async def test_add_document_returns_existing_document_without_writes(existing_card, source):
    workflow, google, gitlab, store, ctx, issue = existing_card
    if source == "description":
        issue["description"] += "\nhttps://docs.google.com/document/d/existing/edit"
    else:
        await store.execute("UPDATE resources SET document_id='existing',folder_id='folder' WHERE issue_iid=2")
    result = await workflow.add_document(2, ctx)
    assert result["document_url"].endswith("/existing/edit")
    assert result["already_exists"]
    google.create_folder.assert_not_awaited()
    google.copy_template.assert_not_awaited()
    google.fill_document.assert_not_awaited()
    gitlab.update.assert_not_awaited()


async def test_add_document_reuses_existing_folder(existing_card):
    workflow, google, _gitlab, store, ctx, issue = existing_card
    await store.execute("UPDATE resources SET folder_id='folder' WHERE issue_iid=2")
    issue["description"] += "\n資料夾：https://drive.google.com/drive/folders/folder"
    result = await workflow.add_folder(2, ctx)
    assert result["already_exists"] and result["folder_url"].endswith("/folder")
    await workflow.add_document(2, ctx)
    google.create_folder.assert_not_awaited()
    google.copy_template.assert_awaited_once_with(
        "folder", "1005_測試", operation_key("issue:2", "add_card_document", {"iid": 2})
    )
    assert issue["description"].count("https://drive.google.com/drive/folders/folder") == 1


@pytest.mark.parametrize("invalid", ["date", "folder_scope", "conflicting_links", "unfinished_create"])
async def test_add_document_validation_stops_before_creating_resources(existing_card, invalid):
    workflow, google, gitlab, store, ctx, issue = existing_card
    if invalid == "date":
        issue["due_date"] = None
    elif invalid == "folder_scope":
        await store.execute("UPDATE resources SET folder_id='folder' WHERE issue_iid=2")
        google.metadata.return_value["parents"] = ["other-root"]
    elif invalid == "conflicting_links":
        issue["description"] += (
            "\nhttps://docs.google.com/document/d/one/edit\nhttps://docs.google.com/document/d/two/edit"
        )
    else:
        await store.operation("old-card-only", "original", ctx.chat_id, ctx.user_id, "create_card", {})
    with pytest.raises(ValueError):
        await workflow.add_document(2, ctx)
    google.create_folder.assert_not_awaited()
    google.copy_template.assert_not_awaited()
    gitlab.update.assert_not_awaited()


async def test_add_document_incomplete_operation_cannot_be_resumed_by_other_owner(existing_card):
    workflow, google, _gitlab, _store, ctx, _issue = existing_card
    google.fill_document.side_effect = ValueError("失敗")
    with pytest.raises(ValueError):
        await workflow.add_document(2, ctx)
    ctx.user_id += 1
    with pytest.raises(ValueError, match="原操作者"):
        await workflow.add_document(2, ctx)
    google.fill_document.assert_awaited_once()


async def test_add_document_concurrent_requests_share_one_resource_set(existing_card):
    import asyncio

    workflow, google, gitlab, _store, ctx, _issue = existing_card
    first, second = await asyncio.gather(workflow.add_folder(2, ctx), workflow.add_folder(2, ctx))
    assert first == second
    first, second = await asyncio.gather(workflow.add_document(2, ctx), workflow.add_document(2, ctx))
    assert first == second
    google.create_folder.assert_awaited_once()
    google.copy_template.assert_awaited_once()
    assert gitlab.update.await_count == 2


async def test_add_document_retry_detects_newly_linked_document_before_more_writes(existing_card):
    workflow, google, _gitlab, _store, ctx, issue = existing_card
    google.copy_template.side_effect = RemoteError("Google", 403)
    with pytest.raises(ValueError):
        await workflow.add_document(2, ctx)
    issue["description"] += "\nhttps://docs.google.com/document/d/manual/edit"
    google.copy_template.side_effect = None
    with pytest.raises(ValueError, match="其他資源"):
        await workflow.add_document(2, ctx)
    google.copy_template.assert_awaited_once()


async def test_add_document_rejects_mismatched_resume_operation(existing_card):
    workflow, google, gitlab, _store, ctx, _issue = existing_card
    with pytest.raises(ValueError, match="不符"):
        await workflow.add_document(2, ctx, resume_id="b" * 24)
    gitlab.get.assert_not_awaited()
    google.preflight.assert_not_awaited()


@pytest.fixture
def standalone(setup):
    """Folder/document without a card: Drive only, any GitLab call is a failure."""
    workflow, google, gitlab, store, ctx, _payload = setup
    for method in vars(gitlab).values():
        method.side_effect = AssertionError("不開卡時不得存取 GitLab")
    google.metadata = AsyncMock(
        return_value={"id": "folder", "mimeType": "application/vnd.google-apps.folder", "parents": ["root"]}
    )
    return workflow, google, gitlab, store, ctx, {"title": "測試", "due_date": "2026-10-15"}


async def test_standalone_folder_creates_only_drive_folder_and_dedupes_per_event(standalone):
    workflow, google, gitlab, store, ctx, payload = standalone
    result = await workflow.create_folder(payload, ctx)
    assert result["folder_id"] == "folder" and result["folder_url"].endswith("/folder")
    assert result["document_url"] is None and "iid" not in result and "issue_url" not in result
    assert "卡片" in result["note"]
    google.create_folder.assert_awaited_once_with("1015_測試", result["operation_id"])
    google.copy_template.assert_not_awaited()
    for method in vars(gitlab).values():
        method.assert_not_awaited()
    assert (await store.one("SELECT kind,state FROM operations"))["kind"] == "create_folder"
    # Same Telegram event: receipt only. A new event is a new request and builds another folder.
    assert await workflow.create_folder(payload, ctx) == result
    google.create_folder.assert_awaited_once()
    ctx.event_id = "event2"
    again = await workflow.create_folder(payload, ctx)
    assert again["operation_id"] != result["operation_id"]
    assert google.create_folder.await_count == 2


async def test_standalone_document_in_root_blanks_card_link_and_reports_root(standalone):
    workflow, google, gitlab, store, ctx, payload = standalone
    result = await workflow.create_document(payload, ctx)
    assert result["document_url"].endswith("/doc/edit") and result["folder_url"] is None
    assert "根目錄" in result["note"]
    google.metadata.assert_not_awaited()
    google.copy_template.assert_awaited_once_with("root", "1015_測試", result["operation_id"])
    google.fill_document.assert_awaited_once_with("doc", "root", "測試", "2026-10-15", None)
    for method in vars(gitlab).values():
        method.assert_not_awaited()
    assert (await store.one("SELECT kind FROM operations"))["kind"] == "create_document"
    assert await store.one("SELECT 1 FROM resources") is None


async def test_standalone_document_goes_into_folder_built_in_the_same_turn(standalone):
    workflow, google, _gitlab, _store, ctx, payload = standalone
    folder = await workflow.create_folder(payload, ctx)
    result = await workflow.create_document({**payload, "folder_id": folder["folder_id"]}, ctx)
    assert result["folder_url"] == folder["folder_url"] and result["document_url"].endswith("/doc/edit")
    assert "根目錄" not in result["note"]
    google.metadata.assert_awaited_once_with("folder")
    google.copy_template.assert_awaited_once_with("folder", "1015_測試", result["operation_id"])
    google.fill_document.assert_awaited_once_with("doc", "folder", "測試", "2026-10-15", None)
    assert result["operation_id"] != folder["operation_id"]


@pytest.mark.parametrize("problem", ["not_a_folder", "outside_root"])
async def test_standalone_document_rejects_folder_id_that_is_not_under_root(standalone, problem):
    workflow, google, _gitlab, store, ctx, payload = standalone
    if problem == "not_a_folder":
        google.metadata.return_value["mimeType"] = "application/vnd.google-apps.document"
    else:
        google.metadata.return_value["parents"] = ["other-root"]
    with pytest.raises(ValueError, match="核准根目錄"):
        await workflow.create_document({**payload, "folder_id": "folder"}, ctx)
    google.copy_template.assert_not_awaited()
    google.fill_document.assert_not_awaited()
    assert (await store.one("SELECT state FROM operations"))["state"] == "error"


async def test_standalone_document_resume_fills_without_second_copy(standalone):
    workflow, google, _gitlab, store, ctx, payload = standalone
    google.fill_document.side_effect = RemoteError("Google", 503, uncertain=True)
    with pytest.raises(ValueError, match="操作"):
        await workflow.create_document(payload, ctx)
    op = await store.one("SELECT * FROM operations WHERE kind='create_document'")
    google.fill_document.side_effect = None
    ctx.event_id = "resume"
    result = await workflow.create_document(payload, ctx, resume_id=op["id"])
    assert result["operation_id"] == op["id"] and result["document_url"].endswith("/doc/edit")
    google.copy_template.assert_awaited_once()
    assert google.fill_document.await_count == 2
    assert await workflow.create_document(payload, ctx, resume_id=op["id"]) == result


async def test_standalone_operation_cannot_be_resumed_by_other_owner(standalone):
    workflow, google, _gitlab, store, ctx, payload = standalone
    google.create_folder.side_effect = RemoteError("Google", 403)
    with pytest.raises(ValueError):
        await workflow.create_folder(payload, ctx)
    key = (await store.one("SELECT id FROM operations"))["id"]
    google.create_folder.side_effect = None
    ctx.user_id += 1
    with pytest.raises(ValueError, match="只能接續自己"):
        await workflow.create_folder(payload, ctx, resume_id=key)
    google.create_folder.assert_awaited_once()
