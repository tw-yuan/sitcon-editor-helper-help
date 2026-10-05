"""Validate explicit dates and unambiguous relative days against the configured timezone."""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

RELATIVE_DAYS = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "後天": 2, "后天": 2}


def validate_explicit_date(text: str, selected: str, timezone: str, *, reference_time: datetime | None = None) -> None:
    zone = ZoneInfo(timezone)
    today = (reference_time.astimezone(zone) if reference_time else datetime.now(zone)).date()
    year = today.year
    pattern = r"(?<!\d)(?:(\d{4})[-/年])?(\d{1,2})[-/月](\d{1,2})(?:日)?(?!\d)"
    candidates = []
    for match in re.finditer(pattern, text):
        try:
            candidates.append(date(int(match[1] or year), int(match[2]), int(match[3])).isoformat())
        except ValueError:
            continue
    # A standalone four-digit MMDD token is explicit input, not a relative-date guess.
    # Whitespace boundaries keep issue numbers and longer identifiers out of the date set.
    for match in re.finditer(r"(?<!\S)([0-9]{2})([0-9]{2})(?!\S)", text):
        try:
            candidates.append(date(year, int(match[1]), int(match[2])).isoformat())
        except ValueError:
            continue
    for match in re.finditer(r"(?<!大)(今天|今日|明天|明日|後天|后天)", text):
        candidates.append((today + timedelta(days=RELATIVE_DAYS[match[0]])).isoformat())
    if selected not in candidates:
        raise ValueError("到期日與使用者提供的日期不符；接受明確日期或依台灣時間換算的今天、明天、後天。")
