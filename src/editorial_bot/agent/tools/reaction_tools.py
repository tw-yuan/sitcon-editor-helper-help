"""Let the agent request a heart on the triggering message after its reply arrives."""

from pydantic import BaseModel, ConfigDict

from .base import Tool, ToolContext

HEART = "❤"


class ReactHeartArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReactHeartTool(Tool):
    name = "react_heart"
    description = (
        "對本輪使用者訊息按 ❤ 愛心 reaction，適用於道謝、好消息、鼓勵或溫暖的互動，不必每則都按。"
        "照常用純文字回覆；愛心會在回覆送達後取代預設的 👍。不能指定其他訊息或其他人。無參數。"
    )
    args_model = ReactHeartArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        ctx.reaction = HEART
        return "已安排在本輪回覆送達後，對觸發訊息按 ❤。"


def build_reaction_tools() -> list[Tool]:
    return [ReactHeartTool()]
