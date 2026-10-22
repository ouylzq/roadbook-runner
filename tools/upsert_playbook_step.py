"""upsert_playbook_step：只提交一个已验证步骤，由代码生成合规 Markdown。

模型不再整份重写 playbook；短参数避免输出截断，固定渲染器保证五要素齐全。
"""
from __future__ import annotations

import contextlib
import os
import re
import tempfile
import threading
from datetime import date
from typing import Any

from .base import Tool, ToolContext

STEP = re.compile(r"(?m)^(\d+)\.\s+【参考】")
STEPS_HEADING = "## 步骤"
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _upsert(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.skill is None:
        return "当前没有加载技能"
    scenario = str(args["scenario"])
    if ctx.scenario and scenario != ctx.scenario:
        return f"拒绝：这次只允许写场景 {ctx.scenario}，不能写 {scenario}"
    if not scenario or "/" in scenario or scenario.startswith("."):
        return f"场景名不合法: {scenario!r}"
    if ctx.playbook_step_sink:
        return ctx.playbook_step_sink(args)
    return apply_playbook_step(ctx.skill.root, ctx.scenario, args)


def apply_playbook_step(skill_root: Any, write_scope: str | None, args: dict[str, Any]) -> str:
    scenario = str(args["scenario"])
    if write_scope and scenario != write_scope:
        return f"拒绝：这次只允许写场景 {write_scope}，不能写 {scenario}"
    target = skill_root / "scenarios" / scenario / "playbook.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_for(target)
    with lock:
        current = target.read_text(encoding="utf-8") if target.exists() else _empty_playbook(scenario)
        rendered = _render_step(args)
        updated, action = _replace_or_append(current, int(args["step_number"]), rendered)
        updated = _refresh_date(updated)
        _atomic_write(target, updated)
    return f"已{action} playbook 第 {args['step_number']} 步（结构化渲染，五要素已校验）"


def _lock_for(target: Any) -> threading.Lock:
    key = str(target.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _refresh_date(text: str) -> str:
    today = date.today().isoformat()
    return re.sub(r"(?m)^> 最后勘路：[^\n]*", f"> 最后勘路：{today}", text, count=1)


def _empty_playbook(scenario: str) -> str:
    return f"# {scenario} 路书\n> 最后勘路：{date.today().isoformat()}\n\n{STEPS_HEADING}\n"


def _render_step(args: dict[str, Any]) -> str:
    lines = [
        f"{args['step_number']}. 【参考】{str(args['reference']).strip() or '—'}",
        f"   【目的】{str(args['goal']).strip()}",
        f"   【锚点】{str(args['anchor']).strip()}",
        f"   【动作】{str(args['action']).strip()}",
        f"   【到位标志】{str(args['done']).strip()}",
    ]
    notes = str(args.get("notes") or "").strip()
    if notes:
        lines.extend(f"   {line}" for line in notes.splitlines())
    return "\n".join(lines) + "\n"


def _replace_or_append(text: str, number: int, block: str) -> tuple[str, str]:
    if STEPS_HEADING not in text:
        text = text.rstrip() + f"\n\n{STEPS_HEADING}\n"
    heading_at = text.index(STEPS_HEADING) + len(STEPS_HEADING)
    tail_at = text.find("\n## ", heading_at)
    end = len(text) if tail_at < 0 else tail_at
    body = text[heading_at:end]
    matches = list(STEP.finditer(body))
    for index, match in enumerate(matches):
        if int(match.group(1)) != number:
            continue
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        new_body = body[:match.start()].rstrip() + "\n\n" + block.rstrip() + "\n" + body[block_end:].lstrip("\n")
        return text[:heading_at] + new_body + text[end:], "更新"
    expected = max((int(match.group(1)) for match in matches), default=0) + 1
    if number != expected:
        raise ValueError(f"只能更新已有步骤，或追加第 {expected} 步；不能直接写第 {number} 步")
    new_body = body.rstrip() + "\n\n" + block.rstrip() + "\n"
    return text[:heading_at] + new_body + text[end:], "追加"


def _atomic_write(target: Any, content: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, target)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


UPSERT_PLAYBOOK_STEP_TOOL = Tool(
    name="upsert_playbook_step",
    description="新增或更新一个已经实际验证通过的路书步骤。只传单步字段，代码负责生成合规 Markdown。",
    parameters={
        "scenario": {"type": "string", "minLength": 1},
        "step_number": {"type": "integer", "minimum": 1},
        "reference": {"type": "string", "minLength": 1},
        "goal": {"type": "string", "minLength": 1},
        "anchor": {"type": "string", "minLength": 1},
        "action": {"type": "string", "minLength": 1},
        "done": {"type": "string", "minLength": 1},
        "notes": {"type": "string"},
    },
    required=("scenario", "step_number", "reference", "goal", "anchor", "action", "done"),
    handler=_upsert,
)
