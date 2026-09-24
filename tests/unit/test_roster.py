import pytest

from editorial_bot.services.google import folder_name
from editorial_bot.services.sheets_roster import RosterUnavailableError, parse_roster


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
    assert not roster.search_by_name("Writer")


def test_conflicting_identity_rejected():
    with pytest.raises(RosterUnavailableError, match="多個 GitLab"):
        parse_roster(
            [["Telegram ID", "gitlab_id", "Nickname", "default", "note"], ["writer_a", "10"], ["writer_a", "11"]]
        )


def test_folder_name_preserves_title_and_mmdd():
    assert folder_name("中秋節&公告年會日期", "2026-09-25") == "0925_中秋節&公告年會日期"
    with pytest.raises(ValueError):
        folder_name("test", "2026-02-30")
