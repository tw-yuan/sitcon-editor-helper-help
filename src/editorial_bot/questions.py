"""Short-lived, single-use answers bound to the requester and Telegram message."""

import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


@dataclass
class Question:
    token: str
    result: Any
    chat_id: int
    thread_id: int | None
    user_id: int
    created_at: float
    message_ids: set[int] = field(default_factory=set)
    used: bool = False


@dataclass
class Choice:
    entry: Question
    text: str
    declined: bool


class Questions:
    def __init__(self, ttl_seconds: int, *, clock=time.monotonic):
        self.ttl = ttl_seconds
        self.clock = clock
        self.entries: dict[str, Question] = {}

    def add(self, result, chat_id, thread_id, user_id) -> Question:
        now = self.clock()
        self.entries = {token: q for token, q in self.entries.items() if now - q.created_at < self.ttl}
        while len(self.entries) >= 500:
            self.entries.pop(next(iter(self.entries)))
        entry = Question(secrets.token_urlsafe(12), result, chat_id, thread_id, user_id, now)
        self.entries[entry.token] = entry
        return entry

    def get(self, token) -> Question | None:
        entry = self.entries.get(token)
        if entry and self.clock() - entry.created_at < self.ttl and not entry.used:
            return entry
        return None

    def bind(self, token: str, message_id: int):
        if entry := self.get(token):
            entry.message_ids.add(message_id)

    def markup(self, token: str) -> InlineKeyboardMarkup | None:
        if not (entry := self.get(token)):
            return None
        options = entry.result.pending.options
        labels = options if options == ["同意", "不同意"] else [str(i + 1) for i in range(len(options))]
        buttons = [InlineKeyboardButton(label, callback_data=f"choose:{token}:{i}") for i, label in enumerate(labels)]
        return InlineKeyboardMarkup([buttons[i : i + 3] for i in range(0, len(buttons), 3)])

    def _validate(self, entry, chat_id, thread_id, user_id):
        if entry is None or self.clock() - entry.created_at >= self.ttl:
            raise ValueError("這個問題已過期，請重新提出需求。")
        if entry.user_id != user_id:
            raise ValueError("這個問題只能由原提問者回答。")
        if (entry.chat_id, entry.thread_id) != (chat_id, thread_id):
            raise ValueError("問題的群組或話題不符。")
        if entry.used:
            raise ValueError("這個問題已回答，請勿重複操作。")

    @staticmethod
    def _consume(entry, text) -> Choice:
        # No await between checking and consuming: concurrent answers cannot resume twice.
        entry.used = True
        declined = entry.result.pending.options == ["同意", "不同意"] and text == "不同意"
        return Choice(entry, text, declined)

    def choose(self, data, chat_id, thread_id, user_id, message_id) -> Choice:
        match = re.fullmatch(r"choose:([A-Za-z0-9_-]{16}):([0-9]{1,2})", data or "")
        if not match:
            raise ValueError("無效的選項，請重新提出需求。")
        entry = self.entries.get(match[1])
        self._validate(entry, chat_id, thread_id, user_id)
        if message_id not in entry.message_ids:
            raise ValueError("問題訊息不符。")
        index = int(match[2])
        options = entry.result.pending.options
        if index >= len(options):
            raise ValueError("無效的選項。")
        return self._consume(entry, options[index])

    def answer_text(self, pending, text, chat_id, thread_id, user_id) -> Choice:
        entry = next((q for q in self.entries.values() if q.result.pending is pending), None)
        self._validate(entry, chat_id, thread_id, user_id)
        answer = text.strip()
        options = pending.options
        if re.fullmatch(r"[0-9]{1,2}", answer) and 1 <= int(answer) <= len(options):
            answer = options[int(answer) - 1]
        return self._consume(entry, answer)
