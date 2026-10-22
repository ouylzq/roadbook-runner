import json

import pytest

from runtime.domain import RunStatus, Verdict
from runtime.expect import EmptyRouteError, load_expect, save_expect
from runtime.traces import Run


def test_run_round_trip_keeps_plain_strings(tmp_path):
    run = Run("r1", "sc", "sk", "playbook", status=RunStatus.PASSED, verdict=Verdict.PASS)
    run.save(tmp_path)
    raw = json.loads((tmp_path / "run.json").read_text())
    assert raw["status"] == "passed" and raw["verdict"] == "PASS"   # 前端和历史文件都只认普通字符串
    loaded = Run.load(tmp_path)
    assert loaded is not None and loaded.status is RunStatus.PASSED


def test_legacy_run_without_skill_still_loads():
    run = Run.from_dict({"run_id": "old", "scenario": "s", "mode": "executor", "status": "passed"})
    assert run.skill == "" and run.status is RunStatus.PASSED


def test_unknown_status_becomes_error_not_crash():
    assert Run.from_dict({"run_id": "x", "scenario": "s", "status": "weird"}).status is RunStatus.ERROR


def test_expect_round_trip_drops_blank_checks(skill):
    scenario = skill.scenario("with-playbook")
    result = save_expect(scenario, " 目标 ", [
        {"go": "打开首页", "check": ["看到 A", "  ", None]},
        {"go": "", "check": []},                       # 全空的步骤被丢掉
    ])
    assert result["steps"] == 1 and result["checks"] == 1
    loaded = load_expect(scenario)
    assert loaded["goal"] == "目标" and loaded["steps"][0]["check"] == ["看到 A"]


def test_expect_requires_at_least_one_step(skill):
    with pytest.raises(EmptyRouteError):
        save_expect(skill.scenario("with-playbook"), "g", [{"go": "", "check": ["", None]}])


def test_save_expect_keeps_hand_written_header_and_does_not_wrap(tmp_path):
    """2026-09-20 实测：看板上「原样保存」把 attachment-onboarding 的 17 行注释头冲掉了。
    存盘必须留住文件开头的 # 注释，长 check 也不能被 yaml 在 80 列折行。"""
    from runtime.domain import Scenario

    d = tmp_path / "scenarios" / "demo"
    d.mkdir(parents=True)
    long_check = "AI 的产出里出现了附件独有的数字口径，至少一条——62%、15-25 元（或 28 元）、早 8 点前 / 午后 14 点，写明命中了哪几条以及出现在哪段文字里"
    # 手写文件的真实写法：列表缩进在父键下、步骤之间空一行
    original = (
        "# 带附件进入 onboard 的验收路线。裁判 agent 按 steps 顺序走。\n"
        "#\n"
        "# ⚠️ 为什么门槛是「至少一条」而不是两条：价格区间是人群划分依据，一定会出现。\n"
        "\n"
        "goal: 验附件被真读进去\n"
        "\n"
        "steps:\n"
        "  - go: 打开草稿，等对话内容加载出来\n"
        "    check:\n"
        "      - 对话里有一条用户消息\n"
        "      - 这条用户消息带着附件\n"
        "\n"
        "  - go: 在同一页面往下读完整个对话\n"
        "    check:\n"
        f"      - {long_check}\n"
    )
    (d / "expect.yaml").write_text(original, encoding="utf-8")
    sc = Scenario("demo", "", d, False, True, None)

    data = load_expect(sc)
    save_expect(sc, data["goal"], data["steps"])          # 原样保存
    text = (d / "expect.yaml").read_text(encoding="utf-8")

    assert "为什么门槛是「至少一条」" in text                 # 注释头一行不少
    assert long_check in text                             # 长句没被折成两行
    assert load_expect(sc) == data                        # 结构没变
    assert text == original                               # 连缩进和空行都没变——原样保存就该一字不动


def test_required_context_keys_extracted_from_expect(tmp_path):
    """验收路线引用的 context.xxx 就是 runner 必须交的事实——静态可知，不该等跑完才发现缺。"""
    from runtime.domain import Scenario
    from runtime.expect import required_context_keys

    d = tmp_path / "scenarios" / "demo"
    d.mkdir(parents=True)
    (d / "expect.yaml").write_text(
        "goal: g\n"
        "steps:\n"
        "  - go: 打开 /zh/x?draft_id=context.draft_id&project_id=context.project_id\n"
        "    check:\n"
        "      - 标题是 context.task_title\n"
        "      - 见 context.yaml 里的说明\n",          # 文件名，不是引用
        encoding="utf-8")
    sc = Scenario("demo", "", d, False, True, None)
    assert required_context_keys(sc) == ["draft_id", "project_id", "task_title"]


