"""工具 wait：等待若干秒。动作型：等的过程中页面可能变，跑完重新观察。"""
from __future__ import annotations

from typing import Any

from tools.base import Tool, ToolContext
from tools.browser.base import NO_BROWSER


def _wait(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.browser is None:
        return NO_BROWSER
    result = ctx.browser.wait(float(args["seconds"]))
    return result + "\n\n" + ctx.browser.observe().render()


WAIT_TOOL = Tool(
    name="wait",
    description="等待若干秒。弹层先进 DOM 后绘制、滚动有动画，都需要等。",
    parameters={"seconds": {"type": "number"}},
    required=("seconds",),
    handler=_wait,
    mutates=True,
)
