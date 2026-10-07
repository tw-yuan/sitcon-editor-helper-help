from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from editorial_bot.services.google import folder_name
from editorial_bot.services.sheets_roster import Member, Roster, RosterService, RosterUnavailableError, parse_roster


def test_actual_editorial_headers_and_roles():
    roster = parse_roster(
        [
            ["Telegram ID", "gitlab_id", "Nickname", "default", "note"],
            ["writer_a", "10", "Writer", "yes"],
            ["leader_a", "11", "Lead", "no", "總召"],
            ["deputy_a", "12", "Deputy", "no", "副召"],
        ]
    )
    assert roster.default_member().gitlab_id == 10
    assert roster.members[0].telegram_id is None
    assert roster.members[0].telegram_username == "writer_a"
    assert {m.gitlab_id for m in roster.chiefs()} == {11, 12}
    assert roster.search_by_name("@WRITER_A")[0].gitlab_id == 10
    assert roster.search_by_name("Writer")[0].gitlab_id == 10


def test_nickname_lookup_is_exact_and_returns_all_ambiguous_matches():
    roster = Roster([Member(1, "octopus", "章魚"), Member(2, "writer_b", "章魚"), Member(3, "writer_c", "octopus")])
    assert [m.gitlab_id for m in roster.search_by_name(" 章魚 ")] == [1, 2]
    assert [m.gitlab_id for m in roster.search_by_name("OCTOPUS")] == [1, 3]
    assert [m.gitlab_id for m in roster.search_by_name("@OCTOPUS")] == [1]
    assert roster.search_by_name("章") == []
    assert roster.search_by_name("") == []
    assert roster.search_by_name("@章魚") == []


async def test_failed_refresh_invalidates_previously_cached_roster():
    values = [["Telegram ID", "gitlab_id", "nickname", "default", "note"], ["octopus", "10", "章魚", "yes"]]
    google = SimpleNamespace(
        roster_values=AsyncMock(side_effect=[values, RuntimeError("offline"), RuntimeError("offline")])
    )
    service = RosterService(google)
    await service.get()
    with pytest.raises(RosterUnavailableError, match="offline"):
        await service.get(force=True)
    with pytest.raises(RosterUnavailableError, match="offline"):
        await service.get()
    assert google.roster_values.await_count == 3


def test_conflicting_identity_rejected():
    with pytest.raises(RosterUnavailableError, match="多個 GitLab"):
        parse_roster(
            [["Telegram ID", "gitlab_id", "Nickname", "default", "note"], ["writer_a", "10"], ["writer_a", "11"]]
        )


def test_folder_name_preserves_title_and_mmdd():
    assert folder_name("中秋節&公告年會日期", "2026-09-25") == "0925_中秋節&公告年會日期"
    with pytest.raises(ValueError):
        folder_name("test", "2026-02-30")
