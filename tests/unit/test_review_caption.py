from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from editorial_bot.agent.tools.base import ToolContext
from editorial_bot.gateway import Gateway
from editorial_bot.review_packets import ReviewPackets, units
from editorial_bot.storage.db import Store


async def test_review_sends_pdf_notice_and_button_in_one_message(tmp_path):
    store = await Store.open(str(tmp_path / "db.sqlite3"))
    try:
        await store.execute("INSERT INTO authorized_groups(chat_id,authorized_by) VALUES (-1,7)")
        settings = SimpleNamespace(tz="Asia/Taipei", max_concurrent_agent_turns=4)
        docs = SimpleNamespace(export_pdf=AsyncMock(return_value=b"%PDF-1.7\nhello"), sign=AsyncMock())
        reviews = ReviewPackets(store, docs, settings)
        gateway = Gateway(settings, store, None, None, None, None, reviews=reviews)
        ctx = ToolContext(-1, 55, 7, "author", "review 676", event_id="event")
        notice = (
            "這是由 @Mina430 負責的 #676 教師節&amp;預告10月徵稿 文案，請 @Nathan2045、@yorukot 幫忙 review\n"
            "文案：https://docs.google.com/document/d/document/edit\n"
            "卡片：https://gitlab.com/sitcon-tw/editorial/board/-/work_items/676"
        )
        key = await reviews.prepare("a" * 24, ctx, {"iid": 676, "title": "教師節"}, "document", notice)
        await store.claim_event("event", -1, 55, 7, 123)
        await gateway.stage(
            "event", -1, 55, 123, gateway.notice_messages([notice], [{"notification": notice, "review_id": key}])
        )
        bot = SimpleNamespace(
            send_document=AsyncMock(return_value=SimpleNamespace(message_id=455)),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=456)),
            edit_message_caption=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        await gateway.deliver(bot)
        await gateway.deliver(bot)
        bot.send_document.assert_awaited_once()
        bot.send_message.assert_not_awaited()
        sent = bot.send_document.call_args.kwargs
        assert sent["caption"] == notice + "\n\n已看過："
        assert sent["parse_mode"] == "HTML"
        assert sent["message_thread_id"] == 55
        assert sent["reply_parameters"].message_id == 123
        assert sent["reply_markup"].inline_keyboard[0][0].callback_data == f"review_sign:{key}"
        packet = await reviews.authorize(key, -1, 455)
        await reviews.sign(packet, SimpleNamespace(id=8, username="reader_one"))
        await reviews.refresh(bot)
        assert bot.edit_message_caption.call_args.kwargs["caption"] == notice + "\n\n已看過：@reader_one"
        assert bot.edit_message_caption.call_args.kwargs["message_id"] == 455
        bot.edit_message_text.assert_not_awaited()
    finally:
        await store.close()


@pytest.mark.parametrize("notice", ["字" * 901, "😀" * 451], ids=["chinese", "emoji"])
async def test_oversized_caption_is_rejected_before_export(tmp_path, notice):
    store = await Store.open(str(tmp_path / "db.sqlite3"))
    try:
        docs = SimpleNamespace(export_pdf=AsyncMock(return_value=b"%PDF-1.7\nhello"))
        reviews = ReviewPackets(store, docs, SimpleNamespace(tz="Asia/Taipei"))
        with pytest.raises(ValueError, match="送審通知過長"):
            await reviews.prepare(
                "a" * 24, ToolContext(-1, 55, 7, None, "review"), {"iid": 1, "title": "文案"}, "doc", notice
            )
        docs.export_pdf.assert_not_awaited()
    finally:
        await store.close()


async def test_caption_reader_pages_fit_even_with_long_notice(tmp_path):
    store = await Store.open(str(tmp_path / "db.sqlite3"))
    try:
        reviews = ReviewPackets(store, None, None)
        # Escaped text and mentions count by their displayed length, including UTF-16 emoji units.
        notice = "😀" * 400 + "&amp;" * 70 + '<a href="tg://user?id=8">' + "名" * 30 + "</a>"
        packet = {"id": "a" * 24, "notice": notice}
        labels = ["@" + "x" * 29 + f"{i:02}" for i in range(40)]
        for i, label in enumerate(labels):
            await store.execute(
                "INSERT INTO review_reads(packet_id,user_id,label) VALUES (?,?,?)", (packet["id"], i, label)
            )
        shown = []
        page = 0
        while True:
            text, markup = await reviews.render(packet, page=page)
            # The notice is exactly 900 UTF-16 units after HTML parsing.
            assert 900 + units(text[len(notice) :]) <= 1024
            shown.extend(label for label in labels if label in text)
            if not any(button.text == "下一頁" for row in markup.inline_keyboard for button in row):
                break
            page += 1
        assert shown == labels
        assert page > 0
    finally:
        await store.close()
