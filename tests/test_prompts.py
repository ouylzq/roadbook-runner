import re

import pytest

from runtime.prompts import PromptLibrary, TaskPrompts

CONDITIONAL = re.compile(r"如果|否则|要么|或者")


@pytest.fixture
def prompts(project_settings):
    return TaskPrompts(PromptLibrary(project_settings.prompts_dir))


def test_for_scenario_is_one_template_with_the_playbook_inlined(prompts, skill):
    """一个模板，没有分支。以前按 has_playbook 分成「回放」「探索」两套提示词——
    那是「路书只在跑通后一次性写」造出来的假分叉；改成边勘边记之后，
    已记 0 步 / 4 步 / 全部只是同一件事的不同位置。"""
    text = prompts.for_scenario(skill.scenario("with-playbook"))
    assert "# 路书" in text                    # playbook 内容已代入，不用模型再 read_resource
    assert "有路书的场景" in text
    assert "照着已记的步骤走" in text


def test_for_scenario_without_playbook_uses_same_template(prompts, skill):
    """一步都没勘过时走的是同一个模板，只是路书那块是占位——不是另一套指令。"""
    text = prompts.for_scenario(skill.scenario("no-playbook"))
    assert "照着已记的步骤走" in text           # 和上面同一个模板
    assert "一步都还没勘过" in text
    assert "upsert_playbook_step" in text       # 仍然要求边勘边记，但只写单步


@pytest.mark.parametrize("name", ["run_scenario", "author_new", "author_edit", "judge", "resume"])
def test_templates_have_no_conditional_branching(project_settings, name):
    raw = PromptLibrary(project_settings.prompts_dir).raw(name)
    assert not CONDITIONAL.search(raw), f"{name}.md 里出现了条件句，分支应该在代码里做"


def test_missing_variable_fails_loudly(project_settings):
    with pytest.raises(KeyError):
        PromptLibrary(project_settings.prompts_dir).render("run_scenario", scenario="x")


def test_author_new_url_hint(prompts):
    with_url = prompts.author_new("验登录", ["a"], "http://t")
    without = prompts.author_new("验登录", [], None)
    assert "http://t" in with_url
    assert "沿用它" in without and "（还没有）" in without


def test_judge_embeds_protocol_expect_context(prompts, skill):
    text = prompts.judge(skill.scenario("with-playbook"), protocol="只信页面原文。", context_yaml="k: v")
    assert "只信页面原文。" in text and "看到标题" in text and "k: v" in text


def test_resume_bullets_skip_blank_answers(prompts):
    text = prompts.resume(["选 (a)", "   ", "第二条"])
    assert "- 选 (a)" in text and "- 第二条" in text and "-  " not in text


def test_authoring_prompts_carry_the_intent_contract(project_settings, skill):
    """建/改场景的提示词必须带上 intent 骨架契约。

    2026-09-20 之前是让 agent「读一个现有场景照着写」——抄到哪份算哪份，
    12 份 intent 就是这么漂成 12 个样子的。契约从 runtime.intent.SECTIONS 生成，
    这条用例锁住「提示词确实接上了那个唯一事实源」。
    """
    from runtime.intent import SECTIONS

    prompts = TaskPrompts(PromptLibrary(project_settings.prompts_dir))
    new = prompts.author_new("加个忘记密码场景", ["login"], None)
    edit = prompts.author_edit(skill.scenario("with-playbook"), "把红线写严一点")

    for text in (new, edit):
        for section in SECTIONS:
            assert f"## {section.heading}" in text
        assert "不写步骤" in text and "不写期望值" in text and "不写工具名" in text
    assert "照着它的\n结构" not in new            # 旧的「抄一份现成的」不许回来


def test_judge_prompt_pins_the_origin(project_settings, skill):
    """验收路线只写相对路径；裁判两次都先猜 dev.example.com 撞 404。
    有 runner 的落点就钉死域名；没有就明说去 config/env.json 取，不许猜。"""
    prompts = TaskPrompts(PromptLibrary(project_settings.prompts_dir))
    sc = skill.scenario("with-playbook")
    pinned = prompts.judge(sc, protocol="P", context_yaml="x: 1", origin="https://app-dev.example.com")
    assert "被测站点：https://app-dev.example.com" in pinned and "不要猜别的域名" in pinned
    unknown = prompts.judge(sc, protocol="P", context_yaml="x: 1")
    assert "config/env.json" in unknown and "不要猜" in unknown


def test_briefing_reaches_runner_but_never_the_judge(project_settings, skill):
    """「这条路最近几次怎么样」是给 runner 的背景，**绝不能进裁判**——
    裁判的独立性靠它只读 expect.yaml + context.yaml，先告诉它"上次这儿没过"就是在给它定锚。
    这道墙是整套设计成立的前提，所以拿测试钉住。"""
    mark = "★上次卡在第5步★"
    prompts = TaskPrompts(PromptLibrary(project_settings.prompts_dir), lambda _s: mark)

    runner_task = prompts.for_scenario(skill.scenario("with-playbook"))
    judge_task = prompts.judge(skill.scenario("with-playbook"), protocol="协议",
                               context_yaml="draft_id: 1", origin="http://x")
    assert mark in runner_task
    assert mark not in judge_task


def test_no_briefing_is_fine(project_settings, skill):
    """没接记忆（或这个场景还没跑过）时，模板照样渲染得出来。"""
    prompts = TaskPrompts(PromptLibrary(project_settings.prompts_dir))
    assert "照着已记的步骤走" in prompts.for_scenario(skill.scenario("no-playbook"))
