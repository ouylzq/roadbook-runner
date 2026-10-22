"""SQLite 索引层：跑批索引 + LLM 调用记录。

大文件（录屏、截图、完整对话）永远留在文件里；这里只存"要能查"的元数据——
按场景看运行记录、按 run_id 看这次调了几次模型、花了多少 token、多久。

零安装（sqlite3 是标准库），单文件，够用到远超这个项目规模的量级。
线程安全用一把锁——写入频率是"每步一次"，不是热路径，锁的代价可以忽略。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from .llm import LlmCallInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    scenario    TEXT NOT NULL,
    skill       TEXT NOT NULL,
    status      TEXT NOT NULL,
    verdict     TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_scenario ON runs(scenario, started_at DESC);

CREATE TABLE IF NOT EXISTS llm_calls (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             TEXT NOT NULL,
    scenario           TEXT NOT NULL,
    judge              INTEGER NOT NULL,   -- 0=主 agent，1=独立裁判
    seq                INTEGER NOT NULL,   -- 这次跑批里的第几次调用（1 起）
    requested_at       TEXT NOT NULL,
    model              TEXT NOT NULL,
    prompt_tokens      INTEGER NOT NULL,
    completion_tokens  INTEGER NOT NULL,
    latency_ms         INTEGER NOT NULL,
    stop_reason        TEXT NOT NULL DEFAULT '',
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_run ON llm_calls(run_id, seq);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """给已经存在的库补新列。`CREATE TABLE IF NOT EXISTS` 只管建表，不管加列——
        老库里没有 cache_* 这两列，不补就会 no such column。"""
        have = {row["name"] for row in self._conn.execute("PRAGMA table_info(llm_calls)")}
        for col in ("cache_read_tokens", "cache_write_tokens"):
            if col not in have:
                self._conn.execute(f"ALTER TABLE llm_calls ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")

    # ---- 每个场景的运行记录 -----------------------------------------------------
    def upsert_run(self, *, run_id: str, scenario: str, skill: str, status: str,
                    verdict: str, started_at: str, finished_at: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO runs (run_id, scenario, skill, status, verdict, started_at, finished_at)
                   VALUES (:run_id, :scenario, :skill, :status, :verdict, :started_at, :finished_at)
                   ON CONFLICT(run_id) DO UPDATE SET
                     status = excluded.status, verdict = excluded.verdict, finished_at = excluded.finished_at""",
                {"run_id": run_id, "scenario": scenario, "skill": skill, "status": status,
                 "verdict": verdict, "started_at": started_at, "finished_at": finished_at},
            )

    def runs_for_scenario(self, scenario: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE scenario = ? ORDER BY started_at DESC LIMIT ?",
                (scenario, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_by_scenario(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT r.* FROM runs r
                   JOIN (SELECT scenario, MAX(started_at) AS latest FROM runs GROUP BY scenario) top
                     ON r.scenario = top.scenario AND r.started_at = top.latest"""
            ).fetchall()
        return {row["scenario"]: dict(row) for row in rows}

    # ---- 日志：每次 LLM 调用，挂在对应的运行记录下 --------------------------------
    def record_llm_call(self, *, run_id: str, scenario: str, judge: bool, seq: int,
                         requested_at: str, info: LlmCallInfo) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO llm_calls (run_id, scenario, judge, seq, requested_at, model,
                                          prompt_tokens, completion_tokens, latency_ms, stop_reason,
                                          cache_read_tokens, cache_write_tokens)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, scenario, int(judge), seq, requested_at, info.model,
                 info.prompt_tokens, info.completion_tokens, info.latency_ms, info.stop_reason or "",
                 info.cache_read_tokens, info.cache_write_tokens),
            )

    def llm_calls_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """展示在跑批详情里的那份日志：这次跑批调了几次模型、每次花了多少 token/多久。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM llm_calls WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
