"""Live read-only acceptance: all mutating tools are blocked at the execution boundary.

Run inside Compose with the source tree mounted at /checks. Never sends Telegram messages.
"""

import asyncio
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from editorial_bot.agent.core import ASK_USER_SPEC, Agent, AgentRequest
from editorial_bot.agent.prompts import PromptBuilder
from editorial_bot.agent.tools.base import ToolContext, ToolRegistry
from editorial_bot.agent.tools.editorial import EditorialTools
from editorial_bot.agent.tools.reaction_tools import build_reaction_tools
from editorial_bot.agent.tools.search_tools import build_search_tools
from editorial_bot.logging_setup import configure
from editorial_bot.services.dates import validate_explicit_date
from editorial_bot.services.gitlab import GitLab
from editorial_bot.services.google import Google
from editorial_bot.services.knowledge import Knowledge
from editorial_bot.services.llm.base import Message, TextBlock, ToolResultBlock, build_llm_client
from editorial_bot.services.sheets_roster import RosterService
from editorial_bot.services.web_search import build_web_search_service
from editorial_bot.services.workflow import CardWorkflow
from editorial_bot.settings import Settings
from editorial_bot.storage.db import Store

READ_TOOLS = {
    "resolve_member",
    "gitlab_get_issue",
    "gitlab_search_issues",
    "gitlab_list_labels",
    "search_wiki",
    "read_wiki_page",
    "read_wiki_document",
    "memory_list",
    "web_search",
    "react_heart",
}


