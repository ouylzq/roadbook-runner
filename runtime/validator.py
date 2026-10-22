"""判分：把裁判交回的 judgment.yaml 换算成结论。

刻意不自己比对语义——判定由裁判 agent 给出，这里只调技能自带的判分脚本，
保证「每步都走了、每条都答了、每个答案都有原文兜底」。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from runtime.domain import RunStatus, Verdict

VERDICT: dict[int, str] = {0: Verdict.PASS, 1: Verdict.FAIL, 2: Verdict.BLOCK}
STATUS: dict[int, RunStatus] = {0: RunStatus.PASSED, 1: RunStatus.FAILED, 2: RunStatus.BLOCKED}
OUTPUT_TAIL_CHARS = 4000
WRAPPER_KEYS = ("context", "context_yaml", "context.yaml")


def score(skill_root: Path, scenario: str, run_dir: Path) -> tuple[int, str]:
    script = skill_root / "scripts" / "judge.py"
    if not script.exists():
        return 2, f"技能里没有 scripts/judge.py: {script}"
    proc = subprocess.run(
        [sys.executable, str(script), str(skill_root / "scenarios" / scenario), str(run_dir)],
        capture_output=True, text=True, cwd=str(skill_root),
    )
    return proc.returncode, (proc.stdout + proc.stderr)[-OUTPUT_TAIL_CHARS:]


def flatten_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """模型有时把事实多套一层 wrapper（实测 2026-09-16 出现过 context_yaml:）。
    判分脚本按顶层键找 context.xxx，套了就判 BLOCK——这里摊平，已有的顶层键优先。"""
    out = dict(facts)
    for key in WRAPPER_KEYS:
        inner = out.pop(key, None)
        if isinstance(inner, dict):
            out = {**inner, **out}
    return out
