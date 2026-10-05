import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from editorial_bot.agent.tools.base import ToolContext
from editorial_bot.services.gitlab import RemoteError
from editorial_bot.services.workflow import CardWorkflow
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
        "requester": "@writer",
    }
    yield workflow, google, gitlab, store, ctx, payload
    await store.close()


async def test_resume_after_document_failure_does_not_duplicate_resources(setup):
    workflow, google, gitlab, store, ctx, payload = setup
    google.fill_document.side_effect = RemoteError("Google", 503, uncertain=True)
    with pytest.raises(ValueError, match="操作"):
        await workflow.create(payload, ctx)
    operation = await store.one("SELECT * FROM operations")
    google.fill_document.side_effect = None
    ctx.event_id = "resume-event"
    result = await workflow.create(payload, ctx, resume_id=operation["id"])
    assert result["folder_url"].endswith("/folder")
    assert result["document_url"].endswith("/doc/edit")
    google.create_folder.assert_awaited_once_with("1015_測試", operation["id"])
    google.copy_template.assert_awaited_once()
    gitlab.create.assert_awaited_once()
    description = gitlab.create.call_args.args[0]["description"]
    assert result["folder_url"] in description and result["document_url"] in description
    assert await workflow.create(payload, ctx, resume_id=operation["id"]) == result


async def test_ambiguous_write_is_not_repeated_if_resource_not_found(setup):
    workflow, google, _gitlab, store, ctx, payload = setup
    google.create_folder.side_effect = RemoteError("Google", None, uncertain=True)
    with pytest.raises(ValueError):
        await workflow.create(payload, ctx)
    operation = await store.one("SELECT id FROM operations")
    with pytest.raises(ValueError, match="結果仍不明"):
        await workflow.create(payload, ctx, resume_id=operation["id"])
    google.create_folder.assert_awaited_once()


async def test_task_card_only_creates_gitlab_issue_even_if_drive_is_unavailable(setup):
    workflow, google, gitlab, store, ctx, payload = setup
    payload["document"] = False
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
    assert "建立者：@writer" in description and "editorial-operation:" in description
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


async def test_unknown_write_can_recover_using_remote_operation_marker(setup):
    workflow, google, _gitlab, store, ctx, payload = setup
    google.create_folder.side_effect = RemoteError("Google", None, uncertain=True)
    with pytest.raises(ValueError):
        await workflow.create(payload, ctx)
    operation = await store.one("SELECT id FROM operations")
    google.find_resource.side_effect = [{"id": "folder"}, None]
    await workflow.create(payload, ctx, resume_id=operation["id"])
    google.create_folder.assert_awaited_once()


@pytest.mark.parametrize("document", [True, False])
async def test_resource_links_render_as_separate_paragraphs(setup, document):
    workflow, _google, gitlab, _store, ctx, payload = setup
    payload.update(document=document, requester="@gitlab_writer", description="原有說明")
    await workflow.create(payload, ctx)
    description = gitlab.create.call_args.args[0]["description"]
    assert "\n\n建立者：@gitlab_writer\n\n" in description
    if document:
        assert "\n\n資料夾：https://drive.google.com/drive/folders/folder\n\n" in description
        assert "\n\n文案：https://docs.google.com/document/d/doc/edit\n\n" in description
    else:
        assert "資料夾：" not in description and "文案：" not in description
    assert description.startswith("原有說明\n\n")


async def test_task_card_unknown_gitlab_write_resumes_without_google_or_duplicate_issue(setup):
    workflow, google, gitlab, store, ctx, payload = setup
    payload["document"] = False
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
    payload["document"] = False
    op = await store.operation("legacy-task", ctx.event_id, ctx.chat_id, ctx.user_id, "create_card", payload)
    await store.checkpoint(op["id"], {"folder": {"id": "old-folder"}}, "error")
    result = await workflow.create(payload, ctx, resume_id=op["id"])
    assert result["folder_url"].endswith("/old-folder")
    assert result["folder_url"] in gitlab.create.call_args.args[0]["description"]
    assert (await store.one("SELECT folder_id FROM resources"))["folder_id"] == "old-folder"
    for method in vars(google).values():
        method.assert_not_awaited()


async def test_legacy_task_with_unknown_folder_write_does_not_start_new_steps(setup):
    workflow, google, gitlab, store, ctx, payload = setup
    payload["document"] = False
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


async def test_add_document_preserves_existing_card_and_deduplicates_across_events(existing_card):
    workflow, google, gitlab, store, ctx, issue = existing_card
    original = dict(issue)
    result = await workflow.add_document(2, ctx)
    assert result["iid"] == 2 and result["document_url"].endswith("/doc/edit")
    assert issue["description"].startswith(original["description"])
    assert result["folder_url"] in issue["description"] and result["document_url"] in issue["description"]
    for field in ("title", "due_date", "labels", "assignees"):
        assert issue[field] == original[field]
    google.create_folder.assert_awaited_once_with("1005_測試", result["operation_id"])
    google.fill_document.assert_awaited_once_with("doc", "folder", "測試", "2026-10-05", issue["web_url"])
    resource = await store.one("SELECT * FROM resources WHERE issue_iid=2")
    assert resource["folder_id"] == "folder" and resource["document_id"] == "doc"
    ctx.event_id = "new-request"
    assert await workflow.add_document(2, ctx) == result
    gitlab.create.assert_not_awaited()
    google.copy_template.assert_awaited_once()
    gitlab.update.assert_awaited_once()


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
    google.create_folder.assert_awaited_once()
    assert google.copy_template.await_count == (2 if failure == "copy_template" else 1)
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
    await workflow.add_document(2, ctx)
    google.create_folder.assert_not_awaited()
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
    first, second = await asyncio.gather(workflow.add_document(2, ctx), workflow.add_document(2, ctx))
    assert first == second
    google.create_folder.assert_awaited_once()
    google.copy_template.assert_awaited_once()
    gitlab.update.assert_awaited_once()


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
