"""Durable card/folder/document coordination; no automatic resource deletion."""

import asyncio
import hashlib
import json
import re
from datetime import date
from functools import partial

from .gitlab import RemoteError
from .google import folder_name

# Operation kinds whose receipts carry issue/folder/document links (resume and crash-recovery summaries).
RESOURCE_OPERATIONS = frozenset(
    {"create_card", "add_card_folder", "add_card_document", "create_folder", "create_document"}
)


def operation_key(event: str, kind: str, payload: dict) -> str:
    raw = json.dumps([event, kind, payload], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def resource_links(folder_id: str | None, document_id: str | None) -> str:
    if not folder_id and not document_id:
        return ""
    lines = ["<!-- editorial-resources:start -->"]
    if folder_id:
        lines.append(f"資料夾：https://drive.google.com/drive/folders/{folder_id}")
    if document_id:
        lines.append(f"文案：https://docs.google.com/document/d/{document_id}/edit")
    lines.append("<!-- editorial-resources:end -->")
    # Blank lines keep each link in its own paragraph in GitLab descriptions.
    return "\n\n".join(lines)


class CardWorkflow:
    def __init__(self, gitlab, google, store, settings):
        self.gitlab = gitlab
        self.google = google
        self.store = store
        self.settings = settings
        self.locks: dict[str, asyncio.Lock] = {}

    async def _step(self, key, steps, kind, find, create):
        if kind in steps:
            return steps[kind]
        existing = await find()
        if existing:
            steps[kind] = existing
            steps.pop("pending", None)
            await self.store.checkpoint(key, steps)
            return existing
        if steps.get("pending") == kind:
            raise ValueError(f"{kind} 的前次寫入結果仍不明，尚未查到關聯資源；請人工查核，避免重複建立。")
        steps["pending"] = kind
        await self.store.checkpoint(key, steps)
        try:
            result = await create()
        except RemoteError as exc:
            if not exc.uncertain:
                steps.pop("pending", None)
            raise
        steps[kind] = result
        steps.pop("pending", None)
        await self.store.checkpoint(key, steps)
        return result

    async def create(self, payload: dict, ctx, *, resume_id: str | None = None) -> dict:
        payload = {**payload, "title": payload["title"].strip()}
        date.fromisoformat(payload["due_date"])
        if not payload["title"].strip():
            raise ValueError("卡片標題不可空白")
        key = resume_id or operation_key(ctx.event_id, "create_card", payload)
        async with self.locks.setdefault(key, asyncio.Lock()):
            if not resume_id:
                unfinished = await self.store.one(
                    "SELECT id FROM operations WHERE event_id=? AND kind='create_card' AND state!='done' AND id!=?",
                    (ctx.event_id, key),
                )
                if unfinished:
                    raise ValueError(
                        f"本輪已有未完成開卡 {unfinished['id']}，請用 resume_operation 接續，不要另開一份。"
                    )
            op = await self.store.operation(key, ctx.event_id, ctx.chat_id, ctx.user_id, "create_card", payload)
            if op["chat_id"] != ctx.chat_id or op["user_id"] != ctx.user_id:
                raise ValueError("只能接續自己在本群的開卡操作。")
            if op["state"] == "done":
                return json.loads(op["result"])
            steps = op["steps"]
            labels = await self.gitlab.validate_labels(payload["labels"], required=True)
            marker = f"editorial-operation:{key}"

            step = partial(self._step, key, steps)

            try:
                # Preserve already-created folders when resuming a card from the pre-split workflow.
                folder = steps.get("folder")
                if steps.get("pending") in {"folder", "document"}:
                    raise ValueError("舊版 Drive 寫入結果仍不明，請人工查核既有資源後再接續。")

                async def find_issue():
                    matches = await self.gitlab.search(state="all", search=marker, **{"in": "description"})
                    exact = [i for i in matches if f"<!-- {marker} -->" in (i.get("description") or "")]
                    if len(exact) > 1:
                        raise ValueError("同一操作找到多張卡片，請人工查核。")
                    return exact[0] if exact else None

                description = "\n\n".join(
                    x
                    for x in [
                        payload.get("description", ""),
                        resource_links(folder["id"] if folder else None, None),
                        f"<!-- {marker} -->",
                    ]
                    if x
                )
                issue = await step(
                    "issue",
                    find_issue,
                    lambda: self.gitlab.create(
                        {
                            "title": payload["title"],
                            "due_date": payload["due_date"],
                            "assignee_ids": payload["assignee_ids"],
                            "labels": labels,
                            "description": description,
                        }
                    ),
                )
                actual = await self.gitlab.get(issue["iid"])
                if not set(labels).issubset(actual.get("labels", [])):
                    raise ValueError("卡片已建立，但 label 未全部套用，請檢查權限及實際結果。")
                if {a["id"] for a in actual.get("assignees", [])} != set(payload["assignee_ids"]):
                    raise ValueError("卡片已建立，但負責人未完整套用；請檢查名冊及專案指派限制。")
                await self.store.execute(
                    "INSERT OR REPLACE INTO resources(issue_iid,folder_id,document_id,operation_id) VALUES (?,?,?,?)",
                    (issue["iid"], folder["id"] if folder else None, None, key),
                )
                result = {
                    "operation_id": key,
                    "iid": issue["iid"],
                    "title": issue["title"],
                    "issue_url": issue["web_url"],
                    "folder_url": f"https://drive.google.com/drive/folders/{folder['id']}" if folder else None,
                    "document_url": None,
                    "due_date": payload["due_date"],
                    "assignee_ids": payload["assignee_ids"],
                }
                await self.store.finish(key, result)
                return result
            except Exception as exc:
                await self.store.checkpoint(key, steps, "uncertain" if steps.get("pending") else "error", str(exc))
                completed = [k for k in ("folder", "issue") if k in steps]
                links = {}
                if steps.get("folder"):
                    links["folder"] = f"https://drive.google.com/drive/folders/{steps['folder']['id']}"
                if steps.get("issue"):
                    links["issue"] = steps["issue"].get("web_url")
                raise ValueError(
                    f"開卡尚未全部完成。操作 {key}；已完成：{', '.join(completed) or '無'}。"
                    f"{exc}。已知連結：{json.dumps(links, ensure_ascii=False)}。可要求接續此操作。"
                ) from exc

    @staticmethod
    def _linked_resources(issue, resource):
        text = issue.get("description") or ""
        folders = set(re.findall(r"https://drive\.google\.com/drive/folders/([A-Za-z0-9_-]+)", text))
        documents = set(re.findall(r"https://docs\.google\.com/document/d/([A-Za-z0-9_-]+)", text))
        if resource:
            if resource["folder_id"]:
                folders.add(resource["folder_id"])
            if resource["document_id"]:
                documents.add(resource["document_id"])
        if len(folders) > 1 or len(documents) > 1:
            raise ValueError("卡片有多個或互相衝突的資料夾／文案連結，請先查核，避免覆蓋既有資源。")
        return next(iter(folders), None), next(iter(documents), None)

    async def _approved_folder(self, folder_id: str) -> None:
        # folder_id may come from the model: it must be a folder directly under the approved root.
        folder = await self.google.metadata(folder_id)
        root = self.settings.drive_root_folder_id
        if folder.get("mimeType") != "application/vnd.google-apps.folder" or root not in folder.get("parents", []):
            raise ValueError("指定的資料夾不在核准根目錄下，停止建立。")

    async def _make_resource(self, kind, key, steps, name, parent, title, due_date, issue_url) -> dict:
        """Find-or-create one Drive resource under `parent`; documents are also filled in."""
        step = partial(self._step, key, steps)
        if kind == "folder":
            return await step(
                "folder",
                lambda: self.google.find_resource(parent, key, "folder"),
                lambda: self.google.create_folder(name, key),
            )
        created = await step(
            "document",
            lambda: self.google.find_resource(parent, key, "document"),
            lambda: self.google.copy_template(parent, name, key),
        )
        if not steps.get("document_filled"):
            await self.google.fill_document(created["id"], parent, title, due_date, issue_url)
            steps["document_filled"] = True
            await self.store.checkpoint(key, steps)
        return created

    async def create_folder(self, payload: dict, ctx, *, resume_id: str | None = None) -> dict:
        return await self._create_resource("folder", payload, ctx, resume_id=resume_id)

    async def create_document(self, payload: dict, ctx, *, resume_id: str | None = None) -> dict:
        return await self._create_resource("document", payload, ctx, resume_id=resume_id)

    async def _create_resource(self, kind: str, payload: dict, ctx, *, resume_id: str | None) -> dict:
        """Create a standalone Drive folder or template document without touching GitLab."""
        root = self.settings.drive_root_folder_id
        payload = {
            "title": payload["title"].strip(),
            "due_date": payload["due_date"],
            **({"folder_id": payload.get("folder_id") or None} if kind == "document" else {}),
        }
        date.fromisoformat(payload["due_date"])
        if not payload["title"]:
            raise ValueError("標題不可空白")
        key = resume_id or operation_key(ctx.event_id, f"create_{kind}", payload)
        async with self.locks.setdefault(key, asyncio.Lock()):
            op = await self.store.operation(key, ctx.event_id, ctx.chat_id, ctx.user_id, f"create_{kind}", payload)
            if op["chat_id"] != ctx.chat_id or op["user_id"] != ctx.user_id:
                raise ValueError("只能接續自己在本群的操作。")
            if op["state"] == "done":
                return json.loads(op["result"])
            steps = op["steps"]
            try:
                await self.google.preflight(kind == "document")
                parent = payload.get("folder_id") or root
                if payload.get("folder_id"):
                    await self._approved_folder(parent)
                name = folder_name(payload["title"], payload["due_date"])
                created = await self._make_resource(
                    kind, key, steps, name, parent, payload["title"], payload["due_date"], None
                )
                folder_value = created["id"] if kind == "folder" else payload.get("folder_id")
                doc_value = created["id"] if kind == "document" else None
                result = {
                    "operation_id": key,
                    "title": payload["title"],
                    "due_date": payload["due_date"],
                    "name": name,
                    "folder_url": f"https://drive.google.com/drive/folders/{folder_value}" if folder_value else None,
                    "document_url": f"https://docs.google.com/document/d/{doc_value}/edit" if doc_value else None,
                    "folder_id": folder_value,
                    "note": "未建立 GitLab 卡片，資源沒有連結到任何卡片。",
                }
                if kind == "document" and not folder_value:
                    result["note"] += "文案放在 Drive 根目錄下。"
                await self.store.finish(key, result)
                return result
            except Exception as exc:
                await self.store.checkpoint(key, steps, "uncertain" if steps.get("pending") else "error", str(exc))
                links = {}
                if steps.get("folder"):
                    links["folder"] = f"https://drive.google.com/drive/folders/{steps['folder']['id']}"
                if steps.get("document"):
                    links["document"] = f"https://docs.google.com/document/d/{steps['document']['id']}/edit"
                completed = [k for k in ("folder", "document", "document_filled") if k in steps]
                raise ValueError(
                    f"建立尚未完成。操作 {key}；已完成：{', '.join(completed) or '無'}。"
                    f"{exc}。已知連結：{json.dumps(links, ensure_ascii=False)}。可要求接續此操作。"
                ) from exc

    async def add_folder(self, iid: int, ctx, *, resume_id: str | None = None) -> dict:
        return await self._add_resource(iid, ctx, "folder", resume_id=resume_id)

    async def add_document(self, iid: int, ctx, *, resume_id: str | None = None) -> dict:
        return await self._add_resource(iid, ctx, "document", resume_id=resume_id)

    async def _add_resource(self, iid: int, ctx, kind: str, *, resume_id: str | None) -> dict:
        """Create one Drive resource for an existing card and append its link to the card."""
        root = self.settings.drive_root_folder_id
        # One durable operation per card and kind, including requests from later Telegram events.
        key = operation_key(f"issue:{iid}", f"add_card_{kind}", {"iid": iid})
        if resume_id is not None and resume_id != key:
            raise ValueError("補建操作與卡片不符。")
        async with self.locks.setdefault(key, asyncio.Lock()):
            previous = await self.store.one("SELECT * FROM operations WHERE id=?", (key,))
            if previous and previous["state"] == "done":
                return json.loads(previous["result"])
            if previous and (previous["chat_id"], previous["user_id"]) != (ctx.chat_id, ctx.user_id):
                raise ValueError(f"此卡已有未完成補建操作 {key}，限原操作者在原群接續。")
            issue = await self.gitlab.get(iid)
            resource = await self.store.one("SELECT * FROM resources WHERE issue_iid=?", (iid,))
            if resource and resource["operation_id"] != key:
                original = await self.store.one(
                    "SELECT id,state FROM operations WHERE id=?", (resource["operation_id"],)
                )
                if original and original["state"] != "done":
                    raise ValueError(f"此卡的前一個操作 {original['id']} 尚未完成，請用 resume_operation 接續。")
            folder_id, document_id = self._linked_resources(issue, resource)
            linked = {"folder": folder_id, "document": document_id}
            if linked[kind] and not previous:
                return {
                    "iid": iid,
                    "issue_url": issue["web_url"],
                    "folder_url": f"https://drive.google.com/drive/folders/{folder_id}" if folder_id else None,
                    "document_url": f"https://docs.google.com/document/d/{document_id}/edit" if document_id else None,
                    "already_exists": True,
                }
            if previous:
                payload = json.loads(previous["payload"])
                # Pre-split add_card_document operations created the folder themselves; keep using it.
                payload["folder_id"] = payload["folder_id"] or json.loads(previous["steps"]).get("folder", {}).get("id")
            else:
                if not issue.get("due_date"):
                    raise ValueError("卡片尚未設定到期日，請先提供明確日期並更新卡片，再補建。")
                payload = {
                    "iid": iid,
                    "title": issue["title"],
                    "due_date": date.fromisoformat(issue["due_date"]).isoformat(),
                    "folder_id": folder_id,
                }
            if kind == "document" and not payload["folder_id"]:
                # A half-finished folder step must be resumed first, or the document lands in the root.
                folder_op = await self.store.one(
                    "SELECT id,state FROM operations WHERE id=?",
                    (operation_key(f"issue:{iid}", "add_card_folder", {"iid": iid}),),
                )
                if folder_op and folder_op["state"] != "done":
                    raise ValueError(f"此卡的資料夾操作 {folder_op['id']} 尚未完成，請先用 resume_operation 接續。")
            op = await self.store.operation(key, ctx.event_id, ctx.chat_id, ctx.user_id, f"add_card_{kind}", payload)
            steps = op["steps"]
            try:
                expected = {"folder": payload["folder_id"], "document": steps.get("document", {}).get("id")}
                if kind == "folder":
                    expected["folder"] = steps.get("folder", {}).get("id")
                if any(linked[k] and linked[k] != expected[k] for k in (kind, "folder")):
                    raise ValueError("卡片已連結其他資源，停止補建；請先人工查核。")
                await self.google.preflight(kind == "document")
                parent = root if kind == "folder" else payload["folder_id"] or root
                if kind == "document" and payload["folder_id"]:
                    await self._approved_folder(parent)
                name = folder_name(payload["title"], payload["due_date"])
                created = await self._make_resource(
                    kind, key, steps, name, parent, payload["title"], payload["due_date"], issue["web_url"]
                )
                # Refresh before appending links so edits since the original request survive.
                current = await self.gitlab.get(iid)
                current_folder, current_doc = self._linked_resources(current, resource)
                if {"folder": current_folder, "document": current_doc}[kind] not in (None, created["id"]):
                    raise ValueError("卡片已連結其他資源，停止更新；請人工查核本次已建立的資源。")
                description = current.get("description") or ""
                linked_folder, linked_doc = self._linked_resources(current, None)
                new_folder = created["id"] if kind == "folder" else payload["folder_id"]
                new_doc = created["id"] if kind == "document" else None
                links = resource_links(None if linked_folder else new_folder, None if linked_doc else new_doc)
                if links:
                    await self.gitlab.update(iid, description="\n\n".join(x for x in (description, links) if x))
                steps["issue_linked"] = True
                await self.store.checkpoint(key, steps)
                folder_value = new_folder or current_folder
                doc_value = new_doc or current_doc
                await self.store.execute(
                    "INSERT OR REPLACE INTO resources(issue_iid,folder_id,document_id,operation_id) VALUES (?,?,?,?)",
                    (iid, folder_value, doc_value, key),
                )
                result = {
                    "operation_id": key,
                    "iid": iid,
                    "title": current["title"],
                    "issue_url": current["web_url"],
                    "folder_url": f"https://drive.google.com/drive/folders/{folder_value}" if folder_value else None,
                    "document_url": f"https://docs.google.com/document/d/{doc_value}/edit" if doc_value else None,
                    "due_date": payload["due_date"],
                }
                if kind == "document" and not payload["folder_id"]:
                    result["note"] = (
                        "這張卡沒有資料夾，文案放在 Drive 根目錄下；需要資料夾可再用 create_card_folder 建立。"
                    )
                await self.store.finish(key, result)
                return result
            except Exception as exc:
                await self.store.checkpoint(key, steps, "uncertain" if steps.get("pending") else "error", str(exc))
                links = {"issue": issue["web_url"]}
                if steps.get("folder"):
                    links["folder"] = f"https://drive.google.com/drive/folders/{steps['folder']['id']}"
                if steps.get("document"):
                    links["document"] = f"https://docs.google.com/document/d/{steps['document']['id']}/edit"
                completed = [k for k in ("folder", "document", "document_filled", "issue_linked") if k in steps]
                raise ValueError(
                    f"補建尚未全部完成。操作 {key}；已完成：{', '.join(completed) or '無'}。"
                    f"{exc}。已知連結：{json.dumps(links, ensure_ascii=False)}。可要求接續此操作。"
                ) from exc
