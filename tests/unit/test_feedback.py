import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, TimedOut

from editorial_bot import feedback


@pytest.mark.parametrize("cancel", [False, True])
async def test_typing_refreshes_in_original_topic_and_stops_on_exit(monkeypatch, cancel):
    monkeypatch.setattr(feedback, "TYPING_INTERVAL", 0.01)
    refreshed = asyncio.Event()

    async def typing(**kwargs):
        if bot.send_chat_action.await_count >= 2:
            refreshed.set()

    bot = SimpleNamespace(send_chat_action=AsyncMock(side_effect=typing), set_message_reaction=AsyncMock())

    async def handle():
        async with feedback.progress(bot, -1, 55, 123):
            await refreshed.wait()
            if cancel:
                await asyncio.Event().wait()

    task = asyncio.create_task(handle())
    try:
        await asyncio.wait_for(refreshed.wait(), 1)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        calls = bot.send_chat_action.await_count
        await asyncio.sleep(0.03)
        assert bot.send_chat_action.await_count == calls
        for call in bot.send_chat_action.call_args_list:
            assert call.kwargs == {"chat_id": -1, "message_thread_id": 55, "action": "typing"}
        reaction = bot.set_message_reaction.call_args.kwargs
        assert reaction["message_id"] == 123 and reaction["reaction"][0].emoji == "👀"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_unsupported_reaction_and_typing_failure_do_not_block_work():
    bot = SimpleNamespace(
        send_chat_action=AsyncMock(side_effect=TimedOut()),
        set_message_reaction=AsyncMock(side_effect=BadRequest("REACTION_INVALID")),
    )
    async with feedback.progress(bot, -1, None, 123):
        await asyncio.sleep(0)
    bot.send_chat_action.assert_awaited()


async def test_feedback_timeout_does_not_block_work(monkeypatch):
    monkeypatch.setattr(feedback, "FEEDBACK_TIMEOUT", 0.01)

    async def hang(**kwargs):
        await asyncio.Event().wait()

    bot = SimpleNamespace(
        send_chat_action=AsyncMock(side_effect=hang), set_message_reaction=AsyncMock(side_effect=hang)
    )
    async with asyncio.timeout(1), feedback.progress(bot, -1, None, 123):
        pass
