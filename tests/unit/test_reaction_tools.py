from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from editorial_bot.agent.core import Agent, AgentRequest
from editorial_bot.agent.tools.base import ToolRegistry
from editorial_bot.agent.tools.reaction_tools import ReactHeartArgs, build_reaction_tools
from editorial_bot.services.llm.base import LLMResponse, ToolCall, Usage


def response(text="", calls=None):
    return LLMResponse(text, calls or [], Usage(0, 0), "tool_use" if calls else "stop", "test", None)


@pytest.mark.parametrize("clarify", [False, True])
async def test_heart_reaches_gateway_after_success_or_later_clarification(clarify):
    final = response(calls=[ToolCall("ask", "ask_user", {"question": "哪一張卡？"})]) if clarify else response("謝謝你")
    llm = SimpleNamespace(chat=AsyncMock(side_effect=[response(calls=[ToolCall("heart", "react_heart", {})]), final]))
    agent = Agent(llm, ToolRegistry(build_reaction_tools()), SimpleNamespace(build=AsyncMock(return_value="policy")))
    result = await agent.handle(AgentRequest(-1, 55, 7, "writer", "小石，謝謝你", event_id="heart"))
    assert result.reaction == "❤"
    assert result.status == ("clarify" if clarify else "ok")


def test_heart_cannot_target_other_messages():
    with pytest.raises(ValidationError):
        ReactHeartArgs(message_id=999)
