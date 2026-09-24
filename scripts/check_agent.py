"""Live read-only acceptance: all mutating tools are blocked at the execution boundary.

Run inside Compose with the source tree mounted at /checks. Never sends Telegram messages.
"""

import asyncio
import json

from editorial_bot.agent.core import Agent, AgentRequest
from editorial_bot.agent.prompts import PromptBuilder
from editorial_bot.agent.tools.base import ToolRegistry
from editorial_bot.agent.tools.editorial import EditorialTools
from editorial_bot.agent.tools.search_tools import build_search_tools
from editorial_bot.logging_setup import configure
from editorial_bot.services.gitlab import GitLab
from editorial_bot.services.google import Google
from editorial_bot.services.knowledge import Knowledge
from editorial_bot.services.llm.base import build_llm_client
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
        tools = editorial.build() + build_search_tools(build_web_search_service(settings))
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
        assert "gitlab_list_labels" in (status.detail or {}).get("tools", []), "狀態回答未查詢實際標籤"
        assert date.status == "clarify", "相對日期未補問"
        assert not attempted_writes, f"意外要求寫入工具：{attempted_writes}"
        print(json.dumps({"live_label_tool_call": True, "relative_date_asks_user": True, "remote_writes": 0}, indent=2))
    finally:
        await gl.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
