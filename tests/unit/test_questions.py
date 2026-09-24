from types import SimpleNamespace

import pytest

from editorial_bot.agent.context import Pending
from editorial_bot.questions import Questions


def result(options):
    return SimpleNamespace(pending=Pending([], [], "ask", options=options))


def test_numeric_buttons_bind_owner_message_and_consume_once():
    questions = Questions(1800)
    entry = questions.add(result(["卡片 A", "卡片 B", "卡片 C"]), -1, 55, 7)
    questions.bind(entry.token, 456)
    buttons = questions.markup(entry.token).inline_keyboard[0]
    assert [b.text for b in buttons] == ["1", "2", "3"]
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)
    with pytest.raises(ValueError, match="提問者"):
        questions.choose(buttons[1].callback_data, -1, 55, 8, 456)
    with pytest.raises(ValueError, match="不符"):
        questions.choose(buttons[1].callback_data, -2, 55, 7, 456)
    with pytest.raises(ValueError, match="不符"):
        questions.choose(buttons[1].callback_data, -1, 56, 7, 456)
    with pytest.raises(ValueError, match="不符"):
        questions.choose(buttons[1].callback_data, -1, 55, 7, 457)
    choice = questions.choose(buttons[1].callback_data, -1, 55, 7, 456)
    assert choice.text == "卡片 B" and choice.entry.result.pending.ask_user_id == "ask"
    with pytest.raises(ValueError, match="已回答"):
        questions.choose(buttons[0].callback_data, -1, 55, 7, 456)


def test_confirmation_buttons_and_text_share_one_answer():
    questions = Questions(1800)
    entry = questions.add(result(["同意", "不同意"]), -1, None, 7)
    questions.bind(entry.token, 456)
    buttons = questions.markup(entry.token).inline_keyboard[0]
    assert [b.text for b in buttons] == ["同意", "不同意"]
    choice = questions.answer_text(entry.result.pending, "2", -1, None, 7)
    assert choice.text == "不同意" and choice.declined
    with pytest.raises(ValueError, match="已回答"):
        questions.choose(buttons[0].callback_data, -1, None, 7, 456)


def test_expired_and_forged_buttons_never_select():
    now = [0]
    questions = Questions(10, clock=lambda: now[0])
    entry = questions.add(result(["同意", "不同意"]), -1, None, 7)
    questions.bind(entry.token, 456)
    data = questions.markup(entry.token).inline_keyboard[0][0].callback_data
    with pytest.raises(ValueError):
        questions.choose(f"choose:{entry.token}:99", -1, None, 7, 456)
    now[0] = 11
    with pytest.raises(ValueError, match="過期"):
        questions.choose(data, -1, None, 7, 456)
    assert questions.markup(entry.token) is None