def test_stopped_is_a_distinct_status_not_error(tmp_path):
    """人手动叫停不是失败：状态要落 stopped/STOPPED，不能跟真出错混在一起——
    界面靠这个区分「我自己停的」和「它挂了」。"""
    from types import SimpleNamespace

    from runtime.domain import RunStatus, Verdict
    from runtime.session import RunSession
    from runtime.traces import Run

    record = Run("r1", "demo", "sk", "playbook")
    finished = []
    fake_self = SimpleNamespace(record=record, _finish=lambda: finished.append(1) or record)

    out = RunSession._stopped(fake_self)

    assert out.status is RunStatus.STOPPED and out.verdict is Verdict.STOPPED
    assert out.summary == "被手动停止"
    assert finished == [1]   # 走的是同一条收尾链，不是另起一套


def test_blocked_runner_finishes_without_starting_judge(tmp_path):
    """环境挡住后直接 BLOCK；不能再让裁判拿重定向后的第三方域名继续跑。"""
    from types import SimpleNamespace

    from runtime.domain import Outcome
    from runtime.session import RunSession
    from runtime.traces import Run

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    record = Run("r1", "demo", "sk", "playbook")
    judged = []
    finished = []
    fake_self = SimpleNamespace(
        record=record,
        run_dir=run_dir,
        scenario=SimpleNamespace(has_expect=True),
        _judge=lambda: judged.append(1),
        _finish=lambda: finished.append(1) or record,
    )
    result = SimpleNamespace(
        outcome=Outcome.BLOCKED,
        summary="被部署保护挡住",
        notes="",
        facts={},
    )

    out = RunSession._settle(fake_self, result)

    assert out.status is RunStatus.BLOCKED and out.verdict is Verdict.BLOCK
    assert judged == [] and finished == [1]
    assert (run_dir / "blocked.txt").read_text() == "被部署保护挡住"


def test_unjudged_mirrors_expect_so_judge_py_can_report_per_check(tmp_path):
    """裁判没交答卷时替它交一份逐条 blocked 的镜像答卷：
    判分脚本一样判 BLOCK，但报告里能逐条看到「这条没判、为什么」，
    而不是之前那句没头没脑的「expect.yaml 有 2 步，judgment.yaml 有 0 步」。"""
    from types import SimpleNamespace

    from runtime.domain import Scenario
    from runtime.session import RunSession

    d = tmp_path / "scenarios" / "demo"
    d.mkdir(parents=True)
    (d / "expect.yaml").write_text(
        "goal: g\nsteps:\n  - go: 打开首页\n    check:\n      - 看到标题\n      - 看到计数\n",
        encoding="utf-8")
    sc = Scenario("demo", "", d, False, True, None)
    result = SimpleNamespace(outcome="max_iterations", summary="到达最大轮数 45 仍未结束")

    out = RunSession._unjudged(SimpleNamespace(scenario=sc), result)

    assert out["scenario"] == "demo" and len(out["steps"]) == 1
    step = out["steps"][0]
    assert step["reached"] is False and step["go"] == "打开首页"
    assert [c["fact"] for c in step["check"]] == ["看到标题", "看到计数"]   # 跟 expect 逐字镜像
    assert all(c["result"] == "blocked" for c in step["check"])
    assert "max_iterations" in step["check"][0]["note"]                   # 真因写进每一条


def test_briefing_reads_back_what_absorb_wrote(tmp_path):
    """reflections/ 以前是**只写不读**的——agent 每次把教训写下来，然后没有任何地方
    再看它一眼。那比没有记忆更坏：它看起来像在学习。这里锁住读路径真的通了。"""
    from runtime.knowledge import KnowledgeStore
    from runtime.traces import Run

    ks = KnowledgeStore(tmp_path / "knowledge")
    assert ks.briefing("demo") == ""          # 没跑过就什么都不说

    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    (run_dir / "context.yaml").write_text("draft_id: 老掉牙的ID\n", encoding="utf-8")
    rec = Run("r1", "demo", "sk", "playbook",
              status=RunStatus.BLOCKED, verdict=Verdict.BLOCK,
              finished_at="2026-09-22T15:19:44", notes="卡在第5步：确认人群那张卡没出现")
    ks.absorb(rec, run_dir)

    text = ks.briefing("demo")
    assert "BLOCK" in text and "卡在第5步" in text
    assert "背景参考" in text                  # 措辞上说清是历史不是结论
    # facts 刻意不进简报：上次的 id 每次跑都是新的，喂进去只会诱导复用一个死 id
    assert "老掉牙的ID" not in text
