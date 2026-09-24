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
    context = SimpleNamespace(bot=feedback_bot())
    await gateway.handle(update(), context)
    await gateway.handle(update(kind="private"), context)
    gateway.agent.handle.assert_not_awaited()
    assert await gateway.store.all("SELECT * FROM events") == []
    context.bot.set_message_reaction.assert_not_awaited()
    context.bot.send_chat_action.assert_not_awaited()


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


def feedback_bot():
    return SimpleNamespace(
        username="editorbot",
        id=99,
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=456)),
        set_message_reaction=AsyncMock(),
        send_chat_action=AsyncMock(),
    )


@pytest.mark.parametrize(
    "status,heart,expected",
    [
        ("ok", None, ["👀", "👍"]),
        ("ok", "❤", ["👀", "❤"]),
        ("clarify", None, ["👀"]),
        ("clarify", "❤", ["👀", "❤"]),
        ("error", None, ["👀"]),
    ],
)
async def test_reactions_follow_delivery_and_deduplicate_updates(gateway, status, heart, expected):
    from editorial_bot.agent.core import AgentResult

    await gateway.store.execute("INSERT INTO authorized_groups(chat_id,authorized_by) VALUES (-1,7)")
    bot = feedback_bot()
    gateway.agent.handle.return_value = AgentResult("純文字回覆", status=status, reaction=heart)
    context = SimpleNamespace(bot=bot)
    await gateway.handle(update(), context)
    await gateway.handle(update(), context)
    assert [c.kwargs["reaction"][0].emoji for c in bot.set_message_reaction.call_args_list] == expected
    gateway.agent.handle.assert_awaited_once()
    assert bot.send_message.call_args.kwargs["parse_mode"] is None


async def test_completion_reaction_waits_for_all_reply_chunks(gateway):
    await gateway.store.execute("INSERT INTO authorized_groups(chat_id,authorized_by) VALUES (-1,7)")
    await gateway.stage(
        "event", -1, 55, 123, [("first", None), ("last", None)], completion_reaction="👍", reaction_message_id=123
    )
    bot = feedback_bot()
    bot.send_message.side_effect = [SimpleNamespace(message_id=1), TimedOut()]
    await gateway.deliver(bot)
    bot.set_message_reaction.assert_not_awaited()
    await gateway.store.execute("UPDATE outbox SET state='pending' WHERE state='uncertain'")
    bot.send_message.side_effect = None
    await gateway.deliver(bot)
    bot.set_message_reaction.assert_awaited_once()
    assert bot.set_message_reaction.call_args.kwargs["reaction"][0].emoji == "👍"


async def test_queued_request_gets_feedback_before_agent_slot(gateway):
    import asyncio

    from editorial_bot.agent.core import AgentResult

    await gateway.store.execute("INSERT INTO authorized_groups(chat_id,authorized_by) VALUES (-1,7)")
    gateway.semaphore = asyncio.Semaphore(0)
    gateway.agent.handle.return_value = AgentResult("完成")
    received = asyncio.Event()
    typing = asyncio.Event()
    bot = feedback_bot()
    bot.set_message_reaction.side_effect = lambda **kw: received.set()
    bot.send_chat_action.side_effect = lambda **kw: typing.set()
    task = asyncio.create_task(gateway.handle(update(), SimpleNamespace(bot=bot)))
    try:
        await asyncio.wait_for(received.wait(), 1)
        await asyncio.wait_for(typing.wait(), 1)
        gateway.agent.handle.assert_not_awaited()
    finally:
        gateway.semaphore.release()
        await task


@pytest.mark.parametrize("button", [False, True])
async def test_tag_all_shows_progress_without_reacting_to_bot_menu(gateway, button):
    await gateway.store.execute("INSERT INTO authorized_groups(chat_id,authorized_by) VALUES (-1,7)")
    bot = feedback_bot()
    incoming = update(text="/ta")
    if button:
        incoming.callback_query = SimpleNamespace(data="mention_editors", answer=AsyncMock())

    async def tag(args, ctx):
        ctx.notices.append("@editor")

    gateway.editorial.tag.side_effect = tag
    await gateway.handle(incoming, SimpleNamespace(bot=bot))
    gateway.agent.handle.assert_not_awaited()
    gateway.editorial.tag.assert_awaited_once()
    bot.send_chat_action.assert_awaited()
    if button:
        bot.set_message_reaction.assert_not_awaited()
        incoming.callback_query.answer.assert_awaited_once()
    else:
        assert [c.kwargs["reaction"][0].emoji for c in bot.set_message_reaction.call_args_list] == ["👀", "👍"]


async def issue_question(gateway, options):
    from editorial_bot.agent.context import Pending
    from editorial_bot.agent.core import AgentResult

    await gateway.store.execute("INSERT INTO authorized_groups(chat_id,authorized_by) VALUES (-1,7)")
    bot = feedback_bot()
    bot.edit_message_reply_markup = AsyncMock()
    pending = Pending([], [], "ask", options=options)
    result = AgentResult("請選擇\n" + "\n".join(options), status="clarify", pending=pending)
    gateway.agent.handle.return_value = result
    await gateway.handle(update(), SimpleNamespace(bot=bot))
    sent = bot.send_message.call_args.kwargs
    assert sent["parse_mode"] is None
    menu = SimpleNamespace(
        message_id=456,
        message_thread_id=55,
        text=sent["text"],
        caption=None,
        reply_markup=sent["reply_markup"],
        from_user=SimpleNamespace(id=99),
        reply_to_message=None,
    )
    gateway.agent.handle.reset_mock()
    gateway.agent.handle.return_value = AgentResult("已收到答案")
    bot.send_message.return_value = SimpleNamespace(message_id=457)
    return bot, pending, menu


