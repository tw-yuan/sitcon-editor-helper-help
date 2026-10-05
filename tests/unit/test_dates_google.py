from datetime import UTC, datetime

import pytest

from editorial_bot.services.dates import validate_explicit_date
from editorial_bot.services.google import folder_name, link_styles


def test_folder_name_and_utf16_doc_links():
    assert folder_name(" 中文標題 ", "2027-01-05") == "0105_中文標題"
    url = "https://drive.google.com/drive/folders/id"
    document = {
        "tabs": [
            {
                "tabProperties": {"tabId": "tab1"},
                "documentTab": {"content": [{"startIndex": 1, "textRun": {"content": "🙂 " + url}}]},
            }
        ]
    }
    request = link_styles(document, [url])[0]["updateTextStyle"]
    assert request["range"] == {"startIndex": 4, "endIndex": 4 + len(url), "tabId": "tab1"}


@pytest.mark.parametrize("text", ["開卡，下週某天到期", "開卡", "2026/02/30 開卡"])
def test_no_guessing_dates(text):
    with pytest.raises(ValueError):
        validate_explicit_date(text, "2026-09-25", "Asia/Taipei")


def test_explicit_future_year():
    validate_explicit_date("開卡 2027年1月5日", "2027-01-05", "Asia/Taipei")


@pytest.mark.parametrize("compact,month,day", [("0925", 9, 25), ("0105", 1, 5), ("1231", 12, 31)])
def test_compact_card_date_is_an_explicit_deadline(compact, month, day):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    year = datetime.now(ZoneInfo("Asia/Taipei")).year
    validate_explicit_date(f"小石開卡 {compact} test", f"{year}-{month:02d}-{day:02d}", "Asia/Taipei")


@pytest.mark.parametrize("number", ["10925", "20260925", "#0925", "test0925", "0925test", "0925/7"])
def test_compact_date_does_not_match_other_identifiers(number):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    year = datetime.now(ZoneInfo("Asia/Taipei")).year
    with pytest.raises(ValueError):
        validate_explicit_date(f"小石開卡 {number} test", f"{year}-09-25", "Asia/Taipei")


@pytest.mark.parametrize("compact", ["0230", "0000", "1331"])
def test_impossible_compact_dates_are_rejected(compact):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    year = datetime.now(ZoneInfo("Asia/Taipei")).year
    with pytest.raises(ValueError):
        validate_explicit_date(f"小石開卡 {compact} test", f"{year}-09-25", "Asia/Taipei")


def test_compact_date_does_not_guess_a_future_year():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    year = datetime.now(ZoneInfo("Asia/Taipei")).year
    with pytest.raises(ValueError):
        validate_explicit_date("小石開卡 0925 test", f"{year + 1}-09-25", "Asia/Taipei")


@pytest.mark.parametrize(
    "word,expected",
    [
        ("今天", "2026-10-06"),
        ("今日", "2026-10-06"),
        ("明天", "2026-10-07"),
        ("明日", "2026-10-07"),
        ("後天", "2026-10-08"),
        ("后天", "2026-10-08"),
    ],
)
def test_relative_dates_use_taiwan_day_instead_of_utc(word, expected):
    reference = datetime(2026, 10, 5, 16, 30, tzinfo=UTC)
    validate_explicit_date(f"小石開卡 公告 DDL {word}", expected, "Asia/Taipei", reference_time=reference)
    with pytest.raises(ValueError):
        validate_explicit_date(f"小石開卡 公告 DDL {word}", "2026-10-05", "Asia/Taipei", reference_time=reference)


@pytest.mark.parametrize(
    "reference,word,expected",
    [
        ("2026-10-31T10:00:00+08:00", "明天", "2026-11-01"),
        ("2026-12-31T10:00:00+08:00", "後天", "2027-01-02"),
        ("2028-02-28T10:00:00+08:00", "明天", "2028-02-29"),
        ("2027-02-28T10:00:00+08:00", "明天", "2027-03-01"),
    ],
)
def test_relative_dates_follow_calendar_boundaries(reference, word, expected):
    validate_explicit_date(word, expected, "Asia/Taipei", reference_time=datetime.fromisoformat(reference))


@pytest.mark.parametrize("text", ["下週某天到期", "過幾天到期", "大後天到期", "大后天到期", "開卡"])
def test_unsupported_relative_dates_do_not_get_a_guessed_deadline(text):
    with pytest.raises(ValueError):
        validate_explicit_date(text, "2026-10-07", "Asia/Taipei", reference_time=datetime(2026, 10, 5, tzinfo=UTC))


async def test_prompt_and_validator_share_the_same_reference_time():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from editorial_bot.agent.prompts import PromptBuilder

    reference = datetime(2026, 12, 31, 16, 1, tzinfo=UTC)
    prompt = await PromptBuilder(
        SimpleNamespace(tz="Asia/Taipei"), SimpleNamespace(all=AsyncMock(return_value=[]))
    ).build(chat_id=-1, reference_time=reference)
    assert "今天／今日=2027-01-01" in prompt
    assert "明天／明日=2027-01-02" in prompt
    assert "後天=2027-01-03" in prompt
    validate_explicit_date("後天", "2027-01-03", "Asia/Taipei", reference_time=reference)
