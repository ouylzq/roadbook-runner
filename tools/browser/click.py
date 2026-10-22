"""工具 click：按引用点击。动作型，跑完自动重新观察。"""
from __future__ import annotations

from typing import Any

from tools.base import Tool, ToolContext
from tools.browser.base import NO_BROWSER


def _click(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.browser is None:
        return NO_BROWSER
    result = ctx.browser.click_ref(args["ref"])
    return result + "\n\n" + ctx.browser.observe().render()


CLICK_TOOL = Tool(
    name="click",
    description="按引用点击。默认驱动方式，引用来自 observe/find，页面一变就失效。",
    parameters={"ref": {"type": "string", "description": "形如 ref_12"}},
    required=("ref",),
    handler=_click,
    mutates=True,
)
