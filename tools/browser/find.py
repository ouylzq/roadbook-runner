"""工具 find：按文案搜可交互元素，产出引用。只读。"""
from __future__ import annotations

from typing import Any

from tools.base import Tool, ToolContext
from tools.browser.base import NO_BROWSER


def _find(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.browser is None:
        return NO_BROWSER
    return ctx.browser.find(args["query"])


FIND_TOOL = Tool(
    name="find",
    description="按文案搜可交互元素，产出引用。页面长、observe 被截断时用。",
    parameters={"query": {"type": "string"}},
    required=("query",),
    handler=_find,
)
