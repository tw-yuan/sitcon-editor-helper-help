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


async def test_question_options_survive_resume_and_plain_text_formatting():
    llm = SimpleNamespace(
        chat=AsyncMock(
            side_effect=[
                LLMResponse(
                    "",
                    [ToolCall("ask", "ask_user", {"question": "要哪張？", "options": ["甲", "乙"]})],
                    Usage(0, 0),
                    "tool_use",
                    "test",
                    None,
                ),
                LLMResponse("選好乙了", [], Usage(0, 0), "stop", "test", None),
            ]
        )
    )
    agent = Agent(llm, ToolRegistry([]), SimpleNamespace(build=AsyncMock(return_value="policy")))
    asked = await agent.handle(AgentRequest(-1, 55, 7, "writer", "小石，選卡片"))
    assert asked.pending.options == ["甲", "乙"]
    assert "選項 1：甲" in asked.reply and "選項 2：乙" in asked.reply
    assert "按鈕" in asked.reply and "回覆" in asked.reply
    answered = await agent.handle(AgentRequest(-1, 55, 7, "writer", "乙", resume=asked.pending))
    assert answered.reply == "選好乙了"
    from editorial_bot.services.llm.base import ToolResultBlock

    results = [b for m in llm.chat.call_args.kwargs["messages"] for b in m.content if isinstance(b, ToolResultBlock)]
    assert any(b.tool_call_id == "ask" and b.content == "乙" for b in results)
