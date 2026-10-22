"""工具 scroll：滚动页面。动作型，跑完自动重新观察。"""
from __future__ import annotations

from typing import Any

from tools.base import Tool, ToolContext
from tools.browser.base import NO_BROWSER


def _scroll(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.browser is None:
        return NO_BROWSER
    result = ctx.browser.scroll(args.get("direction", "down"), int(args.get("amount", 3)))
    return result + "\n\n" + ctx.browser.observe().render()


SCROLL_TOOL = Tool(
    name="scroll",
    description="滚动页面，滚完自动等 1.2 秒（动画中间帧不可信）。",
    parameters={
        "direction": {"type": "string", "enum": ["down", "up"]},
        "amount": {"type": "integer"},
    },
    required=("direction",),
    handler=_scroll,
    mutates=True,
)
