"""工具 observe：重新观察当前页面。"""
from __future__ import annotations

from typing import Any

from tools.base import Tool, ToolContext
from tools.browser.base import NO_BROWSER


def _observe(ctx: ToolContext, _args: dict[str, Any]) -> str:
    if ctx.browser is None:
        return NO_BROWSER
    return ctx.browser.observe_text()   # 跟上次一模一样时只回一句话，不重复灌整棵树


OBSERVE_TOOL = Tool(
    name="observe",
    description="重新观察当前页面：返回页面结构（可交互元素带 {ref_N}）和可见文本。",
    handler=_observe,
)
