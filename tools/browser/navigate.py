"""工具 navigate：打开网址。动作型，跑完自动重新观察。"""
from __future__ import annotations

from typing import Any

from tools.base import Tool, ToolContext
from tools.browser.base import NO_BROWSER


def _navigate(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.browser is None:
        return NO_BROWSER
    result = ctx.browser.navigate(args["url"])
    return result + "\n\n" + ctx.browser.observe().render()


NAVIGATE_TOOL = Tool(
    name="navigate",
    description="打开一个网址。写操作后回读、重载都靠它。",
    parameters={"url": {"type": "string"}},
    required=("url",),
    handler=_navigate,
    mutates=True,
)
