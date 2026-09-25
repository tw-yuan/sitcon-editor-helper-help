"""Live read-only acceptance: all mutating tools are blocked at the execution boundary.

Run inside Compose with the source tree mounted at /checks. Never sends Telegram messages.
"""

import asyncio
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from editorial_bot.agent.core import ASK_USER_SPEC, Agent, AgentRequest
from editorial_bot.agent.prompts import PromptBuilder
from editorial_bot.agent.tools.base import ToolRegistry
from editorial_bot.agent.tools.editorial import EditorialTools
from editorial_bot.agent.tools.reaction_tools import build_reaction_tools
from editorial_bot.agent.tools.search_tools import build_search_tools
from editorial_bot.logging_setup import configure
from editorial_bot.services.dates import validate_explicit_date
from editorial_bot.services.gitlab import GitLab
from editorial_bot.services.google import Google
from editorial_bot.services.knowledge import Knowledge
from editorial_bot.services.llm.base import Message, TextBlock, build_llm_client
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
                "小石，幫我開一張文案卡：測試公告，明天到期。",
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
        assert date.status == "clarify", "相對日期未補問"
        for result in (status, date, consent):
            assert not re.search(
                r"(?m)^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s)|`|\*\*|\[[^\]]+\]\([^)]+\)|</?[A-Za-z][^>]*>", result.reply
            ), "回覆包含 Markdown 或 HTML 語法"
        assert not attempted_writes, f"意外要求寫入工具：{attempted_writes}"
        # Inspect the live model's proposed arguments only. Never execute these tool calls.
        create = next(tool for tool in tools if tool.name == "create_card")
        expected_date = f"{datetime.now(ZoneInfo(settings.tz)).year}-09-25"
        cases = [
            ("小石開卡 0925 test", "test", False),
            ("小石，僅開卡 0925 文案規劃，不需要文件", "文案規劃", False),
            ("小石，開卡並建立文案 0925 test", "test", True),
        ]
        for text, title, document in cases:
            compact = await build_llm_client(settings).chat(
                system=await PromptBuilder(settings, store).build(chat_id=-1),
                messages=[Message("user", [TextBlock(text)])],
                tools=[create.spec(), ASK_USER_SPEC],
                thinking=settings.llm_thinking,
            )
            assert len(compact.tool_calls) == 1 and compact.tool_calls[0].name == "create_card", "MMDD 格式不應補問"
            parsed = create.args_model.model_validate(compact.tool_calls[0].arguments)
            assert parsed.title == title and parsed.due_date == expected_date, "MMDD 到期日或標題解析錯誤"
            assert parsed.document is document, f"開卡模式解析錯誤：{text}"
            validate_explicit_date(text, parsed.due_date, settings.tz)
        print(
            json.dumps(
                {
                    "live_label_tool_call": True,
                    "relative_date_asks_user": True,
                    "compact_date_and_title": True,
                    "card_only_and_document_modes": True,
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
