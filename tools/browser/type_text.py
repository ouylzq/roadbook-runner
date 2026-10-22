"""工具 type_text：在输入框里打字。动作型，跑完自动重新观察。"""
from __future__ import annotations

from typing import Any

from tools.base import Tool, ToolContext
from tools.browser.base import NO_BROWSER


def _type_text(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.browser is None:
        return NO_BROWSER
    result = ctx.browser.type_text(args["ref"], args["text"], submit=bool(args.get("submit")))
    return result + "\n\n" + ctx.browser.observe().render()


TYPE_TEXT_TOOL = Tool(
    name="type_text",
    description="在输入框里打字（模拟真实键盘，受控组件只认这个）。",
    parameters={
        "ref": {"type": "string"},
        "text": {"type": "string"},
        "submit": {"type": "boolean", "description": "打完是否回车提交"},
    },
    required=("ref", "text"),
    handler=_type_text,
    mutates=True,
)
