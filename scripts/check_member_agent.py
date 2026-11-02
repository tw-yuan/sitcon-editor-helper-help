"""Live model acceptance using synthetic roster data; no Google, GitLab or Telegram requests.

The create tool only captures arguments and stops the agent before any write.
Run via Compose with scripts mounted at /checks; only the configured model API is contacted.
"""

import asyncio
import json
from dataclasses import asdict
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from editorial_bot.agent.context import Pending
from editorial_bot.agent.core import Agent, AgentRequest
from editorial_bot.agent.prompts import PromptBuilder
from editorial_bot.agent.tools.base import ToolRegistry
from editorial_bot.agent.tools.editorial import EditorialTools
from editorial_bot.agent.tools.external_data import wrap_external
from editorial_bot.logging_setup import configure
from editorial_bot.services.dates import validate_explicit_date
from editorial_bot.services.llm.base import Message, TextBlock, ToolResultBlock, ToolUseBlock, build_llm_client
from editorial_bot.services.sheets_roster import Member, RosterService
from editorial_bot.settings import Settings
from editorial_bot.storage.db import Store

TITLE = "製作 IG 精選動態封面"
REQUEST = f"小石開卡 {TITLE} 指定章魚 不要建文件 DDL 10/11"
OLD_ID, NEW_ID = 7036398656, 23265503


class CapturedCreate(BaseException):
    """Leave the agent before it can execute any write or report a fake success."""


def history():
    old = asdict(Member(OLD_ID, "test_octopus", nickname="章魚"))
    return [
        Message("user", [TextBlock(REQUEST)]),
        Message("assistant", [ToolUseBlock("old-roster", "resolve_member", {"query": "章魚"})]),
        Message("user", [ToolResultBlock("old-roster", wrap_external(json.dumps(old, ensure_ascii=False)))]),
        Message("assistant", [TextBlock(f"名冊把章魚的 GitLab ID 列為 {OLD_ID}，查不到帳號，尚未開卡。")]),
    ]


async def check_case(settings, store, name, req, *, stale_sheet=False, ambiguous=False):
    values = [
        ["Telegram ID", "gitlab_id", "nickname", "default", "note"],
        ["test_octopus", str(OLD_ID if stale_sheet else NEW_ID), "章魚", "yes"],
    ]
    if ambiguous:
        values.append(["test_other", "123456", "章魚", "no"])
    google = SimpleNamespace(roster_values=AsyncMock(return_value=values))
    roster = RosterService(google)
    gl = SimpleNamespace(user_username=AsyncMock(return_value="test_octopus"))
    editorial = EditorialTools(gl, google, roster, None, None, store, settings)
    tools = [t for t in editorial.build() if t.name in {"resolve_member", "create_card"}]
    queries, proposed = [], []
    resolver = next(t for t in tools if t.name == "resolve_member")

    async def resolve(args, ctx):
        queries.append(args.query)
        return await editorial.resolve_member(args, ctx)

    async def capture(args, ctx):
        validate_explicit_date(ctx.text, args.due_date, settings.tz, reference_time=ctx.reference_time)
        proposed.append(args)
        raise CapturedCreate

    resolver.fn = resolve
    next(t for t in tools if t.name == "create_card").fn = capture
    agent = Agent(
        build_llm_client(settings),
        ToolRegistry(tools),
        PromptBuilder(settings, store),
        roster,
        thinking=settings.llm_thinking,
        max_iterations=5,
    )
    result = None
    try:
        result = await agent.handle(req)
    except CapturedCreate:
        pass
    assert queries, f"{name}: 未重新查詢人員"
    if ambiguous:
        assert not proposed and result.status == "clarify", f"{name}: 同名未補問"
        assert len(result.pending.options) == 2, f"{name}: 未列出兩位候選"
    else:
        assert len(proposed) == 1, f"{name}: 未提出開卡，reply={result.reply if result else ''}"
        card = proposed[0]
        assert card.title == TITLE and card.document_card is False, f"{name}: 未遵守原標題與不要文件"
        assert card.due_date == f"{datetime.now(ZoneInfo(settings.tz)).year}-10-11", f"{name}: 原日期遺失"
        assert card.assignee_ids == [NEW_ID], f"{name}: 未採用更正後 ID"
        if stale_sheet:
            assert str(NEW_ID) in queries, f"{name}: 未查證使用者明確更正的 ID"
    print(json.dumps({"case": name, "passed": True, "remote_writes": 0}, ensure_ascii=False), flush=True)


async def main():
    settings = Settings()
    configure(settings)
    store = await Store.open(":memory:")
    try:
        cases = [
            ("nickname", AgentRequest(-1, None, 7, None, REQUEST), {}),
            (
                "old_refusal",
                AgentRequest(
                    -1,
                    None,
                    7,
                    None,
                    "你不會去翻資料表嗎明明有給你，nickname 就有章魚",
                    history=[
                        Message("user", [TextBlock(REQUEST)]),
                        Message("assistant", [TextBlock("依規則不能用暱稱猜人，請提供 Telegram username。")]),
                    ],
                ),
                {},
            ),
            (
                "sheet_updated",
                AgentRequest(-1, None, 7, None, "我已經改好資料表，請重新查詢後繼續開卡", history=history()),
                {},
            ),
            (
                "explicit_id_correction",
                AgentRequest(-1, None, 7, None, str(NEW_ID), history=history()),
                {"stale_sheet": True},
            ),
            (
                "clarification_id_correction",
                AgentRequest(
                    -1,
                    None,
                    7,
                    None,
                    str(NEW_ID),
                    resume=Pending(
                        messages=[
                            *history(),
                            Message("assistant", [ToolUseBlock("id-question", "ask_user", {"question": "正確 ID？"})]),
                        ],
                        resolved_results=[],
                        ask_user_id="id-question",
                    ),
                ),
                {"stale_sheet": True},
            ),
            ("duplicate_nickname", AgentRequest(-1, None, 7, None, REQUEST), {"ambiguous": True}),
        ]
        for name, req, kwargs in cases:
            await check_case(settings, store, name, req, **kwargs)
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
