"""仅重判：preflight 的每一道闸 + 现场继承（_inherit）。

重判是给「判据改错了/裁判卡死了」的补救通道。它的安全性全在 preflight：
证据没了要拦、runner 没交棒要拦、登录态坏了要拦——每一道都用真实文件布局锁住。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import yaml

from runtime.app import QaAgent, RejudgeRefusedError
from runtime.config import Settings
from runtime.domain import RunStatus
from runtime.session import RunSession, rejudge_run_id
from runtime.store import Store
from runtime.traces import Run

STATE = {"cookies": [{"name": "t", "value": "v"}], "origins": []}


def _mk_skill(tmp: Path) -> Path:
    """一个带三种场景的技能：有判据的、判据引用事实的、没判据的。"""
    root = tmp / "skills" / "demo"
    (root / "references").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "SKILL.md").write_text("---\nname: demo-skill\ndescription: 测试\n---\n# d\n", encoding="utf-8")
    (root / "references" / "judge-protocol.md").write_text("只信页面原文。", encoding="utf-8")

    def scene(name: str, expect: str = "") -> None:
        d = root / "scenarios" / name
        d.mkdir(parents=True)
        (d / "intent.md").write_text(
            f"---\nname: {name}\ndescription: t\n---\n入口 http://localhost:4173/ 。", encoding="utf-8")
        if expect:
            (d / "expect.yaml").write_text(expect, encoding="utf-8")

    scene("plain", "goal: g\nsteps:\n  - go: 打开\n    check:\n      - 看到标题\n")
    scene("needs-fact", "goal: g\nsteps:\n  - go: 打开\n    check:\n      - 列表里有 context.project_id\n")
    scene("no-expect")
    return root


@pytest.fixture
def qa(monkeypatch, tmp_path: Path) -> QaAgent:
    for var in ("QA_AGENT_SKILLS", "ROADBOOK_REPO"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("QA_AGENT_SKILLS", str(_mk_skill(tmp_path / "assets").parent))
    monkeypatch.setenv("QA_AGENT_CLEANUP_HOLD_HOURS", "4")
    settings = Settings.from_env(tmp_path / "qa")
    return QaAgent(settings)


def _mk_run(qa: QaAgent, run_id: str, scenario: str = "plain", *, outcome: str = "done",
            status: str = "failed", verdict: str = "BLOCK", external: bool = False,
            context: dict | None = None, storage_state: dict | None = None,
            rejudge_of: str = "") -> tuple[Run, Path]:
    run_dir = qa.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    rec = Run(run_id, scenario, "demo", "strict", env="dev", started_at="2026-10-28T11:28:29",
              status=RunStatus(status), verdict=verdict, runner_outcome=outcome,
              external=external, rejudge_of=rejudge_of)
    rec.save(run_dir)
    if context is not None:
        (run_dir / "context.yaml").write_text(
            yaml.safe_dump(context, allow_unicode=True), encoding="utf-8")
    if storage_state is not None:
        (run_dir / "storage_state.json").write_text(json.dumps(storage_state), encoding="utf-8")
    return rec, run_dir


def test_preflight_happy_path(qa: QaAgent):
    _mk_run(qa, "r-ok", context={"project_id": "p1"}, storage_state=STATE)
    rec, root_dir, _skill, scenario, warnings = qa.rejudge_preflight("r-ok")
    assert rec.run_id == "r-ok" and scenario.name == "plain" and warnings == []
    assert root_dir == qa.run_dir("r-ok")


def test_preflight_404_when_record_missing(qa: QaAgent):
    with pytest.raises(RejudgeRefusedError) as e:
        qa.rejudge_preflight("ghost")
    assert e.value.status == 404


def test_preflight_rejects_external(qa: QaAgent):
    _mk_run(qa, "r-ext", external=True, context={"project_id": "p"}, storage_state=STATE)
    with pytest.raises(RejudgeRefusedError) as e:
        qa.rejudge_preflight("r-ext")
    assert e.value.status == 400 and "外部" in e.value.message


def test_preflight_rejects_still_running(qa: QaAgent):
    _mk_run(qa, "r-run", status="running", context={"project_id": "p"}, storage_state=STATE)
    with pytest.raises(RejudgeRefusedError) as e:
        qa.rejudge_preflight("r-run")
    assert e.value.status == 409 and "还在跑" in e.value.message


def test_preflight_rejects_runner_that_never_handed_off(qa: QaAgent):
    """runner 没交棒（blocked/needs_input）就没有事实和登录态——重判无从取证。"""
    _mk_run(qa, "r-blk", outcome="blocked", status="blocked", verdict="BLOCK")
    with pytest.raises(RejudgeRefusedError) as e:
        qa.rejudge_preflight("r-blk")
    assert e.value.status == 409 and "没交棒" in e.value.message


def test_preflight_rejects_scenario_without_expect(qa: QaAgent):
    _mk_run(qa, "r-noexp", scenario="no-expect", context={"project_id": "p"}, storage_state=STATE)
    with pytest.raises(RejudgeRefusedError) as e:
        qa.rejudge_preflight("r-noexp")
    assert e.value.status == 409 and "验收路线" in e.value.message


def test_preflight_rejects_missing_context(qa: QaAgent):
    _mk_run(qa, "r-noctx", storage_state=STATE)   # 没写 context.yaml
    with pytest.raises(RejudgeRefusedError) as e:
        qa.rejudge_preflight("r-noctx")
    assert e.value.status == 409 and "事实" in e.value.message


def test_preflight_rejects_missing_or_broken_storage_state(qa: QaAgent):
    _mk_run(qa, "r-nost", context={"project_id": "p"})
    with pytest.raises(RejudgeRefusedError) as e:
        qa.rejudge_preflight("r-nost")
    assert e.value.status == 409 and "登录态" in e.value.message

    _, d = _mk_run(qa, "r-badst", context={"project_id": "p"})
    (d / "storage_state.json").write_text("{截断的", encoding="utf-8")
    with pytest.raises(RejudgeRefusedError) as e:
        qa.rejudge_preflight("r-badst")
    assert e.value.status == 409 and "损坏" in e.value.message


def test_preflight_rejects_when_new_expect_needs_unrecorded_facts(qa: QaAgent):
    """判据改过之后引用了本次没留下的事实——这是重判唯一救不了的缺口：
    裁判拿不到那个值，只能整跑重新采集。"""
    _mk_run(qa, "r-needfact", scenario="needs-fact", context={"other": "x"}, storage_state=STATE)
    with pytest.raises(RejudgeRefusedError) as e:
        qa.rejudge_preflight("r-needfact")
    assert e.value.status == 409 and "project_id" in e.value.message


def test_preflight_rejects_cleaned_evidence(qa: QaAgent):
    _mk_run(qa, "r-cleaned", context={"project_id": "p1"}, storage_state=STATE)
    qa.cleanup.store.upsert_cleanup_task(
        task_id="r-cleaned#0", run_id="r-cleaned", scenario="plain",
        script="cleanup_project.sh", args=["p1"], state="done", next_at="2026-10-28T00:00:00")
    with pytest.raises(RejudgeRefusedError) as e:
        qa.rejudge_preflight("r-cleaned")
    assert e.value.status == 409 and "已被清理" in e.value.message


def test_preflight_warns_on_stale_storage_state(qa: QaAgent):
    """登录态是加密 token，入口没法精确判断是否过期——用 mtime 做软警告，不硬拦。"""
    _, run_dir = _mk_run(qa, "r-old", context={"project_id": "p1"}, storage_state=STATE)
    stale = time.time() - 5 * 3600      # 5 小时前的登录态，超过 4 小时窗口
    os.utime(run_dir / "storage_state.json", (stale, stale))
    *_, warnings = qa.rejudge_preflight("r-old")
    assert any("登录态" in w for w in warnings)


def test_preflight_resolves_to_root_for_a_rejudge_run(qa: QaAgent):
    """对重判记录再点重判 = 对 root 重判：输入源必须回到 root 的目录。"""
    _mk_run(qa, "root", context={"project_id": "p1"}, storage_state=STATE)
    _mk_run(qa, "root+rejudge120000", rejudge_of="root",
            context={"project_id": "p1"}, storage_state=STATE)
    rec, root_dir, *_ = qa.rejudge_preflight("root+rejudge120000")
    assert rec.run_id == "root" and root_dir == qa.run_dir("root")


def test_inherit_copies_scene_and_leaves_source_untouched(qa: QaAgent):
    """继承是「复制」不是「引用」：新目录有自己的现场，原目录一个字节不动。"""
    skill = qa.skill("demo")
    src_rec, src_dir = _mk_run(qa, "r-src", context={"project_id": "p1"},
                               storage_state=STATE, verdict="BLOCK")
    (src_dir / "task").mkdir()
    (src_dir / "task" / "intent.md").write_text("# intent 存档\n", encoding="utf-8")
    src_rec.steps = [{"n": 1, "tool": "navigate", "args": {}, "result_preview": "ok"}]

    mtimes_before = {p: p.stat().st_mtime_ns for p in sorted(src_dir.rglob("*")) if p.is_file()}

    new_id = rejudge_run_id("r-src")
    session = RunSession(skill, skill.scenario("plain"), new_id, qa.settings,
                         store=Store(qa.settings.database_path), env="dev",
                         rejudge_of=(src_rec, src_dir))

    new_dir = qa.run_dir(new_id)
    assert "p1" in (new_dir / "context.yaml").read_text(encoding="utf-8")
    assert json.loads((new_dir / "storage_state.json").read_text())["cookies"]
    assert (new_dir / "task" / "intent.md").exists()
    assert session.record.rejudge_of == "r-src"
    assert session.record.steps[0]["tool"] == "navigate"     # runner 轨迹继承过来
    mtimes_after = {p: p.stat().st_mtime_ns for p in sorted(src_dir.rglob("*")) if p.is_file()}
    assert mtimes_before == mtimes_after                     # 原目录原封不动


def test_inherit_snapshots_current_expect(qa: QaAgent):
    """重判读的是当前判据——新目录里要存下「这次用的是哪一版」，事后才有据可查。"""
    skill = qa.skill("demo")
    src_rec, _ = _mk_run(qa, "r-exp", context={"project_id": "p1"}, storage_state=STATE)
    new_id = rejudge_run_id("r-exp")
    RunSession(skill, skill.scenario("plain"), new_id, qa.settings,
               store=Store(qa.settings.database_path), env="dev",
               rejudge_of=(src_rec, qa.run_dir("r-exp")))
    assert "看到标题" in (qa.run_dir(new_id) / "task" / "expect.yaml").read_text(encoding="utf-8")
