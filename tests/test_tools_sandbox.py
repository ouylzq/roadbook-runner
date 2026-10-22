from pathlib import Path

from tools import REGISTRY, ToolContext
from tools.browser import NO_BROWSER


def _write(ctx, **args):
    return REGISTRY.dispatch(ctx, "write_scenario_file", args)


def test_write_rejects_path_traversal(skill):
    out = _write(ToolContext(skill=skill), scenario="../../evil", filename="intent.md", content="x")
    assert "不合法" in out or "拒绝" in out
    assert not (skill.root.parent / "evil").exists()


def test_write_rejects_disallowed_filename(skill):
    out = _write(ToolContext(skill=skill), scenario="with-playbook", filename="hack.py", content="x")
    assert "值无效" in out and "handler 未执行" in out


def test_write_scope_lock_blocks_other_scenarios(skill):
    ctx = ToolContext(skill=skill, scenario="with-playbook")
    out = _write(ctx, scenario="no-playbook", filename="intent.md", content="placeholder")
    assert "拒绝" in out
    assert (skill.root / "scenarios" / "no-playbook" / "intent.md").read_text(encoding="utf-8") != "placeholder"


def test_write_creates_intent_within_scope(skill):
    ctx = ToolContext(skill=skill, scenario="no-playbook")
    out = _write(ctx, scenario="no-playbook", filename="intent.md", content="new intent")
    assert "已覆盖" in out
    assert (skill.root / "scenarios" / "no-playbook" / "intent.md").read_text() == "new intent"


def test_script_rejects_traversal_and_missing(skill):
    ctx = ToolContext(skill=skill)
    assert "不合法" in REGISTRY.dispatch(ctx, "run_skill_script", {"script": "../SKILL.md"})
    assert "没有这个脚本" in REGISTRY.dispatch(ctx, "run_skill_script", {"script": "nope.py"})


def test_script_runs_inside_scripts_dir(skill):
    out = REGISTRY.dispatch(ToolContext(skill=skill), "run_skill_script",
                            {"script": "echo.py", "args": ["a", "b c"]})
    assert "退出码 0" in out
    assert "echo:a b c" in out


def test_resource_read_rejects_traversal(skill):
    assert "拒绝" in REGISTRY.dispatch(ToolContext(skill=skill), "read_resource", {"path": "../../etc/passwd"})


def test_browser_tools_refuse_without_browser(skill):
    ctx = ToolContext(browser=None, skill=skill)
    assert REGISTRY.dispatch(ctx, "navigate", {"url": "http://x"}) == NO_BROWSER
    assert REGISTRY.dispatch(ctx, "observe", {}) == NO_BROWSER


def test_tools_without_skill_say_so(tmp_path: Path):
    ctx = ToolContext()
    assert "没有加载技能" in REGISTRY.dispatch(ctx, "read_resource", {"path": "a"})
    assert "没有加载技能" in REGISTRY.dispatch(ctx, "write_scenario_file",
                                          {"scenario": "s", "filename": "intent.md", "content": ""})


def test_full_playbook_overwrite_is_not_an_available_operation(skill):
    before = (skill.root / "scenarios" / "with-playbook" / "playbook.md").read_text()
    out = REGISTRY.dispatch(ToolContext(skill=skill), "write_scenario_file", {
        "scenario": "with-playbook", "filename": "playbook.md", "content": "replacement"})
    assert "值无效" in out and "handler 未执行" in out
    assert (skill.root / "scenarios" / "with-playbook" / "playbook.md").read_text() == before


def _step(number=1, **overrides):
    data = {"scenario": "with-playbook", "step_number": number, "reference": "—",
            "goal": "打开首页", "anchor": "标题", "action": "打开页面", "done": "列表出现"}
    data.update(overrides)
    return data


def test_upsert_playbook_step_updates_one_step_without_touching_the_rest(skill):
    target = skill.root / "scenarios" / "with-playbook" / "playbook.md"
    target.write_text("# 路书\n> 最后勘路：旧日期\n\n## 步骤\n\n"
                      "1. 【参考】—\n   【目的】旧目的\n   【锚点】标题\n"
                      "   【动作】打开\n   【到位标志】出现\n\n"
                      "2. 【参考】—\n   【目的】保留我\n   【锚点】按钮\n"
                      "   【动作】点击\n   【到位标志】完成\n", encoding="utf-8")
    out = REGISTRY.dispatch(ToolContext(skill=skill, scenario="with-playbook"),
                            "upsert_playbook_step", _step(goal="新目的"))
    text = target.read_text()
    assert "已更新" in out and "【目的】新目的" in text and "【目的】保留我" in text


def test_upsert_missing_required_field_never_reaches_handler(skill):
    target = skill.root / "scenarios" / "with-playbook" / "playbook.md"
    before = target.read_text()
    args = _step()
    args.pop("done")
    out = REGISTRY.dispatch(ToolContext(skill=skill), "upsert_playbook_step", args)
    assert "done 缺失" in out and "handler 未执行" in out and target.read_text() == before
