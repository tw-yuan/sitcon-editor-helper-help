from pathlib import Path

import pytest

from editorial_bot.storage.db import Store


@pytest.fixture
async def store(tmp_path):
    db = await Store.open(str(tmp_path / "test.sqlite3"))
    yield db
    await db.close()


async def test_events_and_migrations_survive_restart(tmp_path):
    path = str(tmp_path / "test.sqlite3")
    db = await Store.open(path)
    assert await db.claim_event("update-1", -1)
    assert not await db.claim_event("update-1", -1)
    await db.add_memory(-1, 7, "writer", "文案先附來源")
    await db.close()
    db = await Store.open(path)
    assert {r["version"] for r in await db.all("SELECT * FROM schema_migrations")} == {
        "001_initial.sql",
        "002_event_delivery.sql",
        "003_review_signatures.sql",
        "004_optional_card_resources.sql",
    }
    assert len(await db.all("SELECT * FROM group_memories WHERE chat_id=?", (-1,))) == 1
    assert not await db.claim_event("update-1", -1)
    await db.close()


async def test_memory_isolation_and_dedup(store):
    first = await store.add_memory(-1, 7, "writer", "群 A 的資訊")
    assert await store.add_memory(-1, 7, "writer", "群 A 的資訊") == first
    assert await store.all("SELECT * FROM group_memories WHERE chat_id=?", (-2,)) == []
    await store.execute("DELETE FROM group_memories WHERE chat_id=? AND id=?", (-2, first))
    assert await store.one("SELECT id FROM group_memories WHERE id=?", (first,))


async def test_optional_resources_migration_preserves_legacy_rows_and_accepts_card_only(tmp_path):
    old_migrations = tmp_path / "old-migrations"
    old_migrations.mkdir()
    for name in ("001_initial.sql", "002_event_delivery.sql", "003_review_signatures.sql"):
        (old_migrations / name).write_text((Path("migrations") / name).read_text())
    path = str(tmp_path / "upgrade.sqlite3")
    db = await Store.open(path, str(old_migrations))
    for row in ((1, "folder", "doc", "document-op"), (2, "legacy-folder", None, "task-op")):
        await db.execute("INSERT INTO resources VALUES (?,?,?,?)", row)
    before = await db.all("SELECT * FROM resources ORDER BY issue_iid")
    await db.close()
    db = await Store.open(path)
    try:
        assert await db.all("SELECT * FROM resources ORDER BY issue_iid") == before
        await db.execute("INSERT INTO resources VALUES (3,NULL,NULL,'card-only-op')")
    finally:
        await db.close()
    db = await Store.open(path)
    try:
        assert len(await db.all("SELECT * FROM resources")) == 3
        assert (await db.one("SELECT folder_id FROM resources WHERE issue_iid=3"))["folder_id"] is None
    finally:
        await db.close()
