"""Do not let a model turn relative dates or missing input into a guessed deadline."""

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo


def validate_explicit_date(text: str, selected: str, timezone: str) -> None:
    year = datetime.now(ZoneInfo(timezone)).year
    pattern = r"(?<!\d)(?:(\d{4})[-/年])?(\d{1,2})[-/月](\d{1,2})(?:日)?(?!\d)"
    candidates = []
    for match in re.finditer(pattern, text):
        try:
            candidates.append(date(int(match[1] or year), int(match[2]), int(match[3])).isoformat())
        except ValueError:
            continue
    if selected not in candidates:
        raise ValueError("找不到使用者明確提供的有效到期日，請補問月／日；不把相對日期自行換算。")
