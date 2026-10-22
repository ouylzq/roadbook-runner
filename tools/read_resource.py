"""read_resource：按需读取当前技能里的资源。渐进式披露的落地点。"""
from __future__ import annotations

from typing import Any

from .base import Tool, ToolContext


def _read_resource(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.skill is None:
        return "当前没有加载技能"
    return ctx.skill.read(str(args.get("path", "")))


READ_RESOURCE_TOOL = Tool(
    name="read_resource",
    description=(
        "读取当前技能里的一份资源（路径见系统提示词里的资源清单）。"
        "原子 atoms/*.md、规则 references/*.md、别的场景的样板都用它取。不要凭空猜内容。"
    ),
    parameters={"path": {"type": "string", "description": "相对技能根目录，如 atoms/add-task.md"}},
    required=("path",),
    handler=_read_resource,
)
