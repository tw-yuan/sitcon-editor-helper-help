"""Telegram authorization, reply chains and durable outbound notifications."""

import asyncio
import json
import logging
import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

from .agent.context import trim_history
from .agent.core import AgentRequest, AgentResult
from .agent.tools.base import ToolContext
from .agent.tools.editorial import Empty
from .feedback import REACT_DONE, progress, set_reaction
from .logging_setup import redact

log = logging.getLogger(__name__)
HELP = """我是編輯組小石。可以 @我、叫「小石」，或回覆我的訊息。

• /ta：標註全部編輯組員
• 小石，開文案卡「報名開跑」，到期日 10/15
• 小石，列出尚未關閉的卡片
• 小石，把 #123 指派給 @username
• review #123 #124：送審並標註總副召
• 小石，把 #123 改為 Doing
• 小石，記住本群文案習慣使用全形標點
• 小石，列出本群記憶

管理員：/authorize 授權本群、/revoke 撤銷、/reload 更新名冊與 Wiki。
操作未完成可告訴我「接續操作 ID」。/delivery 查看本群未送達通知。
/resend 編號：管理員人工確認未送達後，重送該通知（可能重複）。"""


def triggered(text: str, username: str, name: str, reply_to_bot: bool) -> bool:
    return bool(
        reply_to_bot
        or name in text
        or re.search(rf"@{re.escape(username)}\b", text, re.I)
        or re.match(r"^review(?:\s|$)", text, re.I)
    )


def split_plain(text: str, limit: int = 3800) -> list[str]:
    """Bound Telegram's UTF-16 length without splitting a Unicode code point."""
    chunks, chars, size = [], [], 0
    for char in text:
        length = len(char.encode("utf-16-le")) // 2
        if size + length > limit:
            chunks.append("".join(chars))
            chars, size = [], 0
        chars.append(char)
        size += length
    if chars:
        chunks.append("".join(chars))
    return chunks


