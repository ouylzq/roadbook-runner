"""度量层的契约。核心是一条：**别把"没记录"说成"没发生"**。"""
from __future__ import annotations

from runtime.domain import RunStatus, Verdict
from runtime.metrics import effective, measure, summarize
from runtime.traces import Run


def _call(judge=False, prompt=0, read=0, write=0, completion=0):
    return {"judge": int(judge), "prompt_tokens": prompt, "completion_tokens": completion,
            "cache_read_tokens": read, "cache_write_tokens": write}


def test_effective_tokens_price_in_the_cache():
    """缓存读 0.1×、写 1.25×。打开缓存之后 prompt_tokens 这个原始数已经不代表花费了。"""
    assert effective(1000, 0, 0) == 1000                    # 没缓存就是原价
    assert effective(1000, 900, 0) == 100 + 90              # 900 按一折
    assert effective(1000, 0, 1000) == 1250                 # 全是写入，贵两成半
    assert effective(0, 0, 0) == 0


def test_landed_is_none_when_never_recorded():
    """runner_outcome 是 2026-09-23 才加的字段，历史跑批那一格是空的。
    空 ≠ 没收口——汇总里必须算成"不知道"，否则会把一批老数据报成"自己收口 0%"。"""
    old = measure(Run("r", "s", "sk", "playbook", status=RunStatus.PASSED, verdict=Verdict.PASS),
                  [_call(prompt=10)])
    assert old.landed is None

    new = measure(Run("r", "s", "sk", "playbook", status=RunStatus.BLOCKED,
                      runner_outcome="max_iterations"), [_call(prompt=10)])
    assert new.landed is False

    s = summarize([old, new])
    assert s["landed_known"] == 1 and s["landed_rate"] == 0.0   # 只在有记录的那 1 次里算


def test_summary_is_none_when_nothing_was_recorded():
    old = measure(Run("r", "s", "sk", "playbook"), [_call(prompt=10)])
    assert summarize([old])["landed_rate"] is None


def test_runner_and_judge_turns_are_counted_separately():
    """轮数（调模型次数）和步数（工具调用数）是两个数——界面上显示的是后者，
    受 max_iterations 约束的是前者。混起来看会以为上限失灵了。"""
    run = Run("r", "s", "sk", "playbook",
              steps=[{}, {}, {}], judge_steps=[{}],
              started_at="2026-09-23T10:00:00", finished_at="2026-09-23T10:02:30")
    m = measure(run, [_call(), _call(), _call(judge=True)])
    assert (m.runner_turns, m.runner_steps) == (2, 3)
    assert (m.judge_turns, m.judge_steps) == (1, 1)
    assert m.wall_seconds == 150


def test_median_not_mean():
    """跑批耗时尾巴很长，一次卡死就能把平均值拽走，所以汇总用中位数。"""
    rows = [measure(Run(f"r{i}", "s", "sk", "playbook"), [_call(prompt=p)])
            for i, p in enumerate([100, 200, 9_000_000])]
    assert summarize(rows)["median_prompt"] == 200


def test_surveyed_is_how_far_the_playbook_moved():
    """光看 PASS/FAIL 分不出「什么都没做成」和「推进了 3 步但没走到头」。
    渐进式路书之后，前后步数差就是这次的进度，不需要 agent 额外汇报什么。"""
    run = Run("r", "s", "sk", "explore", playbook_before=4, playbook_after=7)
    assert measure(run, []).surveyed == 3

    # 只改写了失配的那一步、没往前推：进度是 0，但不是负数
    assert measure(Run("r", "s", "sk", "explore", playbook_before=6, playbook_after=6), []).surveyed == 0
    assert measure(Run("r", "s", "sk", "explore", playbook_before=6, playbook_after=5), []).surveyed == 0

    rows = [measure(Run("a", "s", "sk", "e", playbook_before=0, playbook_after=4), []),
            measure(Run("b", "s", "sk", "e", playbook_before=4, playbook_after=6), [])]
    assert summarize(rows)["surveyed_steps"] == 6
