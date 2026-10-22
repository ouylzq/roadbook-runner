"""SQLite 索引层：每个场景的运行记录 + 挂在 run_id 下的调用日志。"""
from __future__ import annotations

from pathlib import Path

import pytest

from runtime.llm import LlmCallInfo
from runtime.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "test.db")


def _upsert(store: Store, run_id: str, scenario: str, *, status="running", verdict="",
           started_at="2026-01-01T00:00:00", finished_at="") -> None:
    store.upsert_run(run_id=run_id, scenario=scenario, skill="roadbook", status=status,
                     verdict=verdict, started_at=started_at, finished_at=finished_at)


def test_reopening_same_file_does_not_lose_schema(tmp_path):
    path = tmp_path / "reuse.db"
    Store(path)
    store2 = Store(path)          # 第二次打开同一个文件不该报"表已存在"
    _upsert(store2, "r1", "sc")
    assert len(store2.runs_for_scenario("sc")) == 1


def test_upsert_is_idempotent_and_updates_in_place(store):
    _upsert(store, "r1", "login", status="running", verdict="")
    _upsert(store, "r1", "login", status="passed", verdict="PASS", finished_at="2026-01-01T00:05:00")
    rows = store.runs_for_scenario("login")
    assert len(rows) == 1                       # 没有变成两行
    assert rows[0]["status"] == "passed" and rows[0]["verdict"] == "PASS"


def test_runs_for_scenario_is_scoped_and_ordered(store):
    _upsert(store, "r1", "login", started_at="2026-01-01T00:00:00")
    _upsert(store, "r2", "login", started_at="2026-01-02T00:00:00")
    _upsert(store, "r3", "signup", started_at="2026-01-03T00:00:00")
    rows = store.runs_for_scenario("login")
    assert [r["run_id"] for r in rows] == ["r2", "r1"]   # 只看 login,最新的在前


def test_runs_for_scenario_respects_limit(store):
    for i in range(5):
        _upsert(store, f"r{i}", "login", started_at=f"2026-01-0{i+1}T00:00:00")
    assert len(store.runs_for_scenario("login", limit=2)) == 2


def test_latest_by_scenario_picks_max_started_at_per_scenario(store):
    _upsert(store, "r1", "login", started_at="2026-01-01T00:00:00")
    _upsert(store, "r2", "login", started_at="2026-01-05T00:00:00")
    _upsert(store, "r3", "signup", started_at="2026-01-02T00:00:00")
    latest = store.latest_by_scenario()
    assert latest["login"]["run_id"] == "r2"
    assert latest["signup"]["run_id"] == "r3"


def test_llm_calls_are_scoped_to_run_and_ordered_by_seq(store):
    info = LlmCallInfo(model="m", prompt_tokens=1, completion_tokens=1, latency_ms=10, stop_reason="stop")
    store.record_llm_call(run_id="r1", scenario="login", judge=False, seq=2,
                          requested_at="t2", info=info)
    store.record_llm_call(run_id="r1", scenario="login", judge=False, seq=1,
                          requested_at="t1", info=info)
    store.record_llm_call(run_id="r2", scenario="login", judge=True, seq=1,
                          requested_at="t1", info=info)
    rows = store.llm_calls_for_run("r1")
    assert [r["seq"] for r in rows] == [1, 2]     # 按 seq 排序，不是插入顺序
    assert all(r["run_id"] == "r1" for r in rows)


def test_llm_call_records_judge_flag_and_usage(store):
    info = LlmCallInfo(model="claude", prompt_tokens=100, completion_tokens=20,
                       latency_ms=350, stop_reason="tool_calls")
    store.record_llm_call(run_id="r1", scenario="login", judge=True, seq=1,
                          requested_at="t1", info=info)
    row = store.llm_calls_for_run("r1")[0]
    assert row["judge"] == 1
    assert row["model"] == "claude" and row["prompt_tokens"] == 100
    assert row["completion_tokens"] == 20 and row["latency_ms"] == 350
    assert row["stop_reason"] == "tool_calls"


def test_llm_calls_for_unknown_run_is_empty(store):
    assert store.llm_calls_for_run("nope") == []


def test_stop_reason_none_stored_as_empty_string(store):
    info = LlmCallInfo(model="m", prompt_tokens=1, completion_tokens=1, latency_ms=1, stop_reason=None)
    store.record_llm_call(run_id="r1", scenario="s", judge=False, seq=1, requested_at="t", info=info)
    assert store.llm_calls_for_run("r1")[0]["stop_reason"] == ""