class Gateway:
    def __init__(self, settings, store, agent, editorial, roster, knowledge):
        self.settings, self.store, self.agent = settings, store, agent
        self.editorial, self.roster, self.knowledge = editorial, roster, knowledge
        self.retry_at = 0.0
        self.sessions = {}
        self.locks = {}
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_agent_turns)

    async def allowed(self, chat):
        return chat.type in ("group", "supergroup") and bool(
            await self.store.one("SELECT 1 FROM authorized_groups WHERE chat_id=?", (chat.id,))
        )

    async def stage(
        self, event, chat, thread, reply_to, messages, *, completion_reaction=None, reaction_message_id=None
    ):
        async with self.store.lock:
            try:
                await self.store.conn.execute("BEGIN IMMEDIATE")
                for ordinal, (text, mode) in enumerate(messages):
                    await self.store.conn.execute(
                        "INSERT OR IGNORE INTO outbox(event_id,ordinal,chat_id,thread_id,reply_to,body) "
                        "VALUES (?,?,?,?,?,?)",
                        (
                            event,
                            ordinal,
                            chat,
                            thread,
                            reply_to,
                            json.dumps(
                                {
                                    "text": text,
                                    "parse_mode": mode,
                                    "completion_reaction": completion_reaction,
                                    "reaction_message_id": reaction_message_id,
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    )
                await self.store.conn.execute(
                    "UPDATE events SET state='queued',updated_at=CURRENT_TIMESTAMP WHERE id=?", (event,)
                )
                await self.store.conn.commit()
            except Exception:
                await self.store.conn.rollback()
                raise

    async def deliver(self, bot, *, event=None, row_id=None):
        if time.monotonic() < self.retry_at:
            return []
        sql = "SELECT * FROM outbox WHERE state='pending'"
        params = ()
        if event is not None:
            sql += " AND event_id=?"
            params = (event,)
        if row_id is not None:
            sql += " AND id=?"
            params = (row_id,)
        rows = await self.store.all(sql + " ORDER BY id", params)
        sent = []
        for row in rows:
            if not await self.store.one("SELECT 1 FROM authorized_groups WHERE chat_id=?", (row["chat_id"],)):
                continue
            # Mark before sending. A crash/timeout is ambiguous: never blindly resend.
            if not await self.store.claim_delivery(row["id"]):
                continue
            body = json.loads(row["body"])
            try:
                message = await bot.send_message(
                    chat_id=row["chat_id"],
                    message_thread_id=row["thread_id"],
                    reply_parameters=ReplyParameters(row["reply_to"], allow_sending_without_reply=True)
                    if row["reply_to"]
                    else None,
                    text=body["text"],
                    parse_mode=body["parse_mode"],
                    disable_web_page_preview=True,
                )
            except RetryAfter as exc:
                delay = exc.retry_after
                self.retry_at = time.monotonic() + (delay.total_seconds() if hasattr(delay, "total_seconds") else delay)
                await self.store.execute("UPDATE outbox SET state='pending' WHERE id=?", (row["id"],))
                log.warning("Telegram rate limited; notification id=%s remains pending", row["id"])
                break
            except (BadRequest, Forbidden) as exc:
                await self.store.execute("UPDATE outbox SET state='failed' WHERE id=?", (row["id"],))
                log.warning("Notification rejected id=%s type=%s", row["id"], type(exc).__name__)
            except NetworkError:
                await self.store.execute("UPDATE outbox SET state='uncertain' WHERE id=?", (row["id"],))
                log.warning("Notification delivery uncertain id=%s", row["id"])
            else:
                await self.store.execute(
                    "UPDATE outbox SET state='sent',telegram_message_id=? WHERE id=?", (message.message_id, row["id"])
                )
                sent.append(message.message_id)
                if body.get("completion_reaction") and body.get("reaction_message_id") is not None:
                    remaining = await self.store.one(
                        "SELECT 1 FROM outbox WHERE event_id=? AND state!='sent' LIMIT 1", (row["event_id"],)
                    )
                    if remaining is None:
                        await set_reaction(
                            bot, row["chat_id"], body["reaction_message_id"], body["completion_reaction"]
                        )
        return sent

    async def recover(self, bot):
        await self.store.execute("UPDATE outbox SET state='uncertain' WHERE state='sending'")
        for event in await self.store.all("SELECT * FROM events WHERE state='running'"):
            ops = await self.store.all("SELECT id,kind,state,result FROM operations WHERE event_id=?", (event["id"],))
            messages = []
            for op in ops:
                result = json.loads(op["result"]) if op["result"] else {}
                if isinstance(result, dict) and result.get("notification"):
                    messages.append((result["notification"], "HTML"))
            summary = "前次處理途中重新啟動，請查核以下紀錄後接續，避免重複操作。\n"
            for op in ops:
                summary += f"{op['id']}：{op['kind']}／{op['state']}\n"
                if op["kind"] == "create_card" and op["state"] == "done":
                    result = json.loads(op["result"])
                    summary += (
                        "\n".join(result[k] for k in ("issue_url", "folder_url", "document_url") if result.get(k))
                        + "\n"
                    )
            messages.extend((chunk, None) for chunk in split_plain(summary))
            await self.stage(event["id"], event["chat_id"], event["thread_id"], event["message_id"], messages)
        await self.deliver(bot)

    def conversation(self, chat, message, user, thread):
        now = time.monotonic()
        self.sessions = {k: v for k, v in self.sessions.items() if now - v[0] < self.settings.context_ttl_seconds}
        entry = self.sessions.get((chat, message))
        if entry and entry[1:3] == (user, thread):
            return entry[3]
        return None

    async def handle(self, update, context):
        message, chat, user = update.effective_message, update.effective_chat, update.effective_user
        if not message or not chat or not user or user.is_bot:
            return
        text = message.text or message.caption or ""
        callback = update.callback_query
        bot_username = context.bot.username
        command_match = re.match(r"^/(\w+)(?:@([A-Za-z0-9_]+))?(?:\s|$)", text)
        if command_match and command_match[2] and command_match[2].casefold() != bot_username.casefold():
            return
        command = command_match[1].lower() if command_match else ""
        admin = user.id == self.settings.telegram_admin_id
        if command == "authorize" and admin and chat.type in ("group", "supergroup"):
            await self.store.execute(
                "INSERT OR REPLACE INTO authorized_groups(chat_id,title,authorized_by) VALUES (?,?,?)",
                (chat.id, chat.title or "", user.id),
            )
            await message.reply_text("本群已授權。使用 /help 查看操作。")
            return
        if not await self.allowed(chat):
            return
        if callback:
            if callback.data != "mention_editors":
                return
            await callback.answer()
            command = "ta"
        reply = message.reply_to_message
        reply_to_bot = bool(reply and reply.from_user and reply.from_user.id == context.bot.id)
        if not command and not triggered(text, bot_username, self.settings.bot_trigger_name, reply_to_bot):
            return
        if command in ("revoke", "reload", "resend") and not admin:
            await message.reply_text("這個指令限設定的管理員使用。")
            return
        if command == "revoke":
            await self.store.execute("DELETE FROM authorized_groups WHERE chat_id=?", (chat.id,))
            await message.reply_text("已撤銷本群授權。")
            return
        if command == "reload":
            async with progress(context.bot, chat.id, message.message_thread_id, message.message_id):
                await self.roster.reload()
                await self.knowledge.refresh(force=True)
                await message.reply_text("名冊及 Wiki 快取已更新。")
            await set_reaction(context.bot, chat.id, message.message_id, REACT_DONE)
            return
        if command in ("help", "start"):
            await message.reply_text(
                HELP,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("@ 所有編輯組員", callback_data="mention_editors")]]
                ),
            )
            return
        if command == "delivery":
            rows = await self.store.all(
                "SELECT id,state FROM outbox WHERE chat_id=? AND state!='sent' ORDER BY id", (chat.id,)
            )
            await message.reply_text("\n".join(f"{r['id']}：{r['state']}" for r in rows) or "本群沒有未送達通知。")
            return
        if command == "resend":
            value = text.split(maxsplit=1)[-1]
            if not value.isdecimal():
                await message.reply_text("請使用 /resend 通知編號。先確認未送達，再重送。")
                return
            await self.store.execute(
                "UPDATE outbox SET state='pending' WHERE id=? AND chat_id=? AND state!='sent'", (int(value), chat.id)
            )
            await self.deliver(context.bot, row_id=int(value))
            return
        if command and command != "ta":
            return
        event = f"telegram:{update.update_id}"
        thread = message.message_thread_id
        if not await self.store.claim_event(event, chat.id, thread, user.id, message.message_id):
            return
        # Feedback starts while queued; callbacks have no new user message to react to.
        reaction_message_id = None if callback else message.message_id
        async with (
            progress(context.bot, chat.id, thread, reaction_message_id),
            self.locks.setdefault((chat.id, thread, user.id), asyncio.Lock()),
            self.semaphore,
        ):
            previous = self.conversation(chat.id, reply.message_id, user.id, thread) if reply_to_bot else None
            if previous and previous.pending:
                self.sessions.pop((chat.id, reply.message_id), None)
            try:
                if command == "ta":
                    ctx = ToolContext(chat.id, thread, user.id, user.username, text, event_id=event)
                    await self.editorial.tag(Empty(), ctx)
                    result = AgentResult("", notices=ctx.notices, action="mention_editors")
                else:
                    result = await self.agent.handle(
                        AgentRequest(
                            chat.id,
                            thread,
                            user.id,
                            user.username,
                            text,
                            event_id=event,
                            resume=previous.pending if previous else None,
                            history=previous.history if previous and not previous.pending else None,
                            reply_context=(reply.text or reply.caption) if reply and not previous else None,
                        )
                    )
            except Exception as exc:
                log.exception("Update failed event=%s chat=%s", event, chat.id)
                result = AgentResult(f"目前無法完成：{redact(exc)}", status="error")
            # Saved review receipts survive an LLM failure after the successful status update.
            rows = await self.store.all(
                "SELECT result FROM operations WHERE event_id=? AND kind='review' AND state='done'", (event,)
            )
            notices = list(dict.fromkeys([*result.notices, *[json.loads(r["result"])["notification"] for r in rows]]))
            messages = [(notice, "HTML") for notice in notices]
            messages.extend((chunk, None) for chunk in split_plain(redact(result.reply)))
            completion_reaction = None
            if result.status == "ok":
                completion_reaction = result.reaction or REACT_DONE
            elif result.status == "clarify":
                completion_reaction = result.reaction
            await self.stage(
                event,
                chat.id,
                thread,
                message.message_id,
                messages,
                completion_reaction=completion_reaction,
                reaction_message_id=reaction_message_id,
            )
            await self.store.execute(
                "INSERT INTO audit_log(chat_id,user_id,action,status,detail) VALUES (?,?,?,?,?)",
                (chat.id, user.id, result.action, result.status, json.dumps(result.detail, ensure_ascii=False)),
            )
            sent = await self.deliver(context.bot, event=event)
            for mid in sent:
                if result.history:
                    result.history = trim_history(result.history)
                self.sessions[(chat.id, mid)] = (time.monotonic(), user.id, thread, result)
            while len(self.sessions) > 500:
                self.sessions.pop(next(iter(self.sessions)))

    async def delivery_worker(self, bot):
        while True:
            await asyncio.sleep(15)
            try:
                await self.deliver(bot)
            except Exception:
                log.exception("Pending notification delivery failed")
