from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from editorial_bot.services.gitlab import RemoteError
from editorial_bot.services.review_documents import ReviewDocuments, signature_request


def document(text="校稿簽到串：、、、、\n", revision="v1"):
    return {
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": "t.0"},
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "startIndex": 76,
                                "paragraph": {
                                    "elements": [{"textRun": {"content": text[:3]}}, {"textRun": {"content": text[3:]}}]
                                },
                            }
                        ]
                    }
                },
            }
        ],
    }


def test_signing_preserves_names_placeholders_and_uses_utf16_tab_index():
    doc = document("校稿簽到串：小😀、@first、、\n")
    request = signature_request(doc, "@next")
    insertion = request["insertText"]
    assert insertion["text"] == "、@next"
    assert insertion["location"] == {
        "index": 76 + len("校稿簽到串：小😀、@first".encode("utf-16-le")) // 2,
        "tabId": "t.0",
    }
    assert signature_request(document("校稿簽到串：@FIRST、@other、、\n"), "@first") is None
    assert signature_request(document("校稿簽到串：@first_more、、\n"), "@first") is not None
    assert signature_request(document(), "@first")["insertText"]["text"] == "@first"


@pytest.mark.parametrize("duplicate", [False, True])
def test_missing_or_ambiguous_sign_in_line_is_not_modified(duplicate):
    doc = document("本文沒有簽到列\n")
    if duplicate:
        doc = document()
        doc["tabs"].append(doc["tabs"][0])
    with pytest.raises(ValueError, match="簽到"):
        signature_request(doc, "@first")


def service():
    google = SimpleNamespace(
        settings=SimpleNamespace(drive_root_folder_id="root"),
        docs=MagicMock(),
        drive=MagicMock(),
        metadata=AsyncMock(
            side_effect=[
                {
                    "mimeType": "application/vnd.google-apps.document",
                    "parents": ["folder"],
                    "capabilities": {"canEdit": True, "canDownload": True},
                },
                {"parents": ["root"]},
            ]
        ),
        execute=AsyncMock(),
    )
    return ReviewDocuments(google), google


async def test_export_is_read_only_and_rejects_non_pdf():
    review, google = service()
    google.execute.side_effect = [document(), b"%PDF-1.7\ncontent"]
    assert await review.export_pdf("doc") == b"%PDF-1.7\ncontent"
    google.drive.files().export_media.assert_called_once_with(fileId="doc", mimeType="application/pdf")
    assert all(not c.kwargs.get("write") for c in google.execute.call_args_list)
    review, google = service()
    google.execute.side_effect = [document(), b"HTML error"]
    with pytest.raises(ValueError, match="PDF"):
        await review.export_pdf("doc")


async def test_document_outside_editorial_root_cannot_export_or_sign():
    review, google = service()
    google.metadata.side_effect = [
        {"mimeType": "application/vnd.google-apps.document", "parents": ["outside"], "capabilities": {"canEdit": True}},
        {"parents": ["other-root"]},
    ]
    with pytest.raises(ValueError, match="核准"):
        await review.sign("doc", "@first")
    google.execute.assert_not_awaited()


@pytest.mark.parametrize("failure", [None, RemoteError("Google", 400), RemoteError("Google", None, uncertain=True)])
async def test_revision_guard_and_uncertain_write_recovery(failure):
    review, google = service()
    signed = document("校稿簽到串：@first、、\n", "v2")
    google.execute.side_effect = [document(), failure or {}, signed]
    await review.sign("doc", "@first")
    writes = [c for c in google.execute.call_args_list if c.kwargs.get("write")]
    assert len(writes) == 1
    body = google.docs.documents().batchUpdate.call_args.kwargs["body"]
    assert body["writeControl"] == {"requiredRevisionId": "v1"}
