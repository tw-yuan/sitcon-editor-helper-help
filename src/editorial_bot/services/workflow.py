"""Durable card/folder/document coordination; no automatic resource deletion."""

import asyncio
import hashlib
import json
from datetime import date

from .gitlab import RemoteError
from .google import folder_name


def operation_key(event: str, kind: str, payload: dict) -> str:
    raw = json.dumps([event, kind, payload], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def resource_links(folder_id: str, document_id: str | None) -> str:
    lines = ["<!-- editorial-resources:start -->", f"資料夾：https://drive.google.com/drive/folders/{folder_id}"]
    if document_id:
        lines.append(f"文案：https://docs.google.com/document/d/{document_id}/edit")
    lines.append("<!-- editorial-resources:end -->")
    return "\n".join(lines)


class CardWorkflow:
    def __init__(self, gitlab, google, store, settings):
        self.gitlab = gitlab
        self.google = google
        self.store = store
        self.settings = settings
        self.locks: dict[str, asyncio.Lock] = {}

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
            await self.google.preflight(payload["document"])
            marker = f"editorial-operation:{key}"
            name = folder_name(payload["title"], payload["due_date"])

            async def step(kind, find, create):
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

            try:
                folder = await step(
                    "folder",
                    lambda: self.google.find_resource(self.settings.drive_root_folder_id, key, "folder"),
                    lambda: self.google.create_folder(name, key),
                )
                doc = None
                if payload["document"]:
                    doc = await step(
                        "document",
                        lambda: self.google.find_resource(folder["id"], key, "document"),
                        lambda: self.google.copy_template(folder["id"], name, key),
                    )

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
                        resource_links(folder["id"], doc["id"] if doc else None),
                        f"建立者：{payload['requester']}",
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
                    (issue["iid"], folder["id"], doc["id"] if doc else None, key),
                )
                if doc and not steps.get("document_filled"):
                    # Replacing the same template tokens is idempotent; safe to resume after a timeout.
                    await self.google.fill_document(
                        doc["id"], folder["id"], payload["title"], payload["due_date"], issue["web_url"]
                    )
                    steps["document_filled"] = True
                    await self.store.checkpoint(key, steps)
                result = {
                    "operation_id": key,
                    "iid": issue["iid"],
                    "title": issue["title"],
                    "issue_url": issue["web_url"],
                    "folder_url": f"https://drive.google.com/drive/folders/{folder['id']}",
                    "document_url": f"https://docs.google.com/document/d/{doc['id']}/edit" if doc else None,
                    "due_date": payload["due_date"],
                    "assignee_ids": payload["assignee_ids"],
                }
                await self.store.finish(key, result)
                return result
            except Exception as exc:
                await self.store.checkpoint(key, steps, "uncertain" if steps.get("pending") else "error", str(exc))
                completed = [k for k in ("folder", "document", "issue", "document_filled") if k in steps]
                links = {}
                if steps.get("folder"):
                    links["folder"] = f"https://drive.google.com/drive/folders/{steps['folder']['id']}"
                if steps.get("document"):
                    links["document"] = f"https://docs.google.com/document/d/{steps['document']['id']}/edit"
                if steps.get("issue"):
                    links["issue"] = steps["issue"].get("web_url")
                raise ValueError(
                    f"開卡尚未全部完成。操作 {key}；已完成：{', '.join(completed) or '無'}。"
                    f"{exc}。已知連結：{json.dumps(links, ensure_ascii=False)}。可要求接續此操作。"
                ) from exc
