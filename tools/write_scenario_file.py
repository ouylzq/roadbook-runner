"""write_scenario_file：写场景资产。

只允许写进技能的 scenarios/<场景名>/ 下的三个白名单文件，越界一律拒绝。
ctx.scenario 不为空时进一步锁死只能写这一个场景——实测 2026-09-16 有一次 agent
拿这个工具当探针，往别人的场景目录写了个 placeholder 去试路径存不存在。
写权限给 LLM，边界就得是代码卡的。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Tool, ToolContext, legal_scenario_name

ALLOWED_FILES = ("expect.yaml", "intent.md")

def _write_scenario_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.skill is None:
        return "当前没有加载技能"
    scenario = str(args.get("scenario") or "").strip()
    filename = str(args.get("filename") or "").strip()
    content = str(args.get("content") or "")

    if ctx.scenario and scenario != ctx.scenario:
        return (f"拒绝：这次只允许写场景 {ctx.scenario}，不能写 {scenario}。"
                f"想看别的场景的文件用 read_resource，别用写工具探路。")
    if not legal_scenario_name(scenario):
        return f"场景名不合法: {scenario!r}"
    if filename not in ALLOWED_FILES:
        return f"只允许写 {list(ALLOWED_FILES)}，不允许 {filename!r}"

    scenarios_root = (ctx.skill.root / "scenarios").resolve()
    target_dir = (scenarios_root / scenario).resolve()
    if not _inside(target_dir, scenarios_root):
        return "拒绝写到 scenarios/ 之外"

    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    verb = "覆盖" if target.exists() else "创建"
    target.write_text(content, encoding="utf-8")
    return f"已{verb} scenarios/{scenario}/{filename}（{len(content)} 字）"


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


WRITE_SCENARIO_FILE_TOOL = Tool(
    name="write_scenario_file",
    description=(
        "把完整内容写进某个场景的 intent.md / expect.yaml。playbook 必须用 upsert_playbook_step 单步更新。"
        "写之前先 read_resource 看一份同类场景的现有文件，照着它的结构和语气写。"
    ),
    parameters={
        "scenario": {"type": "string", "description": "场景目录名，小写短横线，如 todo-delete"},
        "filename": {"type": "string", "enum": list(ALLOWED_FILES)},
        "content": {"type": "string", "description": "文件完整内容（覆盖写）"},
    },
    required=("scenario", "filename", "content"),
    handler=_write_scenario_file,
)
