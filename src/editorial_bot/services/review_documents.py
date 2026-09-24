"""Read-only PDF snapshots and narrowly scoped, revision-guarded sign-in edits."""

import re

from .gitlab import RemoteError
from .google import document_text

MAX_PDF_BYTES = 10 * 1024 * 1024


def signature_request(document: dict, label: str) -> dict | None:
    matches = []

    def walk(value, tab_id=None):
        if isinstance(value, list):
            for item in value:
                walk(item, tab_id)
        elif isinstance(value, dict):
            if "tabProperties" in value:
                tab_id = value["tabProperties"].get("tabId")
            if "paragraph" in value:
                text = document_text(value["paragraph"])
                anchor = re.match(r"^[ \t]*校稿簽到串[：:]", text)
                if anchor:
                    matches.append((value, text, anchor.end(), tab_id))
            for key, item in value.items():
                if key not in ("headers", "footers", "footnotes"):
                    walk(item, tab_id)

    walk(document)
    if len(matches) != 1:
        raise ValueError("文件必須有唯一的「校稿簽到串：」欄位，請先確認文案。")
    paragraph, text, offset, tab_id = matches[0]
    tail = text[offset:].rstrip("\n")
    if re.search(r"(?:^|[、，,;；\s])" + re.escape(label) + r"(?=$|[、，,;；\s])", tail, re.I):
        return None
    existing = tail.rstrip("、 \t")
    offset += len(existing)
    location = {"index": paragraph["startIndex"] + len(text[:offset].encode("utf-16-le")) // 2}
    if tab_id:
        location["tabId"] = tab_id
    return {"insertText": {"location": location, "text": ("、" if existing else "") + label}}


class ReviewDocuments:
    def __init__(self, google):
        self.google = google

    async def validate(self, document_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", document_id):
            raise ValueError("文案文件 ID 不正確")
        meta = await self.google.metadata(document_id)
        if meta.get("mimeType") != "application/vnd.google-apps.document":
            raise ValueError("文案必須是 Google Docs 文件")
        parents = meta.get("parents", [])
        root = self.google.settings.drive_root_folder_id
        inside = root in parents
        if not inside and len(parents) == 1:
            folder = await self.google.metadata(parents[0])
            inside = root in folder.get("parents", [])
        if not inside:
            raise ValueError("文案不在核准的編輯組資料夾內，停止匯出與簽到。")
        if not meta.get("capabilities", {}).get("canEdit"):
            raise ValueError("Google service account 無法編輯文案簽到欄。")
        return meta

    async def read(self, document_id):
        return await self.google.execute(
            self.google.docs.documents().get(documentId=document_id, includeTabsContent=True)
        )

    async def export_pdf(self, document_id: str) -> bytes:
        meta = await self.validate(document_id)
        if not meta.get("capabilities", {}).get("canDownload"):
            raise ValueError("文案不允許下載 PDF")
        doc = await self.read(document_id)
        signature_request(doc, "@check_sign_in_field")  # Validate only; no document writes.
        pdf = await self.google.execute(
            self.google.drive.files().export_media(fileId=document_id, mimeType="application/pdf")
        )
        if not isinstance(pdf, bytes) or not pdf.startswith(b"%PDF-") or len(pdf) > MAX_PDF_BYTES:
            raise ValueError("文案 PDF 匯出失敗或超過 10 MB，請調整文件後重試。")
        return pdf

    async def sign(self, document_id: str, label: str):
        if not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}|Telegram ID [0-9]+", label):
            raise ValueError("簽到身分格式不正確")
        await self.validate(document_id)
        for _ in range(3):
            doc = await self.read(document_id)
            request = signature_request(doc, label)
            if request is None:
                return
            if not doc.get("revisionId"):
                raise ValueError("文件未提供版本識別，無法安全寫入簽到。")
            try:
                await self.google.execute(
                    self.google.docs.documents().batchUpdate(
                        documentId=document_id,
                        body={"requests": [request], "writeControl": {"requiredRevisionId": doc["revisionId"]}},
                    ),
                    write=True,
                )
            except RemoteError as exc:
                if exc.status == 400 or exc.uncertain:
                    # Re-read before retrying. The revision guard prevents duplicate inserts.
                    continue
                raise
            if signature_request(await self.read(document_id), label) is None:
                return
        raise ValueError("文件仍在變更或簽到結果不明，請稍後再按一次；不會重複留名。")
