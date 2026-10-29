"""清理生命周期：登记 → 保持窗口 → 调度执行 → 与仅重判的联动。

都是纯代码路径（不调 LLM、不开浏览器），但时序契约很重要：
hold 窗口是「修判据 → 只重跑裁判」能否落地的前提，锁错一秒证据就可能被提前清掉。
"""
from __future__ import annotations

import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from runtime.cleanup import CleanupManager, parse_cleanup_block, read_facts, resolve_args
from runtime.config import Settings
from runtime.store import Store

INTENT_WITH_BLOCK = textwrap.dedent("""\
    # 场景
    正文……
    ## 清理
    ```yaml cleanup
    - run: cleanup_project.sh
      args: [context.project_id]
    ```
    """)


@pytest.fixture
def mgr(monkeypatch, tmp_path: Path) -> CleanupManager:
    """隔离环境变量（别读到本机真实的 ROADBOOK_REPO），hold 窗口固定 4 小时。"""
    for var in ("QA_AGENT_SKILLS", "ROADBOOK_REPO"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("QA_AGENT_CLEANUP_HOLD_HOURS", "4")
    settings = Settings.from_env(tmp_path / "qa")
    return CleanupManager(settings, Store(settings.database_path))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def test_parse_block_and_resolve_args():
    entries = parse_cleanup_block(INTENT_WITH_BLOCK)
    assert len(entries) == 1 and entries[0]["run"] == "cleanup_project.sh"
    args, missing = resolve_args(entries[0]["args"], {"project_id": " 638217 "})
    assert args == ["638217"] and not missing          # 去空白
    args, missing = resolve_args(entries[0]["args"], {})
    assert not args and missing == ["project_id"]      # 缺了要点名，不许瞎编


def test_block_verdict_gets_hold_window_pass_does_not(mgr: CleanupManager):
    now = datetime.now()
    tasks = mgr.register_from_run("r-block", "sc", INTENT_WITH_BLOCK,
                                  {"project_id": "p1"}, verdict="BLOCK")
    assert tasks and tasks[0]["state"] == "pending"
    row = mgr.store.cleanup_tasks_for_run("r-block")[0]
    assert row["hold_until"]                       # 保持窗口有值
    assert datetime.fromisoformat(row["next_at"]) >= now + timedelta(hours=3.9)

    mgr.register_from_run("r-pass", "sc", INTENT_WITH_BLOCK, {"project_id": "p2"}, verdict="PASS")
    row = mgr.store.cleanup_tasks_for_run("r-pass")[0]
    assert not row["hold_until"]                   # PASS 不保持，立即排程
    assert datetime.fromisoformat(row["next_at"]) < now + timedelta(minutes=1)


def test_hold_then_release_only_touches_live_tasks(mgr: CleanupManager):
    mgr.register_from_run("r1", "sc", INTENT_WITH_BLOCK, {"project_id": "p1"}, verdict="BLOCK")
    mgr.store.set_cleanup_state("r1#0", "done", 1, "", "")   # 这个已经清完了
    mgr.register_from_run("r2", "sc", INTENT_WITH_BLOCK, {"project_id": "p2"}, verdict="FAIL")

    until = (datetime.now() + timedelta(hours=9)).isoformat(timespec="seconds")
    assert mgr.store.hold_cleanup("r1", until) == 0        # done 的不复活
    assert mgr.store.hold_cleanup("r2", until) == 1        # pending 的被续租

    soon = _now()
    assert mgr.store.due_cleanup_tasks(soon) == []         # 保持窗口内不会被调度取走

    assert mgr.store.release_cleanup("r2", soon) == 1      # 放行 → 立即可调度
    assert [t["id"] for t in mgr.store.due_cleanup_tasks(soon)] == ["r2#0"]


def test_claim_is_atomic_second_claim_loses(mgr: CleanupManager):
    mgr.register_from_run("r1", "sc", INTENT_WITH_BLOCK, {"project_id": "p1"}, verdict="PASS")
    now = _now()
    assert mgr.store.claim_cleanup_task("r1#0", now) is True
    assert mgr.store.claim_cleanup_task("r1#0", now) is False   # 第二次抢不到：不幂等的脚本不会跑两遍
    assert mgr.store.cleanup_tasks_for_run("r1")[0]["state"] == "running"


def test_same_script_and_args_dedup(mgr: CleanupManager):
    """深度防御：两个 run 产出同一个 project_id 时，同一脚本+实值只保留一条活任务。"""
    first = mgr.register_from_run("r1", "sc", INTENT_WITH_BLOCK, {"project_id": "same"}, verdict="PASS")
    second = mgr.register_from_run("r2", "sc", INTENT_WITH_BLOCK, {"project_id": "same"}, verdict="PASS")
    assert second[0].get("dedup") and second[0]["task_id"] == first[0]["task_id"]
    assert len(mgr.store.all_cleanup_tasks()) == 1


def test_after_rejudge_releases_on_pass_holds_otherwise(mgr: CleanupManager):
    mgr.register_from_run("root", "sc", INTENT_WITH_BLOCK, {"project_id": "p1"}, verdict="BLOCK")
    mgr.after_rejudge("root", "PASS", scenario="sc",
                      intent_text=INTENT_WITH_BLOCK, facts={"project_id": "p1"})
    soon = _now()
    assert mgr.store.due_cleanup_tasks(soon)               # PASS → 立即放行

    mgr.register_from_run("root2", "sc", INTENT_WITH_BLOCK, {"project_id": "p2"}, verdict="PASS")
    mgr.after_rejudge("root2", "BLOCK", scenario="sc",
                      intent_text=INTENT_WITH_BLOCK, facts={"project_id": "p2"})
    row = mgr.store.cleanup_tasks_for_run("root2")[0]
    assert row["hold_until"]                               # 仍非 PASS → 再续窗口
    assert datetime.fromisoformat(row["next_at"]) >= datetime.now() + timedelta(hours=3.9)
    assert len(mgr.store.cleanup_tasks_for_run("root2")) == 1   # 不重复登记


def test_after_rejudge_registers_for_legacy_root(mgr: CleanupManager):
    """hold 机制上线前的老跑批没登记过任务：重判后按 root 补登记一次。"""
    out = mgr.after_rejudge("legacy", "BLOCK", scenario="sc",
                            intent_text=INTENT_WITH_BLOCK, facts={"project_id": "p9"})
    assert out and out[0]["task_id"] == "legacy#0"
    assert mgr.store.cleanup_tasks_for_run("legacy")[0]["hold_until"]


def test_evidence_state(mgr: CleanupManager):
    assert mgr.evidence_state("none") == "intact"
    mgr.register_from_run("r1", "sc", INTENT_WITH_BLOCK, {"project_id": "p1"}, verdict="BLOCK")
    assert mgr.evidence_state("r1") == "intact"            # 只是排了队，还没清
    mgr.store.set_cleanup_state("r1#0", "running", 1, "", "")
    assert mgr.evidence_state("r1") == "cleaning"
    mgr.store.set_cleanup_state("r1#0", "done", 1, "", "")
    assert mgr.evidence_state("r1") == "cleaned"           # 这时 preflight 必须拒绝重判


def test_execute_uses_the_right_script_path(mgr: CleanupManager):
    """2026-10-28 的路径 bug：scripts_dir 已经是 …/roadbook/scripts，老代码又拼了一次，
    任何任务真执行都会 127。这里用真脚本锁住修复。"""
    scripts = mgr.scripts_dir
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "ok.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    (scripts / "cool.sh").write_text("#!/bin/bash\nexit 75\n", encoding="utf-8")
    (scripts / "boom.sh").write_text("#!/bin/bash\nexit 3\n", encoding="utf-8")

    for tid, script in (("t-ok", "ok.sh"), ("t-cool", "cool.sh"), ("t-boom", "boom.sh")):
        mgr.store.upsert_cleanup_task(task_id=tid, run_id="r", scenario="sc", script=script,
                                      args=[], state="pending", next_at=_now())
    mgr.run_due()
    states = {t["id"]: t["state"] for t in mgr.store.all_cleanup_tasks()}
    assert states["t-ok"] == "done"                        # 路径修好后真能执行到 done
    assert states["t-cool"] == "retry"                     # 冷却退出码 → 自动重试
    assert states["t-boom"] == "failed"                    # 真失败 → 进人工队列


def test_read_facts_tolerates_missing_file(tmp_path: Path):
    assert read_facts(tmp_path / "nope") == {}
    (tmp_path / "context.yaml").write_text("project_id: abc\n", encoding="utf-8")
    assert read_facts(tmp_path) == {"project_id": "abc"}
