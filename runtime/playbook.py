"""路书（playbook.md）的结构化读写。人在看板上改的就是这份。

路书不是纯数据，是「一段说明 + 一串结构化步骤 + 一段观察」的混合体。所以这里只把
「## 步骤」那一段拆成字段，前后的散文原样留着——不解析、不重排、不丢。
往返（读出来再写回去）对现有路书必须是幂等的，`tests/test_playbook.py` 拿真实路书锁这条。

步骤的五要素和 roadbook 的 `tests/lint_playbook.py` 是同一套契约：
【参考】必填（没有就写 —），【目的】【锚点】【动作】【到位标志】四件齐全。
结构化编辑的意义就在这儿——存盘前卡住，写不出合法路书。
"""
from __future__ import annotations

import re
from typing import Any

from .domain import Scenario

PLAYBOOK_FILE = "playbook.md"
STEPS_HEADING = "## 步骤"
STEP_START = re.compile(r"^(\d+)\.\s", re.M)
FIELDS = (("ref", "参考"), ("goal", "目的"), ("anchor", "锚点"),
          ("action", "动作"), ("done", "到位标志"))
REQUIRED = ("goal", "anchor", "action", "done")


class PlaybookError(ValueError):
    """存不进去的路书：缺步骤，或者某步缺了五要素里的必填项。"""


HEADING_RE = re.compile(rf"^{re.escape(STEPS_HEADING)}\s*$", re.M)


def _split(text: str) -> tuple[str, str, str]:
    """切成（步骤段之前、步骤段、步骤段之后）。

    只认独占一行的「## 步骤」标题——散文里提到这四个字不算（自测踩到过）。
    """
    m = HEADING_RE.search(text)
    if not m:
        return text, "", ""
    head, body = text[:m.start()], text[m.end():]
    if "\n## " in body:
        steps_body, rest = body.split("\n## ", 1)
        return head, steps_body, "\n## " + rest
    return head, body, ""


def _parse_step(raw: str) -> dict[str, Any]:
    """一步的全文 → 字段。按行走：带【标记】的行开一个字段，后面不带标记的行是它的续行。

    续行必须留在所属字段里（原样，含缩进）——早先把续行统一收进 notes，
    重新渲染时它们会跑到步骤末尾，顺序和缩进全变（实测 5 份真实路书被改坏）。
    """
    step: dict[str, Any] = {key: "" for key, _ in FIELDS}
    notes: list[str] = []
    current: str | None = None
    for i, line in enumerate(raw.rstrip("\n").split("\n")):
        body = STEP_START.sub("", line, count=1) if i == 0 else line
        stripped = body.strip()
        marker = next((k for k, label in FIELDS if stripped.startswith(f"【{label}】")), None)
        if marker:
            current = marker
            step[marker] = stripped[len(dict(FIELDS)[marker]) + 2:]
        elif current:
            step[current] += "\n" + line          # 续行原样保留，缩进不动
        elif stripped:
            notes.append(line)
    step["notes"] = "\n".join(notes)
    return step


def load_playbook(scenario: Scenario) -> dict[str, Any]:
    """读出结构。`structured` 说明这份路书能不能安全地结构化编辑——

    判据是硬的：解析完再渲染回去，必须和原文逐字一致。不一致就说明这份路书的写法
    超出了解析器的理解范围（实测真有：有的没有「## 步骤」段，有的用【到位】而不是
    【到位标志】），这时候只能按纯文本改，绝不能拿结构化编辑器去动它——那会改坏资产。
    """
    path = scenario.dir / PLAYBOOK_FILE
    if not path.exists():
        return {"exists": False, "structured": False, "raw": "",
                "preamble": "", "steps": [], "trailing": ""}
    text = path.read_text(encoding="utf-8")
    preamble, steps_body, trailing = _split(text)

    marks = [(m.start(), m.group(1)) for m in STEP_START.finditer(steps_body)]
    steps = []
    for i, (start, num) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(steps_body)
        steps.append({"n": int(num), **_parse_step(steps_body[start:end])})

    head, tail = preamble.rstrip("\n"), trailing.strip("\n")
    return {"exists": True, "raw": text, "preamble": head, "steps": steps, "trailing": tail,
            "structured": bool(steps) and render_playbook(head, steps, tail) == text}


def render_playbook(preamble: str, steps: list[dict[str, Any]], trailing: str) -> str:
    out = [preamble.rstrip("\n"), "", STEPS_HEADING, ""]
    for i, step in enumerate(steps, 1):
        lines = [f"{i}. 【参考】{str(step.get('ref') or '—').strip()}"]
        lines += [f"   【{label}】{step.get(key) or ''}"
                  for key, label in FIELDS if key != "ref"]
        lines += [ln for ln in str(step.get("notes") or "").split("\n") if ln.strip()]
        out.append("\n".join(lines))
        out.append("")
    tail = trailing.strip("\n")
    return "\n".join(out).rstrip("\n") + ("\n\n" + tail if tail else "") + "\n"


def save_playbook(scenario: Scenario, preamble: str, raw_steps: list[dict[str, Any]],
                  trailing: str) -> dict[str, Any]:
    steps = [s for s in raw_steps
             if any(str(s.get(k) or "").strip() for k, _ in FIELDS)]
    if not steps:
        raise PlaybookError("至少要有一步")
    for i, step in enumerate(steps, 1):
        missing = [dict(FIELDS)[k] for k in REQUIRED if not str(step.get(k) or "").strip()]
        if missing:
            raise PlaybookError(f"第 {i} 步缺：{'、'.join('【' + m + '】' for m in missing)}"
                                "——四要素齐全才是勘明白了（lint_playbook.py 也会拦）")
    (scenario.dir / PLAYBOOK_FILE).write_text(
        render_playbook(preamble, steps, trailing), encoding="utf-8")
    return {"ok": True, "path": f"scenarios/{scenario.name}/{PLAYBOOK_FILE}", "steps": len(steps)}


def count_steps(text: str) -> int:
    """路书里记了几步。比 load_playbook 宽松：只数「## 步骤」段下 `N. ` 开头的条目。

    为什么不复用 load_playbook：那个只在 parse→render 字节还原时才认结构（那道安全阀
    是给**编辑**用的，改坏资产的代价太大）。数步数只是读，8 份真实路书里有 4 份过不了
    那道阀，但它们照样记着实实在在的步骤，不该被算成 0。
    """
    if not text.strip():
        return 0
    _, body, _ = _split(text)
    return len(STEP_START.findall(body or text))
