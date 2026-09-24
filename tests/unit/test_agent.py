from types import SimpleNamespace
from unittest.mock import AsyncMock

from editorial_bot.agent.core import Agent, AgentRequest
from editorial_bot.agent.tools.base import ToolRegistry
from editorial_bot.agent.tools.editorial import Empty, FunctionTool
from editorial_bot.services.llm.base import LLMResponse, ToolCall, Usage


async def test_ask_user_never_executes_sibling_mutation():
    write = AsyncMock()
    tool = FunctionTool("write", "mutation", Empty, write)
    llm = SimpleNamespace(
        chat=AsyncMock(
            return_value=LLMResponse(
                usage=Usage(0, 0),
                stop_reason="tool_use",
                model="test",
                raw_assistant=None,
                text="",
                tool_calls=[ToolCall("a", "write", {}), ToolCall("b", "ask_user", {"question": "日期？"})],
            )
        )
    )
    agent = Agent(llm, ToolRegistry([tool]), SimpleNamespace(build=AsyncMock(return_value="policy")))
    result = await agent.handle(AgentRequest(-1, 3, 7, "writer", "開卡", event_id="event"))
    assert result.status == "clarify" and result.pending
    write.assert_not_awaited()
    assert len(result.pending.resolved_results) == 1
