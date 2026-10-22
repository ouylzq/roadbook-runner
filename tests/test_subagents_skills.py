import pytest

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


def test_skill_identity_is_directory_not_frontmatter(skill):
    assert skill.name == "demo"
    assert skill.title == "demo-skill"


def test_brief_lists_resources_but_not_manifest(skill):
    brief = skill.brief()
    assert "scenarios/with-playbook/playbook.md" in brief
    assert "SKILL.md" not in brief.split("## 本技能的可读资源")[1]
