"""应用知识图谱：加载/校验、三个查询、纯函数更新、接入点与 demo 端到端兜底。

demo 端到端用的是仓库里 `examples/graph/` 那份真实 demo-app 图谱（对应
`D:\\roadbook` 的 todo-* 场景），不是临时拼的玩具数据。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from runtime.app import QaAgent
from runtime.cleanup import CleanupManager
from runtime.config import Settings
from runtime.domain import Scenario
from runtime.graph import (
    GRAPH_FILE,
    AppGraph,
    load_graph,
    load_skill_graph,
    parse_graph,
)
from runtime.skills import Skill
from runtime.store import Store

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "graph"
INTENT_WITH_BLOCK = "## 清理\n```yaml cleanup\n- run: manual.sh\n  args: [context.task_title]\n```\n"
INTENT_NO_BLOCK = "## 目标\n加一条任务，确认它是真被存下来了。\n"


@pytest.fixture
def graph() -> AppGraph:
    loaded = load_graph(EXAMPLES / "app.yaml")
    assert loaded.is_valid, loaded.warnings
    return loaded


@pytest.fixture
def skill_with_graph(tmp_path: Path) -> Skill:
    """把示例图谱与配方按约定路径铺成一个技能目录。"""
    root = tmp_path / "skills" / "roadbook"
    (root / "graph").mkdir(parents=True)
    shutil.copy(EXAMPLES / "app.yaml", root / GRAPH_FILE)
    shutil.copytree(EXAMPLES / "recipes", root / "recipes")
    return Skill(name="roadbook", root=root, instructions="")


# ---- 加载与校验 ---------------------------------------------------------------
def test_example_graph_loads_clean(graph: AppGraph):
    assert graph.app == "demo-app"
    assert len(graph.nodes) == 5 and len(graph.edges) == 3
    assert graph.node("page.list") is not None
    assert graph.node("nope") is None


def test_missing_and_broken_files_yield_empty_graph(tmp_path: Path):
    missing = load_graph(tmp_path / "absent.yaml")
    assert not missing and not missing.is_valid
    assert "不存在" in missing.warnings[0]

    broken = tmp_path / "app.yaml"
    broken.write_text("nodes: [oops\n", encoding="utf-8")
    bad = load_graph(broken)
    assert not bad and not bad.is_valid
    assert "读取失败" in bad.warnings[0]


def test_parse_rejects_non_dict_and_wrong_container_types():
    assert not parse_graph([]).nodes
    assert "根节点" in parse_graph([]).warnings[0]
    wrong = parse_graph({"app": "x", "nodes": {}, "edges": []})
    assert not wrong.nodes and "必须是列表" in wrong.warnings[0]


def test_parse_collects_validation_warnings_but_keeps_usable_parts():
    data = {
        "app": "x",
        "nodes": [
            {"id": "page.a", "kind": "page", "entry": True, "mystery": 1},
            {"id": "page.a", "kind": "page"},
            {"id": "BAD ID", "kind": "page"},
            {"id": "thing.x", "kind": "wat"},
            {"id": "entity.e", "kind": "entity", "cleanup_recipe": "r",
             "cleanup_via": "action.missing"},
        ],
        "edges": [
            {"from": "page.a", "to": "ghost", "confidence": 3},
            {"from": "page.a", "to": "page.a", "confidence": "abc"},
        ],
    }
    result = parse_graph(data)
    joined = " ".join(result.warnings)
    assert "未知字段" in joined and "重复" in joined and "id 不合法" in joined
    assert "kind 只能是" in joined and "不存在的节点" in joined
    assert "confidence 越界" in joined and "不是数字" in joined
    assert "cleanup_via 指向不存在" in joined
    # 坏条目被丢掉，好节点仍在；坏边（引用 ghost）不进图
    assert [n.id for n in result.nodes] == ["page.a", "entity.e"]
    assert len(result.edges) == 1


# ---- ① plan ------------------------------------------------------------------
def test_match_goal_and_plan_shortest_path(graph: AppGraph):
    plan = graph.plan("打开统计视图")
    assert plan is not None and plan.found
    assert plan.goal_node.id == "page.stats"
    assert len(plan.steps) == 1                 # page.list 有直达边，BFS 不会绕两跳
    assert plan.steps[0].anchor == "统计"
    assert "任务列表 → 统计视图" in plan.render()


def test_plan_walks_multiple_hops_and_handles_missing_target(graph: AppGraph):
    plan = graph.plan("看统计", start="action.reset-demo-data")
    assert plan is not None and len(plan.steps) == 2
    assert [s.target.id for s in plan.steps] == ["page.list", "page.stats"]
    assert graph.plan("跟这个应用毫无关系的句子") is None


def test_plan_start_equals_target_returns_empty_path(graph: AppGraph):
    plan = graph.plan("任务列表")
    assert plan is not None and not plan.found


def test_briefing_is_labeled_as_hint_not_verdict(graph: AppGraph):
    hint = graph.briefing("打开统计视图")
    assert "仅供导航参考，不是判据" in hint
    assert "以页面上实际看到的锚点为准" in hint
    assert graph.briefing("") == ""


def test_briefing_when_goal_is_the_entry_node(graph: AppGraph):
    """真实 demo 场景的 intent 大多写「任务列表」（正是入口页）。
    没有跨页路径也要给锚点/动作，不能整段消失。"""
    hint = graph.briefing("任务列表")
    assert "不需要跨页导航" in hint
    assert "重置演示数据" in hint
    assert "点顶部导航「统计」 → page.stats" in hint


# ---- ② cleanup_for ------------------------------------------------------------
def test_cleanup_for_returns_reverse_edge(graph: AppGraph):
    hint = graph.cleanup_for("entity.task")
    assert hint is not None
    assert hint.via == "action.reset-demo-data"
    assert hint.recipe == "reset_demo_data"
    assert hint.args == ()
    assert graph.cleanup_for("page.list") is None
    assert graph.cleanup_for("nope") is None


def test_cleanup_entries_only_for_facts_that_really_exist(graph: AppGraph):
    assert graph.cleanup_entries_for_facts({"task_title": "演示任务"}) == [
        {"recipe": "reset_demo_data", "args": []}]
    assert graph.cleanup_entries_for_facts({}) == []
    assert graph.cleanup_entries_for_facts({"task_title": "   "}) == []


def test_cleanup_entries_dedupe_same_recipe_and_args():
    data = {"app": "x", "nodes": [
        {"id": "entity.a", "kind": "entity", "fact_key": "a", "cleanup_via": "action.c",
         "cleanup_recipe": "same"},
        {"id": "entity.b", "kind": "entity", "fact_key": "b", "cleanup_via": "action.c",
         "cleanup_recipe": "same"},
        {"id": "action.c", "kind": "action"},
    ], "edges": []}
    entries = parse_graph(data).cleanup_entries_for_facts({"a": "1", "b": "2"})
    assert len(entries) == 1


# ---- resolve / apply_evidence --------------------------------------------------
def test_resolve_finds_nodes_by_anchor_or_alias(graph: AppGraph):
    ids = {n.id for n in graph.resolve("重置演示数据")}
    assert {"page.list", "action.reset-demo-data"} <= ids
    assert graph.resolve("") == ()


def test_apply_evidence_is_pure_and_clamps(graph: AppGraph):
    original = graph.edges[0].confidence
    updated = graph.apply_evidence(0, True)
    assert updated.edges[0].confidence == min(1.0, round(original + 0.05, 2))
    assert graph.edges[0].confidence == original       # 原图一个字节没动
    assert graph.apply_evidence(999, True) is graph    # 越界索引原样返回
    assert graph.apply_evidence(0, False).edges[0].confidence < original


def test_load_skill_graph_uses_convention_path(skill_with_graph: Skill):
    loaded = load_skill_graph(skill_with_graph.root)
    assert loaded.is_valid and loaded.app == "demo-app"
    assert not load_skill_graph(skill_with_graph.root.parent)


# ---- 接入点：briefing ----------------------------------------------------------
def test_merge_briefing_empty_hint_returns_original_callback():
    def base(_scenario: str) -> str:
        return "历史"

    assert QaAgent.merge_briefing(base, "") is base
    merged = QaAgent.merge_briefing(base, "图谱提示")
    assert merged("x") == "历史\n\n图谱提示"
    assert QaAgent.merge_briefing(lambda _s: "", "图谱提示")("x") == "图谱提示"


def _scenario(root: Path, title: str) -> Scenario:
    return Scenario(name="demo-app/todo-persistence", description="确认任务被存下来",
                    dir=root, has_playbook=True, has_expect=True,
                    start_url=None, title=title)


def _agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: str) -> QaAgent:
    monkeypatch.setenv("QA_AGENT_SKILLS", str(tmp_path / "skills"))
    monkeypatch.setenv("QA_AGENT_GRAPH_ENABLED", enabled)
    return QaAgent(Settings.from_env(tmp_path / "qa"))


def test_briefing_off_by_default_does_not_read_graph(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch,
                                                     skill_with_graph: Skill):
    agent = _agent(tmp_path, monkeypatch, "off")
    assert not agent.settings.graph_enabled
    brief = agent._briefing_for(skill_with_graph, _scenario(skill_with_graph.root, "看统计"))
    assert brief("demo-app/todo-persistence") == ""       # 没有历史，也没有图谱提示


def test_briefing_on_injects_plan_into_runner_only(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch,
                                                   skill_with_graph: Skill):
    agent = _agent(tmp_path, monkeypatch, "on")
    brief = agent._briefing_for(skill_with_graph, _scenario(skill_with_graph.root, "看统计"))
    text = brief("demo-app/todo-persistence")
    assert "已知导航路径" in text and "统计" in text
    assert "不是判据" in text


# ---- 接入点：清理台账的图谱兜底（真实 demo 资产） --------------------------------
def _cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CleanupManager:
    monkeypatch.setenv("QA_AGENT_SKILLS", str(tmp_path / "skills"))
    settings = Settings.from_env(tmp_path / "qa")
    return CleanupManager(settings, Store(settings.database_path))


def test_graph_fills_cleanup_when_intent_has_no_block(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch,
                                                      skill_with_graph: Skill):
    mgr = _cleanup(tmp_path, monkeypatch)
    graph = load_skill_graph(skill_with_graph.root)
    tasks = mgr.register_from_run("r1", "demo-app/todo-persistence", INTENT_NO_BLOCK,
                                  {"task_title": "演示任务"}, verdict="PASS", graph=graph)
    assert len(tasks) == 1 and tasks[0]["script"] == "reset_demo_data.sh"
    row = mgr.store.cleanup_tasks_for_run("r1")[0]
    assert row["state"] == "pending" and row["args_json"] == "[]"


def test_explicit_intent_block_wins_over_graph(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch,
                                               skill_with_graph: Skill):
    mgr = _cleanup(tmp_path, monkeypatch)
    graph = load_skill_graph(skill_with_graph.root)
    tasks = mgr.register_from_run("r1", "demo-app/todo-persistence", INTENT_WITH_BLOCK,
                                  {"task_title": "演示任务"}, verdict="PASS", graph=graph)
    assert tasks[0]["script"] == "manual.sh"      # 人写死的清理块优先，图不覆盖


def test_no_graph_and_no_block_registers_nothing(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch):
    mgr = _cleanup(tmp_path, monkeypatch)
    assert mgr.register_from_run("r1", "s", INTENT_NO_BLOCK,
                                 {"task_title": "x"}, verdict="PASS") == []
