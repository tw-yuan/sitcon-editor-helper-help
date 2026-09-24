"""Durable PDF snapshots and independent per-reader sign-ins for review notices."""

import asyncio
import html
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

log = logging.getLogger(__name__)


def units(text):
    return len(text.encode("utf-16-le")) // 2


class ReviewPackets:
    def __init__(self, store, documents, settings):
        self.store, self.documents, self.settings = store, documents, settings
        self.document_locks = {}
        self.packet_locks = {}
        self.retry_at = 0.0

    async def prepare(self, key, ctx, issue, document_id, notice):
        if await self.store.one("SELECT id FROM review_packets WHERE id=?", (key,)):
            return key
        if units(notice) > 3000:
            raise ValueError("送審通知過長，請縮短卡名或減少負責人後重試。")
        pdf = await self.documents.export_pdf(document_id)
        title = re.sub(r"[\\/\x00-\x1f]", "_", issue["title"])[:100]
        await self.store.execute(
            "INSERT OR IGNORE INTO review_packets "
            "(id,chat_id,thread_id,issue_iid,document_id,filename,notice,pdf,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                key,
                ctx.chat_id,
                ctx.thread_id,
                issue["iid"],
                document_id,
                f"{issue['iid']}_{title}.pdf",
                notice,
                pdf,
                datetime.now(ZoneInfo(self.settings.tz)).isoformat(timespec="seconds"),
            ),
        )
        return key

    async def get(self, key):
        packet = await self.store.one("SELECT * FROM review_packets WHERE id=?", (key,))
        if packet is None:
            raise ValueError("找不到這份送審紀錄，請重新 review。")
        return packet

    async def bind(self, key, chat, message):
        await self.store.execute(
            "INSERT OR IGNORE INTO review_messages(packet_id,chat_id,message_id) VALUES (?,?,?)", (key, chat, message)
        )

    async def authorize(self, key, chat, message):
        if not re.fullmatch(r"[a-f0-9]{24}", key):
            raise ValueError("無效的送審按鈕。")
        packet = await self.get(key)
        if packet["chat_id"] != chat:
            raise ValueError("請在原群組的送審通知簽到。")
        if not await self.store.one("SELECT 1 FROM authorized_groups WHERE chat_id=?", (chat,)):
            raise ValueError("本群尚未授權。")
        # A Telegram message ID is unique within its chat, including across forum topics.
        # Callback message_thread_id may identify a reply thread, unlike the review request.
        # Bind authorization to the delivered notice, not to that optional metadata.
        if not await self.store.one(
            "SELECT 1 FROM review_messages WHERE packet_id=? AND chat_id=? AND message_id=?", (key, chat, message)
        ):
            raise ValueError("請使用原始送審通知的按鈕。")
        return packet

    async def sign(self, packet, user):
        doc, key = packet["document_id"], packet["id"]
        async with self.document_locks.setdefault(doc, asyncio.Lock()):
            if await self.store.one("SELECT 1 FROM review_reads WHERE packet_id=? AND user_id=?", (key, user.id)):
                async with self.packet_locks.setdefault(key, asyncio.Lock()):
                    await self.store.execute("UPDATE review_messages SET dirty=1 WHERE packet_id=?", (key,))
                return False
            label = f"@{user.username}" if user.username else f"Telegram ID {user.id}"
            if not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}|Telegram ID [0-9]+", label):
                raise ValueError("無法確認你的 Telegram 身分，請稍後再試。")
            # Freeze identity before the external write; retries and username changes keep the same label.
            await self.store.execute(
                "INSERT OR IGNORE INTO document_signatures(document_id,user_id,label) VALUES (?,?,?)",
                (doc, user.id, label),
            )
            identity = await self.store.one(
                "SELECT label FROM document_signatures WHERE document_id=? AND user_id=?", (doc, user.id)
            )
            label = identity["label"]
            await self.documents.sign(doc, label)
            async with self.packet_locks.setdefault(key, asyncio.Lock()), self.store.lock:
                try:
                    await self.store.conn.execute("BEGIN IMMEDIATE")
                    await self.store.conn.execute(
                        "UPDATE document_signatures SET state='done' WHERE document_id=? AND user_id=?", (doc, user.id)
                    )
                    await self.store.conn.execute(
                        "INSERT OR IGNORE INTO review_reads(packet_id,user_id,label) VALUES (?,?,?)",
                        (key, user.id, label),
                    )
                    await self.store.conn.execute("UPDATE review_messages SET dirty=1 WHERE packet_id=?", (key,))
                    await self.store.conn.execute(
                        "INSERT INTO audit_log(chat_id,user_id,action,target,status) VALUES (?,?,?,?,?)",
                        (packet["chat_id"], user.id, "review_sign", str(packet["issue_iid"]), "ok"),
                    )
                    await self.store.conn.commit()
                except Exception:
                    await self.store.conn.rollback()
                    raise
        return True

    async def render(self, packet, *, page=0):
        rows = await self.store.all(
            "SELECT label FROM review_reads WHERE packet_id=? ORDER BY created_at,rowid", (packet["id"],)
        )
        base = packet["notice"] + "\n\nPDF 為送審當下版本；修改文案後請重新 review。\n"
        budget = 3900 - units(base)
        pages, names, size = [], [], 0
        for row in rows:
            label = html.escape(row["label"])
            length = units(label) + 1
            if names and size + length > budget:
                pages.append(names)
                names, size = [], 0
            names.append(label)
            size += length
        pages.append(names)
        page = max(0, min(page, len(pages) - 1))
        text = base + "已看過：" + ("、".join(pages[page]) or "尚無")
        buttons = [[InlineKeyboardButton("簽到", callback_data=f"review_sign:{packet['id']}")]]
        if len(pages) > 1:
            text += f"\n名單第 {page + 1}/{len(pages)} 頁，共 {len(rows)} 人"
            navigation = []
            if page:
                navigation.append(
                    InlineKeyboardButton("上一頁", callback_data=f"review_page:{packet['id']}:{page - 1}")
                )
            if page < len(pages) - 1:
                navigation.append(
                    InlineKeyboardButton("下一頁", callback_data=f"review_page:{packet['id']}:{page + 1}")
                )
            buttons.append(navigation)
        return text, InlineKeyboardMarkup(buttons)

    async def set_page(self, packet, message, page):
        async with self.packet_locks.setdefault(packet["id"], asyncio.Lock()):
            await self.store.execute(
                "UPDATE review_messages SET page=?,dirty=1 WHERE packet_id=? AND chat_id=? AND message_id=?",
                (page, packet["id"], packet["chat_id"], message),
            )

    async def refresh(self, bot, *, packet_id=None):
        if time.monotonic() < self.retry_at:
            return
        sql = "SELECT * FROM review_messages WHERE dirty=1"
        params = ()
        if packet_id:
            sql += " AND packet_id=?"
            params = (packet_id,)
        for row in await self.store.all(sql, params):
            async with self.packet_locks.setdefault(row["packet_id"], asyncio.Lock()):
                if not await self.store.one("SELECT 1 FROM authorized_groups WHERE chat_id=?", (row["chat_id"],)):
                    continue
                current = await self.store.one(
                    "SELECT * FROM review_messages WHERE chat_id=? AND message_id=?",
                    (row["chat_id"], row["message_id"]),
                )
                if current["dirty"] != 1:
                    continue
                text, markup = await self.render(await self.get(row["packet_id"]), page=current["page"])
                dirty = 0
                try:
                    await bot.edit_message_text(
                        chat_id=row["chat_id"],
                        message_id=row["message_id"],
                        text=text,
                        parse_mode="HTML",
                        reply_markup=markup,
                        disable_web_page_preview=True,
                    )
                except RetryAfter as exc:
                    delay = exc.retry_after
                    self.retry_at = time.monotonic() + (
                        delay.total_seconds() if hasattr(delay, "total_seconds") else delay
                    )
                    return
                except (BadRequest, Forbidden) as exc:
                    if not isinstance(exc, BadRequest) or "message is not modified" not in str(exc).lower():
                        dirty = 2
                        log.warning(
                            "Review message edit rejected chat=%s message=%s type=%s",
                            row["chat_id"],
                            row["message_id"],
                            type(exc).__name__,
                        )
                except TelegramError as exc:
                    log.warning(
                        "Review message edit pending chat=%s message=%s type=%s",
                        row["chat_id"],
                        row["message_id"],
                        type(exc).__name__,
                    )
                    continue
                await self.store.execute(
                    "UPDATE review_messages SET dirty=? WHERE chat_id=? AND message_id=?",
                    (dirty, row["chat_id"], row["message_id"]),
                )