def choose_update(menu, index=0, user=8):
    incoming = update(user=user)
    incoming.update_id = 10
    incoming.effective_message = menu
    incoming.callback_query = SimpleNamespace(
        data=menu.reply_markup.inline_keyboard[0][index].callback_data, answer=AsyncMock()
    )
    return incoming


async def test_choice_button_resumes_original_question_once(gateway):
    bot, pending, menu = await issue_question(gateway, ["卡片 A", "卡片 B", "卡片 C"])
    assert [b.text for b in menu.reply_markup.inline_keyboard[0]] == ["1", "2", "3"]
    incoming = choose_update(menu, 1)
    await gateway.handle(incoming, SimpleNamespace(bot=bot))
    request = gateway.agent.handle.call_args.args[0]
    assert request.resume is pending and request.text == "卡片 B"
    assert request.thread_id == 55 and request.user_id == 8
    bot.edit_message_reply_markup.assert_awaited_once_with(chat_id=-1, message_id=456, reply_markup=None)
    incoming.update_id = 11
    await gateway.handle(incoming, SimpleNamespace(bot=bot))
    gateway.agent.handle.assert_awaited_once()
    assert "已回答" in incoming.callback_query.answer.call_args.kwargs["text"]


@pytest.mark.parametrize("invalid", ["user", "chat", "thread", "message", "expired", "restart", "forged", "revoked"])
async def test_invalid_choices_never_resume_or_consume(gateway, invalid):
    from editorial_bot.questions import Questions

    bot, _pending, menu = await issue_question(gateway, ["同意", "不同意"])
    incoming = choose_update(menu)
    if invalid == "user":
        incoming.effective_user.id = 99
    elif invalid == "chat":
        await gateway.store.execute("INSERT INTO authorized_groups(chat_id,authorized_by) VALUES (-2,7)")
        incoming.effective_chat.id = -2
    elif invalid == "thread":
        menu.message_thread_id = 56
    elif invalid == "message":
        menu.message_id = 999
    elif invalid == "expired":
        gateway.questions.ttl = 0
    elif invalid == "restart":
        gateway.questions = Questions(1800)
    elif invalid == "forged":
        incoming.callback_query.data = incoming.callback_query.data.rsplit(":", 1)[0] + ":99"
    elif invalid == "revoked":
        await gateway.store.execute("DELETE FROM authorized_groups")
    await gateway.handle(incoming, SimpleNamespace(bot=bot))
    gateway.agent.handle.assert_not_awaited()
    bot.edit_message_reply_markup.assert_not_awaited()
    assert not any(q.used for q in gateway.questions.entries.values())


@pytest.mark.parametrize("button", [True, False])
@pytest.mark.parametrize("agree", [True, False])
async def test_consent_and_refusal_are_enforced(gateway, button, agree):
    bot, pending, menu = await issue_question(gateway, ["同意", "不同意"])
    assert [b.text for b in menu.reply_markup.inline_keyboard[0]] == ["同意", "不同意"]
    if button:
        incoming = choose_update(menu, 0 if agree else 1)
    else:
        incoming = update(text="1" if agree else "2")
        incoming.update_id = 10
        incoming.effective_message.reply_to_message = menu
    await gateway.handle(incoming, SimpleNamespace(bot=bot))
    if agree:
        gateway.agent.handle.assert_awaited_once()
        assert gateway.agent.handle.call_args.args[0].resume is pending
        assert gateway.agent.handle.call_args.args[0].text == "同意"
    else:
        gateway.agent.handle.assert_not_awaited()
        assert "已取消" in bot.send_message.call_args.kwargs["text"]
        assert (await gateway.store.one("SELECT action FROM audit_log ORDER BY id DESC LIMIT 1"))["action"] == "cancel"
    stale = update(text="同意") if button else choose_update(menu)
    stale.update_id = 11
    if button:
        stale.effective_message.reply_to_message = menu
    await gateway.handle(stale, SimpleNamespace(bot=bot))
    assert gateway.agent.handle.await_count == int(agree)


async def test_callback_ack_and_keyboard_failure_do_not_drop_answer(gateway):
    bot, _pending, menu = await issue_question(gateway, ["甲", "乙"])
    incoming = choose_update(menu)
    incoming.callback_query.answer.side_effect = TimedOut()
    bot.edit_message_reply_markup.side_effect = TimedOut()
    await gateway.handle(incoming, SimpleNamespace(bot=bot))
    gateway.agent.handle.assert_awaited_once()
    assert gateway.agent.handle.call_args.args[0].text == "甲"


async def test_deferred_question_delivery_still_supports_buttons_and_text(gateway):
    from editorial_bot.agent.context import Pending
    from editorial_bot.agent.core import AgentResult

    await gateway.store.execute("INSERT INTO authorized_groups(chat_id,authorized_by) VALUES (-1,7)")
    result = AgentResult("請選擇", status="clarify", pending=Pending([], [], "ask", options=["甲", "乙"]))
    entry = gateway.questions.add(result, -1, 55, 8)
    await gateway.stage("event", -1, 55, 123, [("第一段", None), ("請選擇", None)], question_token=entry.token)
    bot = feedback_bot()
    bot.send_message.side_effect = [SimpleNamespace(message_id=455), SimpleNamespace(message_id=456)]
    await gateway.deliver(bot)
    first, last = bot.send_message.call_args_list
    assert first.kwargs["reply_markup"] is None
    markup = last.kwargs["reply_markup"]
    assert gateway.conversation(-1, 456, 8, 55) is result
    assert gateway.questions.choose(markup.inline_keyboard[0][1].callback_data, -1, 55, 8, 456).text == "乙"
