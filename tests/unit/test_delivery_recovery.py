import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from editorial_bot.gateway import Gateway
from editorial_bot.storage.db import Store


async def test_concurrent_delivery_and_restart_preserve_confirmed_sent(tmp_path):
    path = str(tmp_path / "state.sqlite3")
    store = await Store.open(path)
    gateway = Gateway(SimpleNamespace(max_concurrent_agent_turns=2), store, None, None, None, None)
    await store.execute("INSERT INTO authorized_groups(chat_id,authorized_by) VALUES (-1,7)")
    await store.claim_event("event", -1, 55, 7, 123)
    await gateway.stage("event", -1, 55, 123, [("review @chief", "HTML")])
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=456)))
    await asyncio.gather(gateway.deliver(bot), gateway.deliver(bot))
    bot.send_message.assert_awaited_once()
    await store.close()
    store = await Store.open(path)
    gateway.store = store
    await gateway.recover(bot)
    bot.send_message.assert_awaited_once()
    assert not await store.claim_event("event", -1)
    await store.close()


async def test_crashed_review_recovers_notification_in_original_topic(tmp_path):
    store = await Store.open(str(tmp_path / "db.sqlite3"))
    await store.execute("INSERT INTO authorized_groups(chat_id,authorized_by) VALUES (-1,7)")
    await store.claim_event("event", -1, 55, 7, 123)
    await store.operation("operation", "event", -1, 7, "review", {"iid": 1})
    await store.finish("operation", {"iid": 1, "notification": "@chief 請 review"})
    gateway = Gateway(SimpleNamespace(max_concurrent_agent_turns=2), store, None, None, None, None)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=456)))
    await gateway.recover(bot)
    rows = await store.all("SELECT * FROM outbox ORDER BY id")
    assert json.loads(rows[0]["body"])["text"] == "@chief 請 review"
    assert rows[0]["thread_id"] == 55
    assert all(row["state"] == "sent" for row in rows)
    await store.close()


@pytest.mark.parametrize("kind", ["create_card", "add_card_folder", "add_card_document"])
async def test_crashed_resource_workflow_recovers_completed_links(tmp_path, kind):
    store = await Store.open(str(tmp_path / "db.sqlite3"))
    try:
        await store.execute("INSERT INTO authorized_groups(chat_id,authorized_by) VALUES (-1,7)")
        await store.claim_event("event", -1, 55, 7, 123)
        await store.operation("operation", "event", -1, 7, kind, {"iid": 682})
        links = {
            "issue_url": "https://gitlab.com/p/-/issues/682",
            "folder_url": "https://drive.google.com/drive/folders/folder",
            "document_url": "https://docs.google.com/document/d/doc/edit",
        }
        await store.finish("operation", links)
        gateway = Gateway(SimpleNamespace(max_concurrent_agent_turns=2), store, None, None, None, None)
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=456)))
        await gateway.recover(bot)
        sent = bot.send_message.call_args.kwargs
        assert all(link in sent["text"] for link in links.values())
        assert sent["message_thread_id"] == 55
        await gateway.recover(bot)
        bot.send_message.assert_awaited_once()
    finally:
        await store.close()
