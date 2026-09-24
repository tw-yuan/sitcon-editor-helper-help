from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TimedOut

from editorial_bot.gateway import Gateway, split_plain, triggered
from editorial_bot.storage.db import Store


@pytest.fixture
async def gateway(tmp_path):
    store = await Store.open(str(tmp_path / "db.sqlite3"))
    settings = SimpleNamespace(
        max_concurrent_agent_turns=2, context_ttl_seconds=1800, telegram_admin_id=7, bot_trigger_name="小石"
    )
    result = Gateway(settings, store, AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock())
    yield result
    await store.close()


def update(chat=-1, user=8, text="小石 開卡", kind="supergroup"):
    message = SimpleNamespace(
        text=text, caption=None, reply_to_message=None, message_id=123, message_thread_id=55, reply_text=AsyncMock()
    )
    return SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=chat, type=kind),
        effective_user=SimpleNamespace(id=user, is_bot=False, username="writer"),
        callback_query=None,
        update_id=9,
    )


async def test_unauthorized_and_private_never_invoke_agent(gateway):
    context = SimpleNamespace(bot=SimpleNamespace(username="editorbot", id=99))
    await gateway.handle(update(), context)
    await gateway.handle(update(kind="private"), context)
    gateway.agent.handle.assert_not_awaited()
    assert await gateway.store.all("SELECT * FROM events") == []


async def test_timeout_receipt_prevents_automatic_resend_and_preserves_topic(gateway):
    await gateway.store.execute("INSERT INTO authorized_groups(chat_id,authorized_by) VALUES (-1,7)")
    await gateway.stage("event", -1, 55, 123, [("@writer", "HTML")])
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimedOut()))
    await gateway.deliver(bot)
    await gateway.deliver(bot)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.call_args.kwargs["message_thread_id"] == 55
    assert (await gateway.store.one("SELECT state FROM outbox"))["state"] == "uncertain"


def test_triggers_and_unicode_limits():
    assert triggered("review 123 124", "editorbot", "小石", False)
    assert not triggered("我們今天要 review", "editorbot", "小石", False)
    assert triggered("@editorbot 開卡", "editorbot", "小石", False)
    text = "🙂" * 3000 + "中文"
    chunks = split_plain(text)
    assert "".join(chunks) == text
    assert all(len(s.encode("utf-16-le")) // 2 <= 3800 for s in chunks)
