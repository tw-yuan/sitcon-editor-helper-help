"""The editorial 'user' sheet is the sole member directory."""

import asyncio
import re
import time
from dataclasses import asdict, dataclass


class RosterUnavailableError(RuntimeError):
    pass


@dataclass
class Member:
    gitlab_id: int
    telegram_username: str | None
    nickname: str = ""
    default: bool = False
    position: str = ""
    telegram_id: int | None = None
    gitlab_username: str | None = None
    role: str = "編輯組"


class Roster:
    def __init__(self, members: list[Member]):
        self.members = members

    def by_telegram_id(self, telegram_id: int) -> Member | None:
        hits = [m for m in self.members if m.telegram_id == telegram_id]
        return hits[0] if len(hits) == 1 else None

    def search_by_name(self, query: str) -> list[Member]:
        # API compatibility with the inherited agent; no nickname guessing for assignment.
        value = query.lstrip("@").casefold()
        return [m for m in self.members if m.telegram_username and m.telegram_username.casefold() == value]

    def by_gitlab(self, user_id: int) -> Member | None:
        return next((m for m in self.members if m.gitlab_id == user_id), None)

    def default_member(self) -> Member:
        matches = [m for m in self.members if m.default]
        if len(matches) != 1:
            raise ValueError("名冊沒有唯一的 default=yes，請指定 Telegram username。")
        return matches[0]

    def chiefs(self) -> list[Member]:
        return [m for m in self.members if set(re.split(r"[、,;/\s]+", m.position)) & {"總召", "副召"}]

    def to_llm_rows(self) -> list[dict]:
        return [asdict(m) for m in self.members]


def parse_roster(values: list[list[str]]) -> Roster:
    if not values:
        raise RosterUnavailableError("成員表沒有資料")
    header = [str(v).strip().casefold() for v in values[0]]
    required = ["telegram id", "gitlab_id", "nickname", "default", "note"]
    if not set(required).issubset(header):
        raise RosterUnavailableError("成員表欄位必須包含 Telegram ID、gitlab_id、Nickname、default、note")
    members: list[Member] = []
    identities: dict[str, int] = {}
    gitlab_users: dict[int, str] = {}
    for number, row in enumerate(values[1:], 2):
        if not any(str(v).strip() for v in row):
            continue
        record = {name: str(row[i]).strip() if i < len(row) else "" for i, name in enumerate(header)}
        handle = record["telegram id"].lstrip("@")
        raw_id = record["gitlab_id"]
        if not raw_id.isdecimal() or int(raw_id) <= 0:
            raise RosterUnavailableError(f"名冊第 {number} 列 gitlab_id 格式不正確，請修正原表。")
        if handle and not handle.isdecimal() and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", handle):
            raise RosterUnavailableError(f"名冊第 {number} 列 Telegram username 格式不正確。")
        identity = handle.casefold()
        gid = int(raw_id)
        if identity and identity in identities and identities[identity] != gid:
            raise RosterUnavailableError("同一 Telegram 身分對應多個 GitLab ID，請先修正名冊。")
        if gid in gitlab_users:
            if gitlab_users[gid] != identity:
                raise RosterUnavailableError("同一 GitLab ID 對應多個 Telegram 身分，請先修正名冊。")
            continue
        if identity:
            identities[identity] = gid
        gitlab_users[gid] = identity
        members.append(
            Member(
                gitlab_id=gid,
                telegram_username=handle if handle and not handle.isdecimal() else None,
                telegram_id=int(handle) if handle.isdecimal() else None,
                nickname=record["nickname"] or handle or raw_id,
                default=record["default"].casefold() == "yes",
                position=record["note"],
            )
        )
    if not members:
        raise RosterUnavailableError("名冊沒有有效成員")
    return Roster(members)


class RosterService:
    def __init__(self, google, ttl_seconds: int = 300):
        self.google = google
        self.ttl = ttl_seconds
        self._roster: Roster | None = None
        self._at = 0.0
        self.lock = asyncio.Lock()

    async def get(self, *, force: bool = False) -> Roster:
        async with self.lock:
            if not force and self._roster is not None and time.monotonic() - self._at < self.ttl:
                return self._roster
            try:
                self._roster = parse_roster(await self.google.roster_values())
                self._at = time.monotonic()
                return self._roster
            except Exception as exc:
                raise RosterUnavailableError(f"成員表目前無法讀取：{exc}") from exc

    async def reload(self) -> Roster:
        return await self.get(force=True)
