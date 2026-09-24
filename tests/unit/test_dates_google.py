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


@pytest.mark.parametrize("text", ["開卡，明天到期", "開卡", "2026/02/30 開卡"])
def test_no_guessing_dates(text):
    with pytest.raises(ValueError):
        validate_explicit_date(text, "2026-09-25", "Asia/Taipei")


def test_explicit_future_year():
    validate_explicit_date("開卡 2027年1月5日", "2027-01-05", "Asia/Taipei")
