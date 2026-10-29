"""run_skill_script：跑技能自带的脚本。

有些事浏览器做不到——取邮箱验证码、清理测试数据、塞夹具。
roadbook 这类技能本来就把这些放在自己的 scripts/ 下，这里开放给 agent 调用。

边界由代码卡死：只能跑 <技能根>/scripts/ 下的文件；不走 shell（subprocess 列表形式），
参数里带什么都注入不进来；有超时，卡住不拖死整轮。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from .base import Tool, ToolContext

SCRIPT_TIMEOUT_SECONDS = 60
STDOUT_MAX_CHARS = 4000
STDERR_MAX_CHARS = 1500


def _run_skill_script(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.skill is None:
        return "当前没有加载技能"
    name = str(args.get("script") or "").strip()
    if not name or "/" in name or name.startswith("."):
        return f"脚本名不合法: {name!r}（只给文件名，不要带路径）"

    scripts_dir = (ctx.skill.root / "scripts").resolve()
    script = (scripts_dir / name).resolve()
    if not _inside(script, scripts_dir):
        return "拒绝：只能跑技能 scripts/ 目录下的脚本"
    if not script.is_file():
        available = sorted(p.name for p in scripts_dir.iterdir()) if scripts_dir.is_dir() else []
        return f"没有这个脚本: {name}。这个技能有：{available}"

    argv = [str(a) for a in (args.get("args") or [])]
    runner = [sys.executable] if script.suffix == ".py" else ["bash"]
    try:
        proc = subprocess.run(
            [*runner, str(script), *argv],
            capture_output=True, text=True, timeout=SCRIPT_TIMEOUT_SECONDS, cwd=str(ctx.skill.root),
        )
    except subprocess.TimeoutExpired:
        return f"脚本超时（{SCRIPT_TIMEOUT_SECONDS}s）：{name}"

    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    lines = [f"退出码 {proc.returncode}", f"输出:\n{stdout[:STDOUT_MAX_CHARS]}" if stdout else "（没有输出）"]
    if stderr:
        lines.append(f"stderr:\n{stderr[:STDERR_MAX_CHARS]}")
    return "\n".join(lines)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


RUN_SKILL_SCRIPT_TOOL = Tool(
    name="run_skill_script",
    description=(
        "跑当前技能 scripts/ 目录下的一个脚本，拿回它的输出。用于浏览器做不到的事（比如取邮箱验证码）。"
        "脚本怎么用、要传什么参数，看技能资源里的说明——不要瞎猜参数。"
    ),
    parameters={
        "script": {"type": "string", "description": "脚本文件名，如 fetch_snapshot.py"},
        "args": {"type": "array", "items": {"type": "string"}, "description": "命令行参数，按顺序给"},
    },
    required=("script",),
    handler=_run_skill_script,
)
