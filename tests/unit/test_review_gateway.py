import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.error import TimedOut

from editorial_bot.agent.tools.base import ToolContext
from editorial_bot.gateway import Gateway
from editorial_bot.review_packets import ReviewPackets
from editorial_bot.storage.db import Store


@pytest.fixture
async def setup(tmp_path):
    store = await Store.open(str(tmp_path / "db.sqlite3"))
    settings = SimpleNamespace(
        max_concurrent_agent_turns=4,
        context_ttl_seconds=1800,
        telegram_admin_id=7,
        bot_trigger_name="小石",
        tz="Asia/Taipei",
    )
    docs = SimpleNamespace(export_pdf=AsyncMock(return_value=b"%PDF-1.7\nhello"), sign=AsyncMock())
    reviews = ReviewPackets(store, docs, settings)
    gateway = Gateway(settings, store, AsyncMock(), AsyncMock(), None, None, reviews=reviews)
    await store.execute("INSERT INTO authorized_groups(chat_id,authorized_by) VALUES (-1,7)")
    ctx = ToolContext(-1, 55, 7, "author", "review 678", event_id="event")
    key = "a" * 24
    notice = "送審通知\n文案：https://docs.google.com/document/d/doc/edit\n卡片：https://gitlab.com/p/-/issues/678"
    await reviews.prepare(key, ctx, {"iid": 678, "title": "文章"}, "doc", notice)
    receipt = {"notification": notice, "review_id": key}
    await store.claim_event("event", -1, 55, 7, 123)
    await gateway.stage("event", -1, 55, 123, gateway.notice_messages([notice], [receipt]))
    bot = SimpleNamespace(
        id=99,
        username="editorbot",
        send_document=AsyncMock(return_value=SimpleNamespace(message_id=455)),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=456)),
        edit_message_text=AsyncMock(),
        edit_message_caption=AsyncMock(),
        send_chat_action=AsyncMock(),
        set_message_reaction=AsyncMock(),
    )
    yield gateway, reviews, bot, key
    await store.close()


def click(key, user=8, topic=55, message=455, chat=-1):
    return SimpleNamespace(
        update_id=10 + user,
        effective_message=SimpleNamespace(
            text="notification", caption=None, message_id=message, message_thread_id=topic
        ),
        effective_chat=SimpleNamespace(id=chat, type="supergroup"),
        effective_user=SimpleNamespace(id=user, username=f"reader_{user}", is_bot=False),
        callback_query=SimpleNamespace(data=f"review_sign:{key}", answer=AsyncMock()),
    )


async def test_pdf_and_sign_in_notice_send_once_in_same_topic(setup):
    gateway, reviews, bot, key = setup
    await gateway.deliver(bot)
    await gateway.deliver(bot)
    bot.send_document.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    assert bot.send_document.call_args.kwargs["message_thread_id"] == 55
    assert bot.send_document.call_args.kwargs["document"].input_file_content.startswith(b"%PDF-")
    assert (
        bot.send_document.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == f"review_sign:{key}"
    )
    assert bot.send_document.call_args.kwargs["caption"] == (await reviews.get(key))["notice"] + "\n\n已看過："
    await gateway.handle(click(key, 8), SimpleNamespace(bot=bot))
    await gateway.handle(click(key, 9), SimpleNamespace(bot=bot))
    await gateway.handle(click(key, 8), SimpleNamespace(bot=bot))
    assert "已看過：@reader_8、@reader_9" in bot.edit_message_caption.call_args.kwargs["caption"]
    assert bot.edit_message_caption.call_args.kwargs["message_id"] == 455
    assert reviews.documents.sign.await_count == 2
    gateway.agent.handle.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    bot.set_message_reaction.assert_not_awaited()


async def test_uncertain_pdf_delivery_does_not_send_sign_in_notice_or_resend_pdf(setup):
    gateway, _reviews, bot, _key = setup
    bot.send_document.side_effect = TimedOut()
    await gateway.deliver(bot)
    await gateway.deliver(bot)
    bot.send_document.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    assert (await gateway.store.one("SELECT state FROM outbox ORDER BY id LIMIT 1"))["state"] == "uncertain"


# Forwarding into another topic creates a new message ID within the same chat.
@pytest.mark.parametrize("topic,message,chat", [(56, 457, -1), (55, 999, -1), (55, 456, -2)])
async def test_callbacks_cannot_sign_other_chat_topic_or_message(setup, topic, message, chat):
    gateway, reviews, bot, key = setup
    await gateway.deliver(bot)
    await gateway.handle(click(key, topic=topic, message=message, chat=chat), SimpleNamespace(bot=bot))
    reviews.documents.sign.assert_not_awaited()
    gateway.agent.handle.assert_not_awaited()


async def test_restart_can_still_sign_existing_notice(setup):
    gateway, reviews, bot, key = setup
    await gateway.deliver(bot)
    restarted = ReviewPackets(gateway.store, reviews.documents, gateway.settings)
    gateway.reviews = restarted
    await gateway.recover(bot)
    await gateway.handle(click(key), SimpleNamespace(bot=bot))
    assert "已看過：@reader_8" in bot.edit_message_caption.call_args.kwargs["caption"]
    bot.send_document.assert_awaited_once()


async def test_simultaneous_readers_do_not_overwrite_notice_names(setup):
    gateway, _reviews, bot, key = setup
    await gateway.deliver(bot)
    await asyncio.gather(*[gateway.handle(click(key, user=i), SimpleNamespace(bot=bot)) for i in (8, 9, 10)])
    text = bot.edit_message_caption.call_args.kwargs["caption"]
    assert all(f"@reader_{i}" in text for i in (8, 9, 10))


