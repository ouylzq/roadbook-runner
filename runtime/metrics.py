"""跑批的度量：把 storage/ 里的轨迹和库里的调用记录换算成几个能比较的数。

为什么需要它：到 2026-09-23 为止，这套 agent 的每一次改动——卡死检测的阈值、
观测去重、提示词缓存、合并任务模板——**都是靠讲道理和事后考古定的，一次度量都没有**。
「context.yaml 里没有 xxx 是最大单一失败原因」那个结论，是人翻了 20 次跑批挖出来的。

这里不做对照实验（那要真跑，花钱）。它做的是把那次人肉考古变成一条命令：
同样的口径、随时可重算、改动前后能对着看。

**等效 token** 是唯一需要解释的列：提示词缓存把读取算 0.1×、写入算 1.25×，
所以 prompt_tokens 这个原始数已经不代表花费了。换算过才比得了。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .domain import RunStatus
from .traces import Run

CACHE_READ_RATE = 0.1      # Anthropic：命中缓存的部分按一折计
CACHE_WRITE_RATE = 1.25    # 写进缓存那次要多付两成半


@dataclass(frozen=True)
class RunMetrics:
    run_id: str
    scenario: str
    status: str
    verdict: str
    runner_outcome: str        # 自己交卷 vs 烧光轮数——有验收路线时这个会被裁判判决盖掉
    runner_turns: int          # 调了几次模型（真正受 max_iterations 约束的那个数）
    runner_steps: int          # 工具调用数（界面上一直在涨的那个数，通常是轮数的两倍左右）
    judge_turns: int
    judge_steps: int
    prompt_tokens: int
    completion_tokens: int
    cache_read: int
    cache_write: int
    effective_tokens: int      # 按缓存计价换算后的等效 prompt 量
    wall_seconds: int
    playbook_before: int       # 开跑时路书记到第几步
    playbook_after: int        # 收尾时记到第几步

    @property
    def surveyed(self) -> int:
        """这次往前勘进了几步。渐进式路书之后这是最直接的进度口径——
        光看 PASS/FAIL 分不出「什么都没做成」和「推进了 3 步但没走到头」。"""
        return max(0, self.playbook_after - self.playbook_before)

    @property
    def cache_saving(self) -> float:
        return 0.0 if not self.prompt_tokens else 1 - self.effective_tokens / self.prompt_tokens

    @property
    def landed(self) -> bool | None:
        """是不是自己收的口（烧光轮数、被停、报错都不算）。

        None = **不知道**，不是"没有"。runner_outcome 是 2026-09-23 才开始记的，
        在那之前的跑批这一格是空的。度量工具的第一条戒律：别把"没记录"说成"没发生"。
        """
        if not self.runner_outcome:
            return None
        return self.runner_outcome in ("done", "blocked", "needs_input")


def effective(prompt: int, cache_read: int, cache_write: int) -> int:
    fresh = max(0, prompt - cache_read - cache_write)
    return round(fresh + cache_read * CACHE_READ_RATE + cache_write * CACHE_WRITE_RATE)


def _seconds(started: str, finished: str) -> int:
    try:
        return max(0, int((datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()))
    except ValueError:
        return 0


def measure(run: Run, calls: list[dict[str, Any]]) -> RunMetrics:
    runner = [c for c in calls if not c.get("judge")]
    judge = [c for c in calls if c.get("judge")]
    total = lambda rows, key: sum(r.get(key) or 0 for r in rows)  # noqa: E731
    prompt = total(calls, "prompt_tokens")
    read, write = total(calls, "cache_read_tokens"), total(calls, "cache_write_tokens")
    return RunMetrics(
        run_id=run.run_id, scenario=run.scenario,
        status=str(run.status), verdict=str(run.verdict),
        runner_outcome=run.runner_outcome or ("error" if run.status is RunStatus.ERROR else ""),
        runner_turns=len(runner), runner_steps=len(run.steps),
        judge_turns=len(judge), judge_steps=len(run.judge_steps),
        prompt_tokens=prompt, completion_tokens=total(calls, "completion_tokens"),
        cache_read=read, cache_write=write,
        effective_tokens=effective(prompt, read, write),
        wall_seconds=_seconds(run.started_at, run.finished_at),
        playbook_before=run.playbook_before, playbook_after=run.playbook_after,
    )


def summarize(rows: list[RunMetrics]) -> dict[str, Any]:
    """一组跑批的汇总。中位数而不是平均——跑批的耗时和 token 分布尾巴很长，
    一次卡死就能把平均值拽走。"""
    if not rows:
        return {}
    def median(values: list[int]) -> int:
        s = sorted(values)
        return s[len(s) // 2] if s else 0
    passed = [r for r in rows if r.verdict == "PASS"]
    known = [r for r in rows if r.landed is not None]
    return {
        "runs": len(rows),
        "pass_rate": len(passed) / len(rows),
        "landed_known": len(known),
        "landed_rate": (sum(bool(r.landed) for r in known) / len(known)) if known else None,
        "median_runner_turns": median([r.runner_turns for r in rows]),
        "median_prompt": median([r.prompt_tokens for r in rows]),
        "median_effective": median([r.effective_tokens for r in rows]),
        "median_wall_seconds": median([r.wall_seconds for r in rows]),
        "surveyed_steps": sum(r.surveyed for r in rows),
    }
