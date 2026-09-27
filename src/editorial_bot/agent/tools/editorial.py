"""Validated editorial tools, with scoped memory and durable write receipts."""

import html
import json
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...review_packets import ReviewPackets
from ...services.gitlab import RemoteError
from ...services.review_documents import ReviewDocuments
from ...services.workflow import operation_key, resource_links
from .base import Tool
from .external_data import wrap_external


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Empty(Args):
    pass


class Person(Args):
    query: str = Field(description="Telegram username，或「我」；不以暱稱猜人。")


class Create(Args):
    title: str = Field(min_length=1, max_length=255)
    due_date: str = Field(description="使用者明確指定日期轉成 YYYY-MM-DD；未給年份用台灣當前年份。")
    document: bool = Field(
        False,
        description="預設 false，僅建 GitLab 卡片；明確要求文案卡或建立文案才為 true，建立 Drive 資料夾與 Docs。",
    )
    description: str = ""
    labels: list[str] = Field(default_factory=list, description="精確使用既有 label；空值採文案／任務預設分類。")
    assignee_ids: list[int] = Field(default_factory=list, description="名冊 GitLab ID；空值使用唯一 default=yes。")

    @field_validator("due_date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("日期必須為 YYYY-MM-DD")
        return date.fromisoformat(value).isoformat()


class Issue(Args):
    iid: int = Field(gt=0)
    include_notes: bool = False


class Search(Args):
    title_query: str = ""
    label_filters: list[str] = Field(default_factory=list)
    assignee_id: int | None = Field(None, gt=0)
    status: str | None = None
    open_only: bool = True
    due_before: date | None = None


class Update(Args):
    iid: int = Field(gt=0)
    title: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    due_date: date | None = None
    clear_due_date: bool = False
    set_assignee_ids: list[int] | None = None
    add_labels: list[str] = Field(default_factory=list)
    remove_labels: list[str] = Field(default_factory=list)
    status: str | None = Field(None, description="現有狀態，例如 Doing、Review、Report。")


class Comment(Args):
    iid: int = Field(gt=0)
    body: str = Field(min_length=1)


class Links(Args):
    iid: int = Field(gt=0)
    target_iids: list[int] = Field(min_length=1, max_length=25)
    link_type: Literal["relates_to", "blocks", "is_blocked_by"] = "relates_to"

    @field_validator("target_iids")
    @classmethod
    def positive_ids(cls, values):
        if any(i <= 0 for i in values):
            raise ValueError("卡號必須是正整數")
        return list(dict.fromkeys(values))


class State(Args):
    iid: int = Field(gt=0)
    state: Literal["close", "reopen"] = Field(description="僅在使用者明確要求關閉／重新開啟時使用。")


class Review(Args):
    targets: list[str] = Field(
        min_length=1, max_length=25, description="卡號、完整 Issue 網址或唯一標題；列出全部目標。"
    )


class Query(Args):
    query: str = Field(min_length=1)


class WikiPage(Args):
    slug: str = Field(min_length=1)
    offset: int = Field(0, ge=0)


class Reference(Args):
    url: str
    offset: int = Field(0, ge=0)


class Remember(Args):
    content: str = Field(min_length=1, max_length=500)


class Forget(Args):
    memory_id: int = Field(gt=0, description="明確的本群記憶編號；不確定先列出記憶。")


class Resume(Args):
    operation_id: str = Field(pattern=r"^[a-f0-9]{24}$")


class FunctionTool(Tool):
    def __init__(self, name, description, model, fn):
        self.name, self.description, self.args_model, self.fn = name, description, model, fn

    def spec(self):
        from ...services.llm.base import ToolSpec

        return ToolSpec(self.name, self.description, self.args_model.model_json_schema())

    async def run(self, args, ctx):
        result = await self.fn(args, ctx)
        return wrap_external(json.dumps(result, ensure_ascii=False, default=str))


def mention(member) -> str | None:
    if member.telegram_username:
        return "@" + html.escape(member.telegram_username)
    if member.telegram_id:
        return f'<a href="tg://user?id={member.telegram_id}">{html.escape(member.nickname[:80])}</a>'
    return None


class EditorialTools:
    def __init__(self, gitlab, google, roster, knowledge, workflow, store, settings):
        self.gl, self.google, self.roster, self.knowledge = gitlab, google, roster, knowledge
        self.workflow, self.store, self.settings = workflow, store, settings
        self.reviews = ReviewPackets(store, ReviewDocuments(google), settings)

    async def once(self, kind, payload, ctx, execute, *, repeat_safe=False):
        key = operation_key(ctx.event_id, kind, payload)
        op = await self.store.operation(key, ctx.event_id, ctx.chat_id, ctx.user_id, kind, payload)
        if op["state"] == "done":
            return json.loads(op["result"])
        if op["steps"].get("started") and not repeat_safe:
            raise ValueError(f"前次操作 {key} 結果不明，請先查核實際資料，避免重複新增。")
        await self.store.checkpoint(key, {**op["steps"], "started": True})
        try:
            result = await execute(key)
            await self.store.finish(key, result)
            return result
        except RemoteError as exc:
            latest = await self.store.one("SELECT steps FROM operations WHERE id=?", (key,))
            steps = json.loads(latest["steps"])
            steps["started"] = exc.uncertain
            if not exc.uncertain:
                steps.pop("attempted", None)
            await self.store.checkpoint(key, steps, "uncertain" if exc.uncertain else "error", str(exc))
            raise

    async def members_for_ids(self, ids):
        roster = await self.roster.get()
        members = [roster.by_gitlab(i) for i in ids]
        if any(m is None for m in members):
            raise ValueError("指定 GitLab ID 不在編輯組名冊，請先用 resolve_member 查證。")
        # The real roster currently contains IDs that may belong to another platform; verify, never guess.
        for user_id in ids:
            response = await self.gl.client.get(f"/users/{user_id}")
            if response.is_error:
                raise ValueError(f"名冊 GitLab ID {user_id} 無法在 GitLab 查證，請修正原表或指定其他負責人。")
        return members

    async def resolve_member(self, args, ctx):
        roster = await self.roster.get()
        if args.query.strip() in ("我", "自己", "me"):
            member = roster.by_telegram_id(ctx.user_id)
            matches = [member] if member else roster.search_by_name(ctx.username or "")
        else:
            matches = roster.search_by_name(args.query)
        if len(matches) != 1:
            raise ValueError("無法唯一對應名冊，請提供 Telegram username。")
        from dataclasses import asdict

        return asdict(matches[0])

    async def creator_attribution(self, ctx, roster) -> str:
        member = roster.by_telegram_id(ctx.user_id)
        if not member and ctx.username:
            matches = roster.search_by_name(ctx.username)
            if len(matches) > 1:
                raise ValueError("建立者無法唯一對應名冊，請先修正名冊。")
            member = matches[0] if matches else None
        if member:
            username = await self.gl.user_username(member.gitlab_id)
            if username:
                return f"@{username}"
        if ctx.username:
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", ctx.username):
                raise ValueError("建立者的 Telegram username 格式不正確")
            # Inline code prevents a Telegram handle from pinging an unrelated GitLab user.
            return f"Telegram：`@{ctx.username}`"
        return f"Telegram ID：{ctx.user_id}"

    async def create(self, args, ctx):
        from ...services.dates import validate_explicit_date

        validate_explicit_date(ctx.text, args.due_date, self.settings.tz)
        roster = await self.roster.get()
        ids = args.assignee_ids or [roster.default_member().gitlab_id]
        await self.members_for_ids(ids)
        labels = args.labels or [
            self.settings.default_document_label if args.document else self.settings.default_task_label
        ]
        if not any(s.startswith("Status::") for s in labels):
            labels = [*labels, self.settings.initial_status]
        payload = args.model_dump()
        payload.update(assignee_ids=ids, labels=labels, requester=await self.creator_attribution(ctx, roster))
        result = await self.workflow.create(payload, ctx)
        if not args.assignee_ids:
            result["default_assignment"] = roster.default_member().nickname
        if self.settings.review_status in labels:
            result["review"] = await self.review(Review(targets=[str(result["iid"])]), ctx)
        return result

    async def resume(self, args, ctx):
        op = await self.store.one(
            "SELECT * FROM operations WHERE id=? AND chat_id=? AND user_id=?",
            (args.operation_id, ctx.chat_id, ctx.user_id),
        )
        if not op or op["kind"] != "create_card":
            raise ValueError("找不到你在本群的可接續開卡操作。")
        payload = json.loads(op["payload"])
        await self.members_for_ids(payload["assignee_ids"])
        return await self.workflow.create(payload, ctx, resume_id=op["id"])

    async def get(self, args, ctx):
        issue = await self.gl.get(args.iid)
        result = {"issue": issue, "links": await self.gl.links(args.iid)}
        if args.include_notes:
            result["notes"] = [n for n in await self.gl.notes(args.iid) if not n.get("system")]
        return result

    async def status_label(self, value):
        aliases = {
            "收件匣": "Inbox",
            "待處理": "To Do",
            "待辦": "To Do",
            "進行中": "Doing",
            "處理中": "Doing",
            "送審": "Review",
            "審稿": "Review",
            "等待": "Waiting",
            "回報": "Report",
        }
        raw = aliases.get(value, value.removeprefix("Status::"))
        existing = [r["name"] for r in await self.gl.labels() if r["name"].startswith("Status::")]
        matches = [name for name in existing if name.removeprefix("Status::").casefold() == raw.casefold()]
        if len(matches) != 1:
            raise ValueError(f"不支援狀態「{value}」。可用：{'、'.join(existing)}；關卡請明確說關閉。")
        return matches[0]

    async def search(self, args, ctx):
        labels = list(args.label_filters)
        if args.status:
            labels.append(await self.status_label(args.status))
        await self.gl.validate_labels(labels)
        params = {"state": "opened" if args.open_only else "all"}
        if labels:
            params["labels"] = ",".join(labels)
        if args.assignee_id:
            params["assignee_id"] = args.assignee_id
        if args.title_query:
            params["search"] = args.title_query
        rows = await self.gl.search(**params)
        if args.due_before:
            rows = [i for i in rows if i.get("due_date") and date.fromisoformat(i["due_date"]) <= args.due_before]
        return [
            {k: i.get(k) for k in ("iid", "title", "labels", "state", "due_date", "web_url", "assignees")} for i in rows
        ]

    async def update(self, args, ctx):
        payload = args.model_dump(mode="json")

        async def execute(_key):
            current = await self.gl.get(args.iid)
            fields = {k: payload[k] for k in ("title", "description", "due_date") if payload[k] is not None}
            if args.due_date and args.clear_due_date:
                raise ValueError("不能同時設定與清除到期日")
            if args.clear_due_date:
                fields["due_date"] = ""
            if args.set_assignee_ids is not None:
                await self.members_for_ids(args.set_assignee_ids)
                fields["assignee_ids"] = args.set_assignee_ids
            if args.description is not None:
                old = current.get("description") or ""
                blocks = re.findall(r"<!-- editorial-resources:start -->.*?<!-- editorial-resources:end -->", old, re.S)
                resource = await self.store.one("SELECT * FROM resources WHERE issue_iid=?", (args.iid,))
                if not blocks and resource:
                    links = resource_links(resource["folder_id"], resource["document_id"])
                    if links:
                        blocks = [links]
                if not blocks:
                    blocks = re.findall(
                        r"https://(?:docs\.google\.com/document/d/|drive\.google\.com/[^\s<]*)[^\s<]*", old
                    )
                markers = re.findall(r"<!-- editorial-operation:[a-f0-9]+ -->", old)
                fields["description"] = (
                    args.description + "\n\n" + "\n".join(b for b in blocks + markers if b not in args.description)
                )
            labels = list(args.add_labels)
            status = await self.status_label(args.status) if args.status else None
            review_requested = status == self.settings.review_status or self.settings.review_status in labels
            if review_requested:
                labels = [s for s in labels if s != self.settings.review_status]
            elif status:
                labels.append(status)
            updated = await self.gl.update(args.iid, add_labels=labels, remove_labels=args.remove_labels, **fields)
            if review_requested:
                return await self.review(Review(targets=[str(args.iid)]), ctx)
            return {
                "iid": args.iid,
                "before": current["labels"],
                "after": updated["labels"],
                "url": updated["web_url"],
                "note": "只修改卡片欄位；既有 Drive 名稱與文案正文未更動。",
            }

        return await self.once("update_issue", payload, ctx, execute, repeat_safe=True)

    async def review(self, args, ctx):
        ctx.review_completed = False
        # Resolve every target first, before any writes.
        issues = [await self.gl.resolve(t) for t in args.targets]
        issues = list({i["iid"]: i for i in issues}.values())
        if any(i["state"] != "opened" for i in issues):
            raise ValueError("包含已關閉卡片，請明確重新開啟後再送審。")
        roster = await self.roster.get()
        chiefs = roster.chiefs()
        roles = {role for m in chiefs for role in re.split(r"[、,;/\s]+", m.position)}
        if not {"總召", "副召"}.issubset(roles) or any(not mention(m) for m in chiefs):
            raise ValueError("名冊 note 欄缺少可標註的總召／副召，請先修正名冊。")
        await self.gl.validate_labels([self.settings.review_status], required=True)
        prepared = []
        for issue in issues:
            text = issue.get("description") or ""
            resource = await self.store.one("SELECT * FROM resources WHERE issue_iid=?", (issue["iid"],))
            docs = re.findall(r"https://docs\.google\.com/document/d/[A-Za-z0-9_-]+(?:/edit)?", text)
            if resource:
                if resource["document_id"]:
                    docs = [f"https://docs.google.com/document/d/{resource['document_id']}/edit"]
            if (
                not docs
                and resource is None
                and (self.settings.default_document_label in issue["labels"] or "文案" in issue["title"])
            ):
                raise ValueError(f"#{issue['iid']} 找不到文案連結，請補上再送審。")
            authors = []
            missing = []
            for assignee in issue.get("assignees", []):
                member = roster.by_gitlab(assignee["id"])
                if member and mention(member):
                    authors.append(mention(member))
                else:
                    missing.append(assignee.get("name", str(assignee["id"])))
            who = "、".join(dict.fromkeys(authors)) or "未指定負責人"
            notice = (
                f"這是由 {who} 負責的 #{issue['iid']} {html.escape(issue['title'])} {'文案' if docs else '任務'}，請 "
            )
            notice += "、".join(dict.fromkeys(mention(m) for m in chiefs)) + " 幫忙 review\n"
            links = [("文案", url) for url in docs[:1]]
            links.append(("卡片", issue["web_url"]))
            notice += "\n".join(f"{label}：{html.escape(url)}" for label, url in links)
            if missing:
                notice += "\n無 Telegram 對照，未能標註：" + html.escape("、".join(missing))
            review_id = None
            if docs:
                document_id = re.search(r"/document/d/([A-Za-z0-9_-]+)", docs[0])[1]
                review_id = await self.reviews.prepare(
                    operation_key(ctx.event_id, "review", {"iid": issue["iid"]}), ctx, issue, document_id, notice
                )
            prepared.append((issue, notice, review_id))
        results = []
        for issue, notice, review_id in prepared:

            async def execute(_key, iid=issue["iid"], text=notice, packet_id=review_id):
                await self.gl.update(iid, add_labels=[self.settings.review_status])
                return {"iid": iid, "notification": text, "status": "review", "review_id": packet_id}

            try:
                result = await self.once("review", {"iid": issue["iid"]}, ctx, execute, repeat_safe=True)
                if result["notification"] not in ctx.notices:
                    ctx.notices.append(result["notification"])
                results.append({"iid": issue["iid"], "status": "已改 Review，通知由系統送出"})
            except Exception as exc:
                results.append({"iid": issue["iid"], "error": str(exc)})
        ctx.review_completed = all(packet_id for _, _, packet_id in prepared) and all("error" not in r for r in results)
        return results

    async def tag(self, args, ctx):
        roster = await self.roster.get()
        members = list(dict.fromkeys(mention(m) for m in roster.members if mention(m)))
        missing = [m.nickname for m in roster.members if not mention(m)]
        for i in range(0, len(members), 30):
            notice = "編輯組夥伴：\n" + " ".join(members[i : i + 30])
            if notice not in ctx.notices:
                ctx.notices.append(notice)
        if missing:
            ctx.notices.append("無法標註：" + html.escape("、".join(missing)))
        return {"mentioned": len(members), "missing": missing, "delivery": "由系統送出，勿在一般回覆重複 mention"}

    async def comment(self, args, ctx):
        async def execute(key):
            marker = f"<!-- editorial-action:{key} -->"
            for note in await self.gl.notes(args.iid):
                if marker in note.get("body", ""):
                    return {"note_id": note["id"], "recovered": True}
            op = await self.store.one("SELECT steps FROM operations WHERE id=?", (key,))
            if op and json.loads(op["steps"]).get("attempted"):
                raise ValueError("留言結果仍不明，請先人工查核；不重複送出。")
            await self.store.checkpoint(key, {"started": True, "attempted": True})
            result = await self.gl.comment(args.iid, args.body + "\n\n" + marker)
            return {"note_id": result["id"], "iid": args.iid}

        return await self.once("comment", args.model_dump(), ctx, execute, repeat_safe=True)

    async def links(self, args, ctx, remove=False):
        result = []
        for target in args.target_iids:

            async def execute(_key, target=target):
                if remove:
                    return {"iid": args.iid, "target": target, "removed": await self.gl.unlink(args.iid, target)}
                return await self.gl.link(args.iid, target, args.link_type)

            result.append(
                await self.once(
                    "unlink" if remove else "link",
                    {"iid": args.iid, "target": target, "type": args.link_type},
                    ctx,
                    execute,
                    repeat_safe=True,
                )
            )
        return result

    async def state(self, args, ctx):
        async def execute(_key):
            issue = await self.gl.update(args.iid, state_event=args.state)
            return {"iid": args.iid, "state": issue["state"], "url": issue["web_url"]}

        return await self.once("state", args.model_dump(), ctx, execute, repeat_safe=True)

    async def memories(self, args, ctx):
        return await self.store.all(
            "SELECT id,content,created_by_name,created_at FROM group_memories WHERE chat_id=?", (ctx.chat_id,)
        )

    async def remember(self, args, ctx):
        content = args.content.strip()
        if not content:
            raise ValueError("記憶不可空白")
        memory_id = await self.store.add_memory(ctx.chat_id, ctx.user_id, ctx.username, content)
        return {"id": memory_id, "remembered": content}

    async def forget(self, args, ctx):
        async def execute(_key):
            row = await self.store.one(
                "SELECT * FROM group_memories WHERE chat_id=? AND id=?", (ctx.chat_id, args.memory_id)
            )
            if not row:
                raise ValueError("本群沒有這筆記憶")
            await self.store.execute(
                "DELETE FROM group_memories WHERE chat_id=? AND id=?", (ctx.chat_id, args.memory_id)
            )
            return {"forgotten": row["content"], "id": row["id"]}

        return await self.once("forget", args.model_dump(), ctx, execute, repeat_safe=True)

    def build(self):
        return [
            FunctionTool(
                "resolve_member", "用 Telegram username 或我查詢編輯組名冊身分。", Person, self.resolve_member
            ),
            FunctionTool(
                "create_card",
                "開卡：預設僅建立 GitLab 卡片；明確要求建立文案才建立 MMDD_TITLE 資料夾與範本 Docs。只用既有 label。",
                Create,
                self.create,
            ),
            FunctionTool("resume_operation", "接續自己在本群未完成的開卡操作。", Resume, self.resume),
            FunctionTool("gitlab_get_issue", "讀取卡片、關聯卡及選用人工留言。", Issue, self.get),
            FunctionTool("gitlab_search_issues", "查卡；未關閉包含 Review，支援條件篩選。", Search, self.search),
            FunctionTool(
                "gitlab_update_issue",
                "更新卡片欄位、既有 label、負責人或狀態；Review 自動通知總副召。",
                Update,
                self.update,
            ),
            FunctionTool(
                "gitlab_list_labels",
                "列出編輯組所有既有 label 及說明；不得建立或管理 label 定義。",
                Empty,
                lambda a, c: self.gl.labels(),
            ),
            FunctionTool("gitlab_comment_issue", "使用者明確要求時，在指定卡片留言。", Comment, self.comment),
            FunctionTool("gitlab_link_issues", "連結同專案內的卡片。", Links, self.links),
            FunctionTool(
                "gitlab_unlink_issues",
                "明確指名時解除卡片關聯；不刪除卡片。",
                Links,
                lambda a, c: self.links(a, c, True),
            ),
            FunctionTool(
                "gitlab_set_issue_state", "使用者明確要求時關閉或重新開啟卡片；不刪除卡片。", State, self.state
            ),
            FunctionTool("review_cards", "將所有指定卡片送 Review 並通知名冊總副召。", Review, self.review),
            FunctionTool("mention_editors", "一鍵標註全部名冊成員，包含發指令者。", Empty, self.tag),
            FunctionTool(
                "search_wiki",
                "查編輯組 Wiki 的工作知識與文件連結，附來源。",
                Query,
                lambda a, c: self.knowledge.search(a.query),
            ),
            FunctionTool(
                "read_wiki_page",
                "讀取編輯組 Wiki 頁面，可用 offset 接續。",
                WikiPage,
                lambda a, c: self.knowledge.page(a.slug, a.offset),
            ),
            FunctionTool(
                "read_wiki_document",
                "讀取 Wiki 連結的 Google Docs 工作手冊，可用 offset 接續。",
                Reference,
                lambda a, c: self.knowledge.reference(a.url, a.offset),
            ),
            FunctionTool("memory_list", "列出本群長期記憶與編號。", Empty, self.memories),
            FunctionTool("memory_remember", "使用者明確要求記住時，保存本群偏好或慣例。", Remember, self.remember),
            FunctionTool("memory_forget", "使用者明確指名編號時刪除本群該筆記憶。", Forget, self.forget),
        ]
