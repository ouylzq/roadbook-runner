"""工具 click_xy：按像素坐标点击，引用定位不到时的退守手段。"""
from __future__ import annotations

from typing import Any

from tools.base import Tool, ToolContext
from tools.browser.base import NO_BROWSER


def _click_xy(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.browser is None:
        return NO_BROWSER
    result = ctx.browser.click_xy(int(args["x"]), int(args["y"]))
    return result + "\n\n" + ctx.browser.observe().render()


CLICK_XY_TOOL = Tool(
    name="click_xy",
    description="按像素坐标点击。退守手段，只在引用实在定位不到时用，并在 notes 说明原因。",
    parameters={"x": {"type": "integer"}, "y": {"type": "integer"}},
    required=("x", "y"),
    handler=_click_xy,
    mutates=True,
)
