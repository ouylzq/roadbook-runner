"""一次跑批的轨迹：每一步做了什么、最后判成什么。落盘为 run.json。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from runtime.domain import RunStatus, Verdict

RUN_FILE = "run.json"


@dataclass
class Step:
    n: int
    tool: str
    args: dict[str, Any]
    result_preview: str
    thinking: str = ""
    elapsed_ms: int = 0
    llm_seq: int = 0        # 这一步是第几次调模型决定的——一次调用可能产出好几步

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Run:
    run_id: str
    scenario: str
    skill: str
    mode: str
    status: RunStatus = RunStatus.RUNNING
    env: str = ""                     # 这次跑的是哪个环境（人在看板上选的）。空 = 场景没声明 envs
    started_at: str = ""
    finished_at: str = ""
    verdict: str = Verdict.NONE       # 一般是 Verdict 的值；判分脚本异常退出码会给 "EXIT<n>"
    summary: str = ""
    notes: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    judge_steps: list[dict[str, Any]] = field(default_factory=list)
    judge_output: str = ""
    judge_incomplete: str = ""         # 裁判没交出 judgment 时的真因（轮数耗尽等）
    runner_outcome: str = ""           # runner 这边是怎么收的口：done/blocked/needs_input/max_iterations
                                       # 有验收路线时最终状态来自裁判判决，runner 自己怎么结束的会被盖掉——
                                       # 不单独记就看不出「它自己交卷了」还是「烧光轮数被迫停的」
    playbook_written: bool = False     # 这次有没有动过路书
    # 路书是渐进式的——勘通一步记一步，所以「从第几步推到第几步」就是这次跑批的进度。
    # 只有 playbook_written 这个 bool 的时候，看不出是推进了 3 步还是原地改了一行。
    playbook_before: int = 0           # 开跑时路书已经记到第几步
    playbook_after: int = 0            # 收尾时记到第几步
    questions: list[str] = field(default_factory=list)   # needs_input 时等人回答的问题
    resumable: bool = False            # 现场已冻结，可以接着跑
    # 裁判质疑判据：判定照旧出（该 blocked 的 blocked），但裁判认为某条判据本身写错了——
    # 在指定页面找不到、亲眼看到该字段在别的位置。挂起等人决定：采纳建议改判据后重判，
    # 或维持原判。pending_status/pending_verdict 存着按原判据算出的结论，维持原判时直接恢复。
    criteria_issues: list[dict[str, Any]] = field(default_factory=list)
    pending_status: str = ""
    pending_verdict: str = ""
    error: str | None = None
    # 外部迁移来的历史记录（roadbook/runs/ 时代、Claude Code 跑出来的）：只有文字产物，
    # 没有轨迹和录屏。看板据此隐藏录屏/轨迹区块、在历史列表里打「外部」标。
    external: bool = False
    source: str = ""                   # external 时指向原始目录，供追溯
    # 仅重判的 run 指向被复用的原始跑批（root）。空 = 正常跑批。
    # 判据（expect.yaml）改错或裁判卡死时，不重跑 runner、只重新取证——这条字段就是两者的关联。
    rejudge_of: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = str(self.status)
        data["verdict"] = str(self.verdict)
        return data

    def save(self, run_dir: Path) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / RUN_FILE).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, run_dir: Path) -> Run | None:
        path = run_dir / RUN_FILE
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Run:
        """容忍历史格式：早期 run.json 没有 skill/mode 字段，未知状态值归为 error 而不是炸掉。"""
        known = set(cls.__dataclass_fields__)
        payload = {k: v for k, v in data.items() if k in known}
        for required in ("run_id", "scenario", "skill", "mode"):
            payload.setdefault(required, "")
        raw_status = str(payload.get("status", RunStatus.RUNNING))
        payload["status"] = RunStatus(raw_status) if raw_status in RunStatus else RunStatus.ERROR
        return cls(**payload)
