from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
