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
