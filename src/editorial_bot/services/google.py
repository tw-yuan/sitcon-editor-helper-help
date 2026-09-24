"""Scoped Drive folder/template workflow and read-only roster access."""

import asyncio
from datetime import date

from googleapiclient.errors import HttpError

from .gitlab import RemoteError
from .google_http import build_google_service, request_http


def folder_name(title: str, due_date: str) -> str:
    return date.fromisoformat(due_date).strftime("%m%d") + "_" + title.strip()


def quote_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class Google:
    def __init__(self, settings):
        self.settings = settings
        scopes = ["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets.readonly"]
        self.drive, self.creds = build_google_service("drive", "v3", settings.google_sa_json_path, scopes)
        self.docs, _ = build_google_service("docs", "v1", settings.google_sa_json_path, scopes)
        self.sheets, _ = build_google_service("sheets", "v4", settings.google_sa_json_path, scopes)

    async def execute(self, request, *, write: bool = False):
        def run():
            try:
                return request.execute(http=request_http(self.creds), num_retries=0 if write else 2)
            except HttpError as exc:
                raise RemoteError("Google", exc.resp.status, uncertain=write and exc.resp.status >= 500) from None
            except Exception:
                raise RemoteError("Google", None, uncertain=write) from None

        return await asyncio.to_thread(run)

    async def roster_values(self) -> list[list[str]]:
        meta = await self.execute(
            self.sheets.spreadsheets().get(
                spreadsheetId=self.settings.roster_sheet_id, fields="sheets(properties(sheetId,title))"
            )
        )
        tab = next(
            (
                s["properties"]["title"]
                for s in meta["sheets"]
                if s["properties"]["sheetId"] == self.settings.roster_sheet_gid
            ),
            None,
        )
        if tab is None:
            raise ValueError("找不到指定的名冊 gid")
        quoted = "'" + tab.replace("'", "''") + "'!A:AZ"
        result = await self.execute(
            self.sheets.spreadsheets().values().get(spreadsheetId=self.settings.roster_sheet_id, range=quoted)
        )
        return result.get("values", [])

    async def metadata(self, file_id: str) -> dict:
        return await self.execute(
            self.drive.files().get(
                fileId=file_id,
                supportsAllDrives=True,
                fields="id,name,mimeType,driveId,parents,webViewLink,capabilities(canAddChildren,canCopy,canEdit)",
            )
        )

    async def preflight(self, needs_document: bool = True) -> dict:
        root = await self.metadata(self.settings.drive_root_folder_id)
        if root.get("mimeType") != "application/vnd.google-apps.folder" or not root.get("driveId"):
            raise ValueError("目標必須為已授權的共用雲端硬碟資料夾。")
        if not root.get("capabilities", {}).get("canAddChildren"):
            raise ValueError("Google service account 無法在目標資料夾建立內容。")
        if needs_document:
            template = await self.metadata(self.settings.doc_template_id)
            if template.get("mimeType") != "application/vnd.google-apps.document":
                raise ValueError("範本必須為 Google Docs 文件。")
            if not template.get("capabilities", {}).get("canCopy"):
                raise ValueError("Google service account 無法複製文案範本。")
        return root

    async def find_resource(self, parent: str, operation: str, kind: str) -> dict | None:
        q = (
            f"'{quote_query(parent)}' in parents and trashed=false and "
            f"appProperties has {{ key='editorial_operation' and value='{quote_query(operation)}' }} and "
            f"appProperties has {{ key='editorial_resource' and value='{quote_query(kind)}' }}"
        )
        result = await self.execute(
            self.drive.files().list(
                q=q,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                fields="files(id,name,webViewLink),nextPageToken",
                pageSize=100,
            )
        )
        files = result.get("files", [])
        if len(files) > 1 or result.get("nextPageToken"):
            raise ValueError("操作識別找到多個 Drive 資源，請人工查核後再接續。")
        return files[0] if files else None

    async def create_folder(self, name: str, operation: str) -> dict:
        return await self.execute(
            self.drive.files().create(
                body={
                    "name": name,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [self.settings.drive_root_folder_id],
                    "appProperties": {"editorial_operation": operation, "editorial_resource": "folder"},
                },
                supportsAllDrives=True,
                fields="id,name,webViewLink",
            ),
            write=True,
        )

    async def copy_template(self, folder_id: str, name: str, operation: str) -> dict:
        # Folder must remain directly within the configured editorial root.
        folder = await self.metadata(folder_id)
        if self.settings.drive_root_folder_id not in folder.get("parents", []):
            raise ValueError("文案資料夾不在核准根目錄下。")
        return await self.execute(
            self.drive.files().copy(
                fileId=self.settings.doc_template_id,
                supportsAllDrives=True,
                body={
                    "name": name,
                    "parents": [folder_id],
                    "appProperties": {"editorial_operation": operation, "editorial_resource": "document"},
                },
                fields="id,name,webViewLink",
            ),
            write=True,
        )

    async def fill_document(self, document_id: str, folder_id: str, title: str, due_date: str, issue_url: str) -> None:
        metadata = await self.metadata(document_id)
        if folder_id not in metadata.get("parents", []):
            raise ValueError("文件與本次資料夾不相符")
        folder_url = f"https://drive.google.com/drive/folders/{folder_id}"
        values = {"TITTLE": title, "GITLAB_LINK": issue_url, "DATE": due_date.replace("-", "/"), "DIR_LINK": folder_url}
        requests = [
            {"replaceAllText": {"containsText": {"text": key, "matchCase": True}, "replaceText": value}}
            for key, value in values.items()
        ]
        await self.execute(
            self.docs.documents().batchUpdate(documentId=document_id, body={"requests": requests}), write=True
        )
        doc = await self.execute(self.docs.documents().get(documentId=document_id, includeTabsContent=True))
        text = document_text(doc)
        if issue_url not in text or folder_url not in text:
            raise ValueError("文案範本缺少 GITLAB_LINK／DIR_LINK，連結尚未完整填入。")
        styles = link_styles(doc, [issue_url, folder_url])
        if styles:
            await self.execute(
                self.docs.documents().batchUpdate(documentId=document_id, body={"requests": styles}), write=True
            )


def document_text(value) -> str:
    if isinstance(value, list):
        return "".join(document_text(x) for x in value)
    if isinstance(value, dict):
        if "textRun" in value:
            return value["textRun"].get("content", "")
        return "".join(document_text(x) for x in value.values())
    return ""


def link_styles(document: dict, urls: list[str]) -> list[dict]:
    requests = []

    def walk(value, tab_id=None):
        if isinstance(value, list):
            for item in value:
                walk(item, tab_id)
        elif isinstance(value, dict):
            if "tabProperties" in value:
                tab_id = value["tabProperties"].get("tabId", tab_id)
            if "textRun" in value and "startIndex" in value:
                text = value["textRun"].get("content", "")
                for url in urls:
                    offset = text.find(url)
                    if offset >= 0:
                        start = value["startIndex"] + len(text[:offset].encode("utf-16-le")) // 2
                        span = {"startIndex": start, "endIndex": start + len(url.encode("utf-16-le")) // 2}
                        if tab_id:
                            span["tabId"] = tab_id
                        requests.append(
                            {"updateTextStyle": {"range": span, "textStyle": {"link": {"url": url}}, "fields": "link"}}
                        )
            for item in value.values():
                walk(item, tab_id)

    walk(document)
    return requests
