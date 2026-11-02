from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from editorial_bot.services.google import Google


async def test_document_resume_does_not_replace_tokens_inside_user_title():
    google = Google.__new__(Google)
    google.settings = SimpleNamespace(drive_root_folder_id="root")
    google.docs = MagicMock()
    google.metadata = AsyncMock(side_effect=[{"parents": ["folder"]}, {"parents": ["root"]}] * 2)
    issue = "https://gitlab.com/p/-/issues/1"
    folder = "https://drive.google.com/drive/folders/folder"
    original = {"body": {"textRun": {"content": "TITTLE DATE GITLAB_LINK DIR_LINK"}}}
    filled = {"body": {"textRun": {"content": f"UPDATE 2027/01/05 {issue} {folder}"}}}
    google.execute = AsyncMock(side_effect=[original, {}, filled, filled])
    await google.fill_document("doc", "folder", "UPDATE", "2027-01-05", issue)
    await google.fill_document("doc", "folder", "UPDATE", "2027-01-05", issue)
    google.docs.documents().batchUpdate.assert_called_once()
    requests = google.docs.documents().batchUpdate.call_args.kwargs["body"]["requests"]
    assert requests[-1]["replaceAllText"]["containsText"]["text"] == "TITTLE"
    assert requests[-1]["replaceAllText"]["replaceText"] == "UPDATE"


async def test_root_folder_is_accepted_as_document_parent_fallback():
    google = Google.__new__(Google)
    google.settings = SimpleNamespace(drive_root_folder_id="root", doc_template_id="template")
    google.drive = MagicMock()
    google.metadata = AsyncMock(side_effect=[{"id": "root", "parents": []}, {"id": "other", "parents": ["elsewhere"]}])
    google.execute = AsyncMock(return_value={"id": "doc"})
    assert await google.copy_template("root", "1015_測試", "op") == {"id": "doc"}
    assert google.drive.files().copy.call_args.kwargs["body"]["parents"] == ["root"]
    with pytest.raises(ValueError, match="核准根目錄"):
        await google.copy_template("other", "1015_測試", "op")


async def test_document_without_card_blanks_gitlab_link_and_styles_only_folder_url():
    google = Google.__new__(Google)
    google.settings = SimpleNamespace(drive_root_folder_id="root")
    google.docs = MagicMock()
    google.metadata = AsyncMock(side_effect=[{"parents": ["root"]}, {"id": "root", "parents": []}])
    folder = "https://drive.google.com/drive/folders/root"
    original = {"body": {"textRun": {"content": "TITTLE DATE GITLAB_LINK DIR_LINK"}}}
    filled = {"body": {"startIndex": 1, "textRun": {"content": f"測試 2026/10/15  {folder}"}}}
    google.execute = AsyncMock(side_effect=[original, {}, filled, {}])
    await google.fill_document("doc", "root", "測試", "2026-10-15", None)
    replace, style = [c.kwargs["body"]["requests"] for c in google.docs.documents().batchUpdate.call_args_list]
    values = {r["replaceAllText"]["containsText"]["text"]: r["replaceAllText"]["replaceText"] for r in replace}
    assert values["GITLAB_LINK"] == "" and values["DIR_LINK"] == folder
    assert [s["updateTextStyle"]["textStyle"]["link"]["url"] for s in style] == [folder]
