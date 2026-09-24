"""SQLite migrations, durable operation checkpoints and notification delivery."""

import asyncio
import json
from pathlib import Path
from typing import Any

import aiosqlite


class Store:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn
        self.lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str, migrations: str = "migrations") -> "Store":
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY)")
        await conn.commit()
        files = await asyncio.to_thread(lambda: sorted(Path(migrations).glob("*.sql")))
        for file in files:
            cur = await conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (file.name,))
            if await cur.fetchone():
                continue
            try:
                await conn.executescript("BEGIN IMMEDIATE;\n" + file.read_text())
                await conn.execute("INSERT INTO schema_migrations VALUES (?)", (file.name,))
                await conn.commit()
            except Exception:
                await conn.rollback()
                await conn.close()
                raise
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    async def all(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self.conn.execute(sql, params) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = await self.all(sql, params)
        return rows[0] if rows else None

    async def execute(self, sql: str, params: tuple = ()) -> int:
        async with self.lock:
            cur = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cur.lastrowid or 0

    async def claim_event(self, event_id: str, chat_id: int) -> bool:
        async with self.lock:
            cur = await self.conn.execute(
                "INSERT OR IGNORE INTO events(id,chat_id,state) VALUES (?,?,'running')", (event_id, chat_id)
            )
            await self.conn.commit()
            return cur.rowcount == 1

    async def operation(self, key: str, event_id: str, chat_id: int, user_id: int, kind: str, payload: dict) -> dict:
        await self.execute(
            "INSERT OR IGNORE INTO operations(id,event_id,chat_id,user_id,kind,payload) VALUES (?,?,?,?,?,?)",
            (key, event_id, chat_id, user_id, kind, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )
        row = await self.one("SELECT * FROM operations WHERE id=?", (key,))
        assert row is not None
        row["steps"] = json.loads(row["steps"])
        return row

    async def checkpoint(self, key: str, steps: dict, state: str = "running", error: str | None = None) -> None:
        await self.execute(
            "UPDATE operations SET steps=?,state=?,error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (json.dumps(steps, ensure_ascii=False), state, error, key),
        )

    async def finish(self, key: str, result: Any) -> None:
        await self.execute(
            "UPDATE operations SET state='done',result=?,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (json.dumps(result, ensure_ascii=False), key),
        )

    async def add_memory(self, chat: int, user: int, username: str | None, content: str) -> int:
        async with self.lock:
            rows = await self.all("SELECT id,content FROM group_memories WHERE chat_id=?", (chat,))
            for row in rows:
                if row["content"] == content:
                    return row["id"]
            if len(rows) >= 30:
                raise ValueError("本群記憶已達 30 筆，請先檢視並明確移除過時事項。")
            cur = await self.conn.execute(
                "INSERT INTO group_memories(chat_id,content,created_by,created_by_name) VALUES (?,?,?,?)",
                (chat, content, user, username),
            )
            await self.conn.commit()
            return int(cur.lastrowid)
