import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TimedOut

from editorial_bot.agent.tools.base import ToolContext
from editorial_bot.review_packets import ReviewPackets
from editorial_bot.storage.db import Store


@pytest.fixture
async def reviews(tmp_path):
    store = await Store.open(str(tmp_path / "db.sqlite3"))
    await store.execute("INSERT INTO authorized_groups(chat_id,authorized_by) VALUES (-1,7)")
    docs = SimpleNamespace(export_pdf=AsyncMock(return_value=b"%PDF-1.7\nreview"), sign=AsyncMock())
    service = ReviewPackets(store, docs, SimpleNamespace(tz="Asia/Taipei"))
    yield service
    await store.close()


async def packet(reviews, key="a" * 24):
    ctx = ToolContext(-1, 55, 7, "author", "review 678", event_id="event")
    await reviews.prepare(
        key,
        ctx,
        {"iid": 678, "title": "文案"},
        "document",
        "通知\n文案：https://docs.google.com/document/d/document/edit",
    )
    message_id = 456 if key == "a" * 24 else 457  # Separate reviews are distinct Telegram messages.
    await reviews.bind(key, -1, message_id)
    return await reviews.authorize(key, -1, 55, message_id)


def person(user_id, username):
    return SimpleNamespace(id=user_id, username=username)


async def test_pdf_snapshot_and_signatures_are_deduplicated_per_person(reviews):
    row = await packet(reviews)
    await packet(reviews)
    reviews.documents.export_pdf.assert_awaited_once()
    assert await reviews.sign(row, person(8, "reader_one"))
    assert not await reviews.sign(row, person(8, "reader_one"))
    assert await reviews.sign(row, person(9, "reader_two"))
    text, markup = await reviews.render(row)
    assert "已看過：@reader_one、@reader_two" in text
    assert markup.inline_keyboard[0][0].callback_data == "review_sign:" + row["id"]
    assert reviews.documents.sign.await_count == 2
    bot = SimpleNamespace(edit_message_text=AsyncMock())
    await reviews.refresh(bot)
    assert bot.edit_message_text.call_args.kwargs["message_id"] == 456
    assert (await reviews.store.one("SELECT dirty FROM review_messages"))["dirty"] == 0


async def test_concurrent_clicks_and_restart_keep_every_reader(reviews):
    row = await packet(reviews)
    await asyncio.gather(*[reviews.sign(row, person(i, f"reader_{i}")) for i in (8, 9, 8, 10)])
    assert len(await reviews.store.all("SELECT * FROM review_reads")) == 3
    restarted = ReviewPackets(reviews.store, reviews.documents, reviews.settings)
    assert not await restarted.sign(row, person(8, "new_username"))
    text, _ = await restarted.render(row)
    assert all(f"@reader_{i}" in text for i in (8, 9, 10))


async def test_google_failure_keeps_message_unsigned_and_can_retry(reviews):
    row = await packet(reviews)
    reviews.documents.sign.side_effect = ValueError("文件暫時無法簽到")
    with pytest.raises(ValueError):
        await reviews.sign(row, person(8, "reader_one"))
    assert await reviews.store.all("SELECT * FROM review_reads") == []
    reviews.documents.sign.side_effect = None
    assert await reviews.sign(row, person(8, "changed_name"))
    assert reviews.documents.sign.call_args.args == ("document", "@reader_one")


async def test_telegram_failure_retries_edit_without_resigning_document(reviews):
    row = await packet(reviews)
    await reviews.sign(row, person(8, None))
    bot = SimpleNamespace(edit_message_text=AsyncMock(side_effect=TimedOut()))
    await reviews.refresh(bot)
    assert (await reviews.store.one("SELECT dirty FROM review_messages"))["dirty"] == 1
    bot.edit_message_text.side_effect = None
    await reviews.refresh(bot)
    reviews.documents.sign.assert_awaited_once_with("document", "Telegram ID 8")
    assert "已看過：Telegram ID 8" in bot.edit_message_text.call_args.kwargs["text"]


@pytest.mark.parametrize("chat,thread,message", [(-2, 55, 456), (-1, 56, 456), (-1, 55, 457)])
async def test_forged_or_forwarded_button_is_rejected(reviews, chat, thread, message):
    row = await packet(reviews)
    with pytest.raises(ValueError):
        await reviews.authorize(row["id"], chat, thread, message)
    reviews.documents.sign.assert_not_awaited()


async def test_new_review_has_new_pdf_and_reader_list_but_keeps_document_identity(reviews):
    first = await packet(reviews)
    await reviews.sign(first, person(8, "reader_one"))
    second = await packet(reviews, "b" * 24)
    text, _ = await reviews.render(second)
    assert "已看過：尚無" in text
    await reviews.sign(second, person(8, "changed_username"))
    assert reviews.documents.sign.call_args.args == ("document", "@reader_one")
    assert reviews.documents.export_pdf.await_count == 2


async def test_large_reader_list_is_paginated_without_losing_names(reviews):
    row = await packet(reviews)
    for i in range(200):
        await reviews.store.execute(
            "INSERT INTO review_reads(packet_id,user_id,label) VALUES (?,?,?)", (row["id"], i, "@" + "x" * 25 + str(i))
        )
    page = 0
    shown = []
    while True:
        text, markup = await reviews.render(row, page=page)
        assert len(text.encode("utf-16-le")) // 2 <= 4096
        shown.append(text)
        next_button = next((b for buttons in markup.inline_keyboard for b in buttons if b.text == "下一頁"), None)
        if next_button is None:
            break
        page += 1
    combined = "\n".join(shown)
    assert page > 0
    for i in range(200):
        assert "@" + "x" * 25 + str(i) in combined
