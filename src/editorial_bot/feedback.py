"""Best-effort Telegram progress, independent of business writes and replies."""

import asyncio
import contextlib
import logging

from telegram import ReactionTypeEmoji
from telegram.constants import ChatAction
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

log = logging.getLogger(__name__)
TYPING_INTERVAL = 4.0
FEEDBACK_TIMEOUT = 3.0
REACT_RECEIVED = "👀"
# Telegram's standard reaction list does not include the annual bot's ✅.
REACT_DONE = "👍"


async def set_reaction(bot, chat_id, message_id, emoji):
    try:
        async with asyncio.timeout(FEEDBACK_TIMEOUT):
            await bot.set_message_reaction(
                chat_id=chat_id, message_id=message_id, reaction=[ReactionTypeEmoji(emoji=emoji)]
            )
    except (TelegramError, TimeoutError) as exc:
        log.debug("Reaction unavailable chat=%s message=%s type=%s", chat_id, message_id, type(exc).__name__)


async def _typing_loop(bot, chat_id, thread_id):
    while True:
        delay = TYPING_INTERVAL
        try:
            async with asyncio.timeout(FEEDBACK_TIMEOUT):
                await bot.send_chat_action(chat_id=chat_id, message_thread_id=thread_id, action=ChatAction.TYPING)
        except RetryAfter as exc:
            retry = exc.retry_after
            delay = max(delay, retry.total_seconds() if hasattr(retry, "total_seconds") else retry)
        except (BadRequest, Forbidden) as exc:
            log.debug("Typing unavailable chat=%s thread=%s type=%s", chat_id, thread_id, type(exc).__name__)
            return
        except (TelegramError, TimeoutError) as exc:
            log.debug("Typing failed chat=%s thread=%s type=%s", chat_id, thread_id, type(exc).__name__)
        await asyncio.sleep(delay)


@contextlib.asynccontextmanager
async def progress(bot, chat_id, thread_id, message_id):
    task = asyncio.create_task(_typing_loop(bot, chat_id, thread_id))
    try:
        if message_id is not None:
            await set_reaction(bot, chat_id, message_id, REACT_RECEIVED)
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
