from pathlib import Path

import pytest

from runtime.session import new_run_id
from runtime.skills import Skill, SkillLoader
from runtime.subagents import SubAgentLoader


def test_agent_identity_is_filename(project_settings):
    loader = SubAgentLoader(project_settings)
    assert {a.name for a in loader.list_all()} == {"agent", "judge"}


def test_agent_frontmatter_parsed(project_settings):
    agent = SubAgentLoader(project_settings).load("agent")
    assert agent.max_iterations == 0       # runner 不按轮数误杀；靠墙钟/停滞/人工停止
    assert "upsert_playbook_step" in agent.tools
    assert "核心原则" in agent.prompt


def test_skill_scenarios_detected(skill):
    by_name = {s.name: s for s in skill.scenarios()}
    assert by_name["with-playbook"].has_playbook and by_name["with-playbook"].has_expect
    assert not by_name["no-playbook"].has_playbook
    assert by_name["with-playbook"].start_url == "http://localhost:4173/"
    assert by_name["with-playbook"].mode.value == "playbook"
    assert by_name["no-playbook"].mode.value == "explore"


def test_scenario_lookup_rejects_traversal(skill):
    with pytest.raises(FileNotFoundError):
        skill.scenario("../references")


@pytest.fixture
def grouped_skill(skill_root: Path) -> Skill:
    """在平铺场景之外再放一组 scenarios/<项目>/<场景>/，和一个无 intent.md 的空壳目录。"""
    grp = skill_root / "scenarios" / "team" / "login"
    grp.mkdir(parents=True)
    (grp / "intent.md").write_text("---\nname: login\ndescription: 分组里的场景\n---\n目标。",
                                   encoding="utf-8")
    (skill_root / "scenarios" / "empty-shell").mkdir()
    (skill_root / "scenarios" / "team" / "not-a-scene").mkdir()
    loader = SkillLoader.__new__(SkillLoader)
    loader.directory = skill_root.parent
    return loader.load(skill_root.name)


def test_scenarios_discovered_in_project_groups(grouped_skill):
    by_name = {s.name: s for s in grouped_skill.scenarios()}
    # 平铺的还在，嵌套的以 <项目>/<场景> 为名；空壳目录和分组目录本身都不是场景
    assert "with-playbook" in by_name and "no-playbook" in by_name
    assert by_name["team/login"].project == "team"
    assert by_name["team/login"].dir.name == "login"
    assert "empty-shell" not in by_name and "team" not in by_name and "not-a-scene" not in by_name


def test_scenario_lookup_accepts_grouped_name(grouped_skill):
    sc = grouped_skill.scenario("team/login")
    assert sc.name == "team/login" and sc.project == "team"


def test_new_run_id_is_directory_safe():
    rid = new_run_id("team/login")
    assert "/" not in rid and rid.endswith("team-login")


def test_legal_scenario_name_accepts_group_and_rejects_escape():
    from tools.base import legal_scenario_name

    assert legal_scenario_name("login")
    assert legal_scenario_name("team/login")            # 一层项目分组合法
    assert not legal_scenario_name("")                     # 空
    assert not legal_scenario_name("../references")        # 逃逸
    assert not legal_scenario_name("team/../../etc")     # 分组里夹逃逸
    assert not legal_scenario_name("/abs/path")            # 绝对路径
    assert not legal_scenario_name("a\\b")                 # 反斜杠
    assert not legal_scenario_name("a//b")                 # 空片段


def test_agent_model_override_is_wired(project_settings):
    agent = SubAgentLoader(project_settings).load("agent")
    assert agent.model == "bedrock/claude-haiku-5-5"       # runner 走便宜快的 haiku（见 agents/agent.md 说明）
    judge = SubAgentLoader(project_settings).load("judge")
    assert judge.model is None                             # 裁判默认全局 sonnet-5


def test_skill_identity_is_directory_not_frontmatter(skill):
    assert skill.name == "demo"
    assert skill.title == "demo-skill"


def test_brief_lists_resources_but_not_manifest(skill):
    brief = skill.brief()
    assert "scenarios/with-playbook/playbook.md" in brief
    assert "SKILL.md" not in brief.split("## 本技能的可读资源")[1]
