"""知识沉淀：把每次跑批里值得留下的东西写进 knowledge/。

三类，边界刻意分清楚：
- facts/        这个场景最近一次留下的事实（runner 交给裁判的那份）。当次真相，下次覆盖。
- experiences/  逐次追加的跑批流水：什么时候跑的、判成什么、跑了多少步。看趋势用，不覆盖。
- reflections/  agent 自己写下的教训。只在非 PASS 且有备注时记——全绿还写就成了噪音。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .domain import RunStatus
from .traces import Run


def safe_stem(scenario: str) -> str:
    """场景名 → 知识库文件名。分组名（team/login）里的斜杠换成双下划线：
    这些文件本来就按场景平铺存放，嵌套目录只会让读写两端各自发明规则。"""
    return scenario.replace("/", "__")


class KnowledgeStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def absorb(self, record: Run, run_dir: Path) -> None:
        stem = safe_stem(record.scenario)
        for sub in ("facts", "experiences", "reflections"):
            (self.directory / sub).mkdir(parents=True, exist_ok=True)

        context = run_dir / "context.yaml"
        if context.exists():
            (self.directory / "facts" / f"{stem}.yaml").write_text(
                f"# 最近一次跑批 {record.run_id} 留下的事实\n" + context.read_text(encoding="utf-8"),
                encoding="utf-8",
            )

        entry = {
            "run_id": record.run_id, "at": record.finished_at,
            "verdict": str(record.verdict), "status": str(record.status),
            "steps": len(record.steps), "judge_steps": len(record.judge_steps),
        }
        # 仅重判的记录带标记：趋势里能认出来，多条结论并排时知道哪条是复核
        if record.rejudge_of:
            entry["rejudge_of"] = record.rejudge_of
        with (self.directory / "experiences" / f"{stem}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # 重判的 notes 是从原跑批继承来的 runner 备注，再写一遍就是重复
        if record.notes and record.status is not RunStatus.PASSED and not record.rejudge_of:
            with (self.directory / "reflections" / f"{stem}.md").open("a", encoding="utf-8") as f:
                f.write(f"\n## {record.run_id} · {record.verdict or record.status}\n\n{record.notes}\n")

    def briefing(self, scenario: str, runs: int = 3, chars: int = 900) -> str:
        """给 runner 的「这条路最近怎么样」。没有历史就返回空串。

        以前 reflections/ 是**只写不读**的——agent 每次把教训写下来，然后没有任何地方
        再看它一眼。那比没有记忆更坏：它看起来像在学习。这里补上读路径。

        **facts/ 刻意不进这里。** 那是上一次跑批留下的 draft_id / project_id 这类值，
        每次跑都是新的；喂给 agent 只会诱导它复用一个已经死掉的 id。它留着给人排查用。

        **裁判也刻意拿不到这份。** 裁判的独立性靠它只读 expect.yaml + context.yaml，
        先告诉它"上次这里没通过"就是在给它定锚。
        """
        rows = self.trend(scenario, runs)
        if not rows:
            return ""
        lines = [f"- {r.get('at', '')[:10]} {r.get('verdict') or r.get('status')} · {r.get('steps', 0)} 步"
                 for r in reversed(rows)]
        out = ["## 这条路最近几次跑批（背景参考，不是这次的结论）", "", *lines]

        path = self.directory / "reflections" / f"{safe_stem(scenario)}.md"
        if path.exists():
            last = path.read_text(encoding="utf-8").split("\n## ")[-1].strip()
            if last:
                out += ["", "最近一次没通过时它自己记下的：", "",
                        last[:chars] + ("…（略）" if len(last) > chars else "")]
        return "\n".join(out) + "\n"

    def trend(self, scenario: str, limit: int = 10) -> list[dict[str, Any]]:
        """最近 N 次的结论，给「这条路最近稳不稳」用。"""
        path = self.directory / "experiences" / f"{safe_stem(scenario)}.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return rows[-limit:]
