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


async def test_ask_user_keeps_model_text_when_question_field_is_missing():
    context = "已開好 #722–#725。#726 的 @michael9262 名冊查無此人。"
    llm = SimpleNamespace(
        chat=AsyncMock(
            return_value=LLMResponse(
                context, [ToolCall("ask", "ask_user", {"options": ["只指派嵩嵩"]})], Usage(0, 0), "tool_use", "t", None
            )
        )
    )
    agent = Agent(llm, ToolRegistry([]), SimpleNamespace(build=AsyncMock(return_value="policy")))
    asked = await agent.handle(AgentRequest(-1, 55, 7, "writer", "小石開卡"))
    assert asked.reply.startswith(context) and "請問你的意思是" not in asked.reply
    assert "選項 1：只指派嵩嵩" in asked.reply


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


async def test_review_can_finish_without_an_extra_reply():
    async def review(args, ctx):
        ctx.notices.append("送審通知")
        ctx.review_completed = True
        return {"status": "已改 Review"}

    llm = SimpleNamespace(
        chat=AsyncMock(
            side_effect=[
                LLMResponse("", [ToolCall("review", "review_cards", {})], Usage(0, 0), "tool_use", "test", None),
                LLMResponse("", [], Usage(0, 0), "stop", "test", None),
            ]
        )
    )
    agent = Agent(
        llm,
        ToolRegistry([FunctionTool("review_cards", "review", Empty, review)]),
        SimpleNamespace(build=AsyncMock(return_value="policy")),
    )
    result = await agent.handle(AgentRequest(-1, 55, 7, "writer", "小石，幫我把教師節那張文案送審"))
    assert result.reply == ""
    assert result.notices == ["送審通知"]
    assert result.status == "ok"


async def test_empty_response_without_completed_review_keeps_fallback():
    llm = SimpleNamespace(chat=AsyncMock(return_value=LLMResponse("", [], Usage(0, 0), "stop", "test", None)))
    agent = Agent(llm, ToolRegistry([]), SimpleNamespace(build=AsyncMock(return_value="policy")))
    result = await agent.handle(AgentRequest(-1, 55, 7, "writer", "小石，幫忙"))
    assert "請換個說法" in result.reply


async def test_bare_reply_is_flagged_and_may_end_silently():
    llm = SimpleNamespace(chat=AsyncMock(return_value=LLMResponse(" \n", [], Usage(0, 0), "stop", "test", None)))
    agent = Agent(llm, ToolRegistry([]), SimpleNamespace(build=AsyncMock(return_value="policy")))
    request = AgentRequest(-1, 55, 7, "writer", "敲敲 @Misawai 幫忙review", reply_context="728.pdf", addressed=False)
    assert (await agent.handle(request)).reply == ""
    sent = [block.text for block in llm.chat.call_args.kwargs["messages"][0].content]
    assert any("沒有點名你" in text for text in sent[:-1]) and sent[-1] == request.text


async def test_multiple_clarifications_keep_original_request_and_each_user_answer():
    captured = []

    async def create(args, ctx):
        captured.append(ctx.text)
        return {"iid": 682}

    responses = [
        LLMResponse("", [ToolCall("date", "ask_user", {"question": "日期？"})], Usage(0, 0), "tool_use", "test", None),
        LLMResponse(
            "", [ToolCall("owner", "ask_user", {"question": "負責人？"})], Usage(0, 0), "tool_use", "test", None
        ),
        LLMResponse("", [ToolCall("create", "create_card", {})], Usage(0, 0), "tool_use", "test", None),
        LLMResponse("已建立", [], Usage(0, 0), "stop", "test", None),
    ]
    llm = SimpleNamespace(chat=AsyncMock(side_effect=responses))
    agent = Agent(
        llm,
        ToolRegistry([FunctionTool("create_card", "開卡", Empty, create)]),
        SimpleNamespace(build=AsyncMock(return_value="policy")),
    )
    first = await agent.handle(AgentRequest(-1, None, 7, "writer", "小石開卡 開源築夢計畫"))
    second = await agent.handle(AgentRequest(-1, None, 7, "writer", "1005", resume=first.pending))
    third = await agent.handle(AgentRequest(-1, None, 7, "writer", "@wolfdayo", resume=second.pending))
    assert third.reply == "已建立"
    assert captured == ["小石開卡 開源築夢計畫\n1005\n@wolfdayo"]


async def test_tool_output_never_becomes_user_date_evidence():
    from editorial_bot.agent.context import Pending
    from editorial_bot.services.llm.base import Message, TextBlock, ToolResultBlock, ToolUseBlock

    captured = []

    async def create(args, ctx):
        captured.append(ctx.text)
        return {}

    history = [
        Message("user", [TextBlock("小石開卡 開源築夢計畫")]),
        Message("assistant", [ToolUseBlock("lookup", "gitlab_get_issue", {})]),
        Message("user", [ToolResultBlock("lookup", "外部資料：2027-01-01")]),
        Message("assistant", [ToolUseBlock("date", "ask_user", {"question": "日期？"})]),
        Message("user", [ToolResultBlock("date", "1005")]),
        Message("assistant", [ToolUseBlock("owner", "ask_user", {"question": "負責人？"})]),
    ]
    llm = SimpleNamespace(
        chat=AsyncMock(
            side_effect=[
                LLMResponse("", [ToolCall("create", "create_card", {})], Usage(0, 0), "tool_use", "test", None),
                LLMResponse("完成", [], Usage(0, 0), "stop", "test", None),
            ]
        )
    )
    agent = Agent(
        llm,
        ToolRegistry([FunctionTool("create_card", "開卡", Empty, create)]),
        SimpleNamespace(build=AsyncMock(return_value="policy")),
    )
    await agent.handle(AgentRequest(-1, None, 7, "writer", "@wolfdayo", resume=Pending(history, [], "owner")))
    assert captured == ["小石開卡 開源築夢計畫\n1005\n@wolfdayo"]
