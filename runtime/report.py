"""跑批报告：一次跑批一份 markdown，人能直接读、也能贴进群里。"""
from __future__ import annotations

from pathlib import Path

from .traces import Run

ARGS_PREVIEW_CHARS = 100


class ReportWriter:
    def __init__(self, directory: Path):
        self.directory = directory

    def path_for(self, run_id: str) -> Path:
        return self.directory / f"{run_id}.md"

    def write(self, record: Run, run_dir: Path) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# {record.scenario} · {record.verdict or record.status}",
            "",
            f"- 跑批：`{record.run_id}`",
            f"- 技能：{record.skill} ｜ 模式：{record.mode}",
            f"- 时间：{record.started_at} → {record.finished_at}",
            f"- 步数：执行 {len(record.steps)} ／ 裁判 {len(record.judge_steps)}",
            "",
            "## 结论", "", record.summary or "（无）", "",
        ]
        if record.notes:
            lines += ["## 执行备注", "", record.notes, ""]
        if record.error:
            lines += ["## 错误", "", f"```\n{record.error}\n```", ""]
        if record.judge_output:
            lines += ["## 判分明细", "", f"```\n{record.judge_output.strip()}\n```", ""]
        lines += ["## 执行轨迹", "", *_trace_lines(record.steps)]
        if record.judge_steps:
            lines += ["", "## 裁判轨迹", "", *_trace_lines(record.judge_steps)]
        judgment = run_dir / "judgment.yaml"
        if judgment.exists():
            lines += ["", "## judgment.yaml", "", f"```yaml\n{judgment.read_text(encoding='utf-8')}\n```"]

        target = self.path_for(record.run_id)
        target.write_text("\n".join(lines), encoding="utf-8")
        return target


def _trace_lines(steps: list[dict]) -> list[str]:
    return [f"{s['n']}. `{s['tool']}` {str(s.get('args', ''))[:ARGS_PREVIEW_CHARS]}" for s in steps]
