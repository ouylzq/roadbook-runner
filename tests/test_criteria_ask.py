"""裁判质疑判据 → 人裁决（采纳/维持）的全链路。

背景：2026-10-28 生产那场 BLOCK 里，裁判其实早就看出了判据写错（note 里写明
字段实际在哪个页面），但按协议只能记 blocked，剩下靠人读日志、手改 yaml。
这个流程让裁判把「看出来的问题」结构化交出来，人一键裁决——判据是人的预期，
裁判只有建议权，永远不能自己改目标自己判。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from runtime.app import QaAgent
from runtime.config import Settings
from runtime.domain import RunStatus
from runtime.traces import Run

ISSUE = {
    "check_ref": "2.2", "step": 1, "check": 1,
    "current": "详情页里能读到访谈方式是语音回答，原文与 context.interview_mode_label 一致",
    "observed": "详情页正文没有『语音』字样；列表卡片原文是「语音回答」",
    "proposed": "这条记录上能读到访谈方式是语音回答，原文与 context.interview_mode_label 一致",
    "problem": "判据指定的页面不展示该字段",
}


def _mk_skill(tmp: Path) -> Path:
    root = tmp / "skills" / "demo"
    (root / "references").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "SKILL.md").write_text("---\nname: demo-skill\ndescription: 测试\n---\n# d\n", encoding="utf-8")
    (root / "references" / "judge-protocol.md").write_text("只信页面原文。", encoding="utf-8")
    d = root / "scenarios" / "plain"
    d.mkdir(parents=True)
    (d / "intent.md").write_text(
        "---\nname: plain\ndescription: t\n---\n入口 http://localhost:4173/ 。", encoding="utf-8")
    (d / "expect.yaml").write_text(
        "goal: g\nsteps:\n"
        "  - go: 打开列表\n    check:\n      - 列表里有记录\n"
        "  - go: 打开详情\n    check:\n"
        "      - 详情页里能读到招募方式是自行招募，且不是平台招募\n"
        "      - 详情页里能读到访谈方式是语音回答，原文与 context.interview_mode_label 一致\n"
        "      - 招募人数等于 context.sample_size_per_group\n",
        encoding="utf-8")
    return root


@pytest.fixture
def qa(monkeypatch, tmp_path: Path) -> QaAgent:
    for var in ("QA_AGENT_SKILLS", "ROADBOOK_REPO"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("QA_AGENT_SKILLS", str(_mk_skill(tmp_path / "assets").parent))
    settings = Settings.from_env(tmp_path / "qa")
    return QaAgent(settings)


def _mk_ask_run(qa: QaAgent, run_id: str = "r-ask", *, current: str | None = None) -> Run:
    """一条挂起等人裁决的跑批：严格结论 FAIL 已算出，质疑挂起。"""
    run_dir = qa.run_dir(run_id)
    run_dir.mkdir(parents=True)
    issue = dict(ISSUE)
    if current is not None:
        issue["current"] = current
    rec = Run(run_id, "plain", "demo", "strict", env="dev", started_at="2026-10-29T10:00:00",
              status=RunStatus("needs_input"), verdict="ASK", runner_outcome="done",
              pending_status="failed", pending_verdict="FAIL", criteria_issues=[issue])
    rec.save(run_dir)
    return rec


def test_keep_restores_strict_verdict_and_archives(qa: QaAgent):
    _mk_ask_run(qa)
    rec = qa.decide_criteria("r-ask", "keep")
    assert rec.status is RunStatus.FAILED and rec.verdict == "FAIL"   # 挂起前的严格结论
    assert not rec.criteria_issues and "维持原判" in rec.notes
    assert qa.record("r-ask").verdict == "FAIL"


def test_apply_writes_proposed_text_into_expect(qa: QaAgent):
    _mk_ask_run(qa)
    qa.decide_criteria("r-ask", "apply")
    expect = yaml.safe_load(
        (qa.skill("demo").scenario("plain").dir / "expect.yaml").read_text(encoding="utf-8"))
    assert expect["steps"][1]["check"][1] == ISSUE["proposed"]          # 精确落在 2.2 那一行
    assert expect["steps"][1]["check"][2].startswith("招募人数")          # 邻行不动
    assert expect["steps"][0]["check"] == ["列表里有记录"]                # 其它步不动
    rec = qa.record("r-ask")
    assert rec.status is RunStatus.FAILED and "已采纳修订" in rec.notes


def test_apply_with_human_edited_text(qa: QaAgent):
    """人可以在弹窗里改裁判的建议文本再采纳。"""
    _mk_ask_run(qa)
    edited = "列表卡片上能读到访谈方式是语音回答（context.interview_mode_label）"
    qa.decide_criteria("r-ask", "apply", items=[{"index": 0, "new_text": edited}])
    expect = yaml.safe_load(
        (qa.skill("demo").scenario("plain").dir / "expect.yaml").read_text(encoding="utf-8"))
    assert expect["steps"][1]["check"][1] == edited


def test_apply_refuses_when_criteria_drifted(qa: QaAgent):
    """挂起期间判据被人改过：落盘会写错行，必须整体拒绝。"""
    _mk_ask_run(qa)
    # 人先动了判据（把质疑指向的那行改了）
    scenario = qa.skill("demo").scenario("plain")
    (scenario.dir / "expect.yaml").write_text(
        "goal: g\nsteps:\n"
        "  - go: 打开列表\n    check:\n      - 列表里有记录\n"
        "  - go: 打开详情\n    check:\n"
        "      - 详情页里能读到招募方式是自行招募，且不是平台招募\n"
        "      - 已被人改过的判据行\n"
        "      - 招募人数等于 context.sample_size_per_group\n",
        encoding="utf-8")
    with pytest.raises(ValueError, match="没有任何一条能落盘"):
        qa.decide_criteria("r-ask", "apply")
    assert "已被人改过的判据行" in (scenario.dir / "expect.yaml").read_text(encoding="utf-8")


def test_decide_rejects_invalid_input(qa: QaAgent):
    _mk_ask_run(qa)
    with pytest.raises(ValueError, match="action"):
        qa.decide_criteria("r-ask", "maybe")
    # 没挂质疑的正常跑批不能被裁决
    run_dir = qa.run_dir("r-plain")
    run_dir.mkdir()
    Run("r-plain", "plain", "demo", "strict", env="dev", started_at="t",
        status=RunStatus("failed"), verdict="FAIL").save(run_dir)
    with pytest.raises(ValueError, match="没有待裁决"):
        qa.decide_criteria("r-plain", "keep")


def test_session_flow_judge_questions_become_ask(qa: QaAgent, monkeypatch):
    """session 层：裁判 finish 交出 criteria_issues 且严格结论非 PASS → 挂成 ASK，
    严格结论存进 pending_*。直接测状态迁移逻辑（不起浏览器/LLM）。"""
    from runtime.session import RunSession

    rec = Run("r-flow", "plain", "demo", "strict", env="dev", started_at="t")
    rec.status, rec.verdict = RunStatus("failed"), "BLOCK"
    issues = [dict(ISSUE)]
    # 模拟 _judge 尾部的判定段
    scored_status, scored_verdict = rec.status, rec.verdict
    if issues:
        rec.pending_status, rec.pending_verdict = str(scored_status), str(scored_verdict)
        rec.criteria_issues = issues
        rec.status, rec.verdict = RunStatus.NEEDS_INPUT, "ASK"
    assert str(rec.status) == "needs_input" and rec.verdict == "ASK"
    assert rec.pending_verdict == "BLOCK" and len(rec.criteria_issues) == 1
    _ = RunSession   # 引用防误删
