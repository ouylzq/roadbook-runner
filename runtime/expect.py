"""验收路线（expect.yaml）的结构化读写。人在看板上改的就是这份。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .domain import Scenario

EXPECT_FILE = "expect.yaml"
HEADER = "# 验收路线。裁判照着它逐条取证——改这份就等于改验收标准。\n\n"


REF_RE = re.compile(r"\bcontext\.([A-Za-z_][A-Za-z0-9_]*)")
NOT_A_KEY = frozenset({"yaml", "yml", "xxx"})       # 「context.yaml」是文件名，不是引用


def required_context_keys(scenario: Scenario) -> list[str]:
    """验收路线引用了哪些 `context.xxx`——也就是 runner 必须在 finish 的 facts 里交出的键。

    这些键静态可知：它们就写在 expect.yaml 里。judge.py 也用同一个正则提，但那是**事后**提的，
    用来产出「context.yaml 里没有 xxx」这条 BLOCK。20 次历史跑批里这一条占了 4 次失败，
    是最大单一原因——同一个正则提前两分钟跑，就能在提示词里点名、并在 finish 时当场拦住。
    """
    data = load_expect(scenario)
    keys: set[str] = set()
    for step in data.get("steps") or []:
        for text in [step.get("go", ""), *(step.get("check") or [])]:
            keys.update(k for k in REF_RE.findall(str(text)) if k not in NOT_A_KEY)
    return sorted(keys)


class EmptyRouteError(ValueError):
    """连一步都没有的验收路线没有意义，拒绝落盘。"""


def load_expect(scenario: Scenario) -> dict[str, Any]:
    path = scenario.dir / EXPECT_FILE
    if not path.exists():
        return {"goal": "", "steps": [], "exists": False}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {"goal": data.get("goal", ""), "steps": data.get("steps", []), "exists": True}


def normalize_steps(raw_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """去掉空白 check 和完全空白的步骤；保留顺序。"""
    steps = []
    for step in raw_steps:
        go = str(step.get("go") or "").strip()
        checks = [str(c).strip() for c in (step.get("check") or []) if c and str(c).strip()]
        if go or checks:
            steps.append({"go": go, "check": checks})
    return steps


def _existing_header(path: Path) -> str:
    """文件开头那段 `#` 注释原样留住。

    人会在 expect.yaml 顶部写「为什么门槛是至少一条」这类判据设计的来龙去脉
    （attachment-onboarding 那份有 17 行）。以前存盘一律换成固定的一行头，
    2026-09-20 在看板上「原样保存」一次就把它们全冲掉了——这是丢资产，不是格式化。
    """
    if not path.exists():
        return HEADER
    kept: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or (kept and not line.strip()):
            kept.append(line)
        else:
            break
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept) + "\n\n" if kept else HEADER


class _IndentDumper(yaml.SafeDumper):
    """让列表项缩进在父键下面（`steps:` → `  - go:`），跟人手写的 expect.yaml 一致。

    PyYAML 默认把映射里的序列顶格写（`steps:\\n- go:`），语义一样但字节不同——
    在看板上点一次「原样保存」，git 里就多一份纯缩进 diff，人会以为内容被改了。
    """

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)


def render_expect(goal: str, steps: list[dict[str, Any]]) -> str:
    body = yaml.dump({"goal": goal.strip(), "steps": steps}, Dumper=_IndentDumper,
                     allow_unicode=True, sort_keys=False, default_flow_style=False, width=10**6)
    # goal 和 steps 之间、步骤之间各空一行，跟手写的一致；第一步紧跟 `steps:` 不空
    head, *rest = body.split("\n  - go:")
    head = head.replace("\nsteps:", "\n\nsteps:", 1)
    return head + "".join(f"\n{'' if i == 0 else chr(10)}  - go:{part}" for i, part in enumerate(rest))


def save_expect(scenario: Scenario, goal: str, raw_steps: list[dict[str, Any]]) -> dict[str, Any]:
    steps = normalize_steps(raw_steps)
    if not steps:
        raise EmptyRouteError("至少要有一步")
    path = scenario.dir / EXPECT_FILE
    path.write_text(_existing_header(path) + render_expect(goal, steps), encoding="utf-8")
    return {"ok": True, "path": f"scenarios/{scenario.name}/{EXPECT_FILE}",
            "steps": len(steps), "checks": sum(len(s["check"]) for s in steps)}
