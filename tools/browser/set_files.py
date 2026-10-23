"""工具 set_files：把技能目录内的本地文件直接装进文件输入框。

字节走 Playwright 的 setInputFiles，不经过模型输出——LLM 手抄 base64 会损坏
（2026-10-22 实测两次转抄全坏），长文件还会撞单次输出上限。路径限定在技能根内，
由 skills 的路径沙箱兜底；目标是文件输入框（input[type=file]）之外的一律拒绝由
Playwright 自己报错。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.base import Tool, ToolContext
from tools.browser.base import NO_BROWSER


def _set_files(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.browser is None:
        return NO_BROWSER
    if ctx.skill is None:
        return "当前没有加载技能，无法解析夹具路径"
    ref = str(args.get("ref") or "").strip()
    selector = str(args.get("selector") or "").strip()
    if bool(ref) == bool(selector):
        return ("ref 和 selector 二选一。文件输入框常是隐藏的（不进 observe、拿不到引用），"
                "此时直接给 selector，如 input[type=file]（多个匹配自动取第一个）")
    root = Path(ctx.skill.root).resolve()
    paths: list[str] = []
    for raw in args["paths"]:
        target = (root / raw).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return f"拒绝：文件必须在技能目录内（{raw!r}）"
        if not target.is_file():
            return f"文件不存在: {raw}（夹具在 references/fixtures/ 下，先看清楚再引用）"
        paths.append(str(target))
    result = ctx.browser.set_files(ref or None, selector or None, paths)
    return result + "\n\n" + ctx.browser.observe().render()


SET_FILES_TOOL = Tool(
    name="set_files",
    description="把技能目录内的本地文件直接装进文件输入框。字节不经过模型输出，"
    "是传 docx/pdf 等夹具文件的唯一正规路径；别用 eval_js 手抄 base64。"
    "文件输入框常是隐藏的（不进 observe），拿不到引用就给 selector。",
    parameters={
        "ref": {"type": "string", "description": "文件输入框的引用（可见时用），形如 ref_12"},
        "selector": {
            "type": "string",
            "description": "CSS 选择器（隐藏输入框用这条），如 input[type=file]；多个匹配自动取第一个",
        },
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "技能根的相对路径，如 references/fixtures/onboarding-reg/宠物食品访谈大纲.docx；可多个",
        },
    },
    required=("paths",),
    handler=_set_files,
    mutates=True,
)