async def test_crash_after_sent_receipt_recovers_button_binding(setup):
    gateway, _reviews, bot, key = setup
    await gateway.deliver(bot)
    await gateway.store.execute("DELETE FROM review_messages")
    await gateway.recover(bot)
    await gateway.handle(click(key), SimpleNamespace(bot=bot))
    assert "已看過：@reader_8" in bot.edit_message_caption.call_args.kwargs["caption"]
    bot.send_document.assert_awaited_once()


async def test_google_signature_failure_reports_error_without_false_read_receipt(setup):
    gateway, reviews, bot, key = setup
    await gateway.deliver(bot)
    reviews.documents.sign.side_effect = ValueError("找不到簽到欄位")
    await gateway.handle(click(key), SimpleNamespace(bot=bot))
    assert "簽到尚未完成" in bot.send_message.call_args.kwargs["text"]
    assert await gateway.store.all("SELECT * FROM review_reads") == []
    bot.edit_message_caption.assert_not_awaited()


async def test_pdf_review_receipt_is_recovered_after_event_crash(setup):
    gateway, _reviews, bot, key = setup
    packet = await gateway.reviews.get(key)
    await gateway.store.execute("DELETE FROM outbox")
    await gateway.store.execute("UPDATE events SET state='running' WHERE id='event'")
    await gateway.store.operation(key, "event", -1, 7, "review", {"iid": 678})
    await gateway.store.finish(key, {"iid": 678, "notification": packet["notice"], "review_id": key})
    await gateway.recover(bot)
    bot.send_document.assert_awaited_once()
    assert bot.send_document.call_args.kwargs["caption"] == packet["notice"] + "\n\n已看過："
    assert not any(c.kwargs.get("reply_markup") for c in bot.send_message.call_args_list)
    await gateway.handle(click(key), SimpleNamespace(bot=bot))
    assert "已看過：@reader_8" in bot.edit_message_caption.call_args.kwargs["caption"]


@pytest.mark.parametrize("stored_thread,callback_thread", [(None, 123), (123, 456), (55, None)])
@pytest.mark.parametrize("action", ["sign", "page"])
async def test_original_notice_callback_accepts_reply_thread_metadata(setup, stored_thread, callback_thread, action):
    gateway, reviews, bot, key = setup
    await gateway.store.execute("UPDATE review_packets SET thread_id=? WHERE id=?", (stored_thread, key))
    await gateway.deliver(bot)
    # Telegram can attach a reply thread to the bot notice although the request had no topic.
    message = {
        "message_id": 455,
        "date": 1,
        "chat": {"id": -1, "type": "supergroup"},
        "caption": "送審通知",
    }
    if callback_thread is not None:
        message["message_thread_id"] = callback_thread
    callback_data = f"review_{action}:{key}" + (":0" if action == "page" else "")
    update = Update.de_json(
        {
            "update_id": 12345,
            "callback_query": {
                "id": "original-notice-click",
                "chat_instance": "original-chat",
                "from": {"id": 8, "is_bot": False, "first_name": "Reader", "username": "reader_8"},
                "message": message,
                "data": callback_data,
            },
        },
        bot,
    )
    bot.answer_callback_query = AsyncMock()
    await gateway.handle(update, SimpleNamespace(bot=bot))
    assert not bot.answer_callback_query.call_args.kwargs.get("show_alert")
    assert bot.edit_message_caption.call_args.kwargs["message_id"] == 455
    if action == "sign":
        reviews.documents.sign.assert_awaited_once_with("doc", "@reader_8")
        assert "已看過：@reader_8" in bot.edit_message_caption.call_args.kwargs["caption"]
    else:
        reviews.documents.sign.assert_not_awaited()
    gateway.agent.handle.assert_not_awaited()
    bot.send_message.assert_not_awaited()


async def test_legacy_text_notice_recovers_and_still_updates_text(setup):
    gateway, reviews, bot, key = setup
    await gateway.store.execute("DELETE FROM outbox")
    await gateway.stage(
        "event",
        -1,
        55,
        123,
        [{"kind": "review_pdf", "review_id": key}, {"kind": "review_notice", "review_id": key}],
    )
    await gateway.deliver(bot)
    await gateway.store.execute("DELETE FROM review_messages")
    await gateway.recover(bot)
    await gateway.handle(click(key, message=456), SimpleNamespace(bot=bot))
    reviews.documents.sign.assert_awaited_once_with("doc", "@reader_8")
    assert "已看過：@reader_8" in bot.edit_message_text.call_args.kwargs["text"]
    assert bot.edit_message_text.call_args.kwargs["message_id"] == 456
    bot.edit_message_caption.assert_not_awaited()
    bot.send_document.assert_awaited_once()
    bot.send_message.assert_awaited_once()


async def test_caption_edit_timeout_retries_without_resending_pdf(setup):
    gateway, reviews, bot, key = setup
    await gateway.deliver(bot)
    bot.edit_message_caption.side_effect = TimedOut()
    await gateway.handle(click(key), SimpleNamespace(bot=bot))
    assert (await gateway.store.one("SELECT dirty FROM review_messages"))["dirty"] == 1
    bot.edit_message_caption.side_effect = None
    await reviews.refresh(bot)
    assert (await gateway.store.one("SELECT dirty FROM review_messages"))["dirty"] == 0
    reviews.documents.sign.assert_awaited_once()
    bot.send_document.assert_awaited_once()
    bot.send_message.assert_not_awaited()