async def main():
    settings = Settings()
    configure(settings)
    store = await Store.open(":memory:")
    gl = GitLab(settings.gitlab_url, settings.gitlab_project, settings.gitlab_token.get_secret_value())
    try:
        google = Google(settings)
        roster = RosterService(google)
        knowledge = Knowledge(gl, google)
        editorial = EditorialTools(
            gl, google, roster, knowledge, CardWorkflow(gl, google, store, settings), store, settings
        )
        attempted_writes = []
        tools = editorial.build() + build_search_tools(build_web_search_service(settings)) + build_reaction_tools()
        for tool in tools:
            if tool.name not in READ_TOOLS:

                async def blocked(args, ctx, name=tool.name):
                    attempted_writes.append(name)
                    raise ValueError("唯讀驗證禁止執行寫入工具。")

                tool.fn = blocked
        agent = Agent(
            build_llm_client(settings),
            ToolRegistry(tools),
            PromptBuilder(settings, store),
            roster,
            thinking=settings.llm_thinking,
            max_iterations=4,
        )
        status = await agent.handle(
            AgentRequest(
                -1,
                None,
                settings.telegram_admin_id,
                None,
                "小石，請查目前編輯組專案有哪些 Status 狀態，不要修改資料。",
                event_id="check-status",
            )
        )
        date = await agent.handle(
            AgentRequest(
                -1,
                None,
                settings.telegram_admin_id,
                None,
                "小石，幫我開一張文案卡：測試公告，下週某天到期。",
                event_id="check-date",
            )
        )
        consent = await agent.handle(
            AgentRequest(
                -1,
                None,
                settings.telegram_admin_id,
                None,
                "小石，請先問我是否同意把本群偏好記為『文案開頭要放重點』，取得我同意後才保存。",
                event_id="check-consent",
            )
        )
        assert consent.status == "clarify" and consent.pending.options == ["同意", "不同意"], (
            "同意問題未提供正確按鈕選項"
        )
        assert "gitlab_list_labels" in (status.detail or {}).get("tools", []), "狀態回答未查詢實際標籤"
        assert date.status == "clarify", "無法確定的日期未補問"
        for result in (status, date, consent):
            assert not re.search(
                r"(?m)^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s)|`|\*\*|\[[^\]]+\]\([^)]+\)|</?[A-Za-z][^>]*>", result.reply
            ), "回覆包含 Markdown 或 HTML 語法"
        assert not attempted_writes, f"意外要求寫入工具：{attempted_writes}"
        # Inspect the live model's proposed arguments only. Never execute these tool calls.
        create = next(tool for tool in tools if tool.name == "create_card")
        reference_time = datetime.now(ZoneInfo(settings.tz))
        expected_date = f"{reference_time.year}-09-25"
        cases = [
            ("小石開卡 0925 test", "test", True, expected_date),
            ("小石，僅開卡 0925 文案規劃，不需要文件", "文案規劃", False, expected_date),
            ("小石，開卡並建立文案 0925 test", "test", True, expected_date),
        ]
        cases.extend(
            (f"小石開卡 test DDL {word}", "test", True, (reference_time.date() + timedelta(days=offset)).isoformat())
            for word, offset in [("今天", 0), ("明天", 1), ("後天", 2)]
        )
        cases.extend(
            (text, title, True, reference_time.date().isoformat())
            for text, title in [
                ("小石開卡 開源築夢計畫 DDL 今天", "開源築夢計畫"),
                ("小石開卡 活動公告 ddl: 今天", "活動公告"),
                ("小石開卡 活動公告 deadline：今天", "活動公告"),
                ("小石開卡 活動公告DDL今天", "活動公告"),
                ("小石開卡 SQL DDL DDL 今天", "SQL DDL"),
                ("小石開卡「SQL DDL 今天」到期日今天", "SQL DDL 今天"),
            ]
        )
        for text, title, document, deadline in cases:
            compact = await build_llm_client(settings).chat(
                system=await PromptBuilder(settings, store).build(chat_id=-1, reference_time=reference_time),
                messages=[Message("user", [TextBlock(text)])],
                tools=[create.spec(), ASK_USER_SPEC],
                thinking=settings.llm_thinking,
            )
            assert len(compact.tool_calls) == 1 and compact.tool_calls[0].name == "create_card", "可確定的日期不應補問"
            parsed = create.args_model.model_validate(compact.tool_calls[0].arguments)
            assert parsed.title == title and parsed.due_date == deadline, f"到期日或標題解析錯誤：{text}"
            assert parsed.document_card is document, f"開卡模式解析錯誤：{text}"
            validate_explicit_date(text, parsed.due_date, settings.tz, reference_time=reference_time)
        # Reproduce the reported command, resolving the real assignee without executing create_card.
        resolver = next(tool for tool in tools if tool.name == "resolve_member")
        text = "@sitcon_editorial_helper_bot 開卡 開源築夢計畫 DDL 今天 @wolfdayo"
        ctx = ToolContext(-1, None, settings.telegram_admin_id, None, text, reference_time=reference_time)
        system = await PromptBuilder(settings, store).build(chat_id=-1, reference_time=reference_time)
        messages = [Message("user", [TextBlock(text)])]
        specs = [resolver.spec(), create.spec(), ASK_USER_SPEC]
        proposal = await build_llm_client(settings).chat(
            system=system, messages=messages, tools=specs, thinking=settings.llm_thinking
        )
        assert len(proposal.tool_calls) == 1 and proposal.tool_calls[0].name == "resolve_member", (
            "已指定負責人應先查名冊，不補問或猜 ID"
        )
        call = proposal.tool_calls[0]
        person = resolver.args_model.model_validate(call.arguments)
        assert person.query.lstrip("@").casefold() == "wolfdayo", "負責人解析錯誤"
        result = await resolver.run(person, ctx)
        messages.extend([proposal.assistant_message(), Message("user", [ToolResultBlock(call.id, result)])])
        proposal = await build_llm_client(settings).chat(
            system=system, messages=messages, tools=specs, thinking=settings.llm_thinking
        )
        assert len(proposal.tool_calls) == 1 and proposal.tool_calls[0].name == "create_card", (
            "標題、日期及負責人完整時應開卡，不重複補問"
        )
        parsed = create.args_model.model_validate(proposal.tool_calls[0].arguments)
        member = await editorial.resolve_member(person, ctx)
        assert parsed.title == "開源築夢計畫", "DDL 或負責人被錯誤放進標題"
        assert parsed.due_date == reference_time.date().isoformat(), "今天未依本輪時間解析"
        assert parsed.assignee_ids == [member["gitlab_id"]], "未使用名冊中的負責人 ID"
        assert parsed.document_card is True, "一般開卡應預設為文案卡"
        add_folder = next(tool for tool in tools if tool.name == "create_card_folder")
        add_document = next(tool for tool in tools if tool.name == "create_card_document")
        resume = next(tool for tool in tools if tool.name == "resume_operation")
        for request in ("小石，幫 #682 補建資料夾與文案", "資料夾跟文案呢"):
            proposal = await build_llm_client(settings).chat(
                system=await PromptBuilder(settings, store).build(chat_id=-1),
                messages=[
                    Message("user", [TextBlock("小石，只開卡 1005 開源築夢計畫")]),
                    Message("assistant", [TextBlock("已建立 #682，只有 GitLab 卡片。")]),
                    Message("user", [TextBlock(request)]),
                ],
                tools=[create.spec(), add_folder.spec(), add_document.spec(), resume.spec(), ASK_USER_SPEC],
                thinking=settings.llm_thinking,
            )
            names = {call.name for call in proposal.tool_calls}
            assert names and names <= {"create_card_folder", "create_card_document"}, (
                "既有卡片補建應使用專用工具，不重開卡或猜測操作 ID"
            )
            for call in proposal.tool_calls:
                assert add_document.args_model.model_validate(call.arguments).iid == 682
        print(
            json.dumps(
                {
                    "live_label_tool_call": True,
                    "ambiguous_date_asks_user": True,
                    "relative_days_resolve_without_confirmation": True,
                    "compact_date_and_title": True,
                    "deadline_markers_and_title": True,
                    "deadline_and_assignee_separate_from_title": True,
                    "card_only_and_document_modes": True,
                    "existing_card_document_tool": True,
                    "consent_button_options": True,
                    "plain_text_replies": True,
                    "remote_writes": 0,
                },
                indent=2,
            )
        )
    finally:
        await gl.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
