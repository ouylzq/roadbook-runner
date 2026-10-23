"""跑哪个环境 + 验收路线里那些 `context.xxx` 到底是什么。

这两件事在 2026-09-23 之前都是盲的：`envs: [dev, prod]` 写在 intent.md 的 frontmatter 里
却从没被读过，看板只能跑配置当前指着的那个环境；`context.app_base` 在验收路线里满屏出现，
人却看不到它代表什么、上次是多少。
"""
from __future__ import annotations

import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from runtime.app import QaAgent
from runtime.context_vars import describe
from runtime.domain import Scenario
from runtime.prompts import PromptLibrary, TaskPrompts
from runtime.skills import Skill, SkillLoader


def _scenario(root: Path, name: str, frontmatter: str, *, expect: str = "", intent_body: str = "") -> Scenario:
    d = root / "scenarios" / name
    d.mkdir(parents=True)
    (d / "intent.md").write_text(f"---\nname: {name}\ndescription: 测试用场景描述，够长了吧够长了吧\n"
                                 f"{frontmatter}---\n\n# {name}\n\n{intent_body}", encoding="utf-8")
    if expect:
        (d / "expect.yaml").write_text(expect, encoding="utf-8")
    loader = SkillLoader.__new__(SkillLoader)
    loader.directory = root.parent
    skill: Skill = loader.load(root.name)
    return skill.scenario(name)


@pytest.fixture
def root(skill_root: Path) -> Path:
    return skill_root


# ---- envs 读取 ---------------------------------------------------------------
def test_envs_come_from_frontmatter(root: Path) -> None:
    sc = _scenario(root, "two-envs", "envs: [dev, prod]\ndefault_env: dev\n")
    assert sc.envs == ("dev", "prod")
    assert sc.default_env == "dev"


def test_default_env_alone_still_gives_one_option(root: Path) -> None:
    """只写 default_env 不写 envs 的，下拉框不该是空的。"""
    sc = _scenario(root, "only-default", "default_env: prod\n")
    assert sc.envs == ("prod",)
    assert sc.default_env == "prod"


def test_no_env_declared_stays_empty(root: Path) -> None:
    """没声明过环境的场景一律留空——留空时任务说明里整段不出现。"""
    sc = _scenario(root, "no-env", "")
    assert sc.envs == () and sc.default_env == ""


def test_roadbook_repo_points_loader_at_its_parent(project_settings, tmp_path, monkeypatch) -> None:
    roadbook = tmp_path / "roadbook"
    roadbook.mkdir()
    monkeypatch.delenv("QA_AGENT_SKILLS", raising=False)
    monkeypatch.setenv("ROADBOOK_REPO", str(roadbook))
    settings = replace(project_settings, root=tmp_path / "qa-agent")
    assert settings.skills_dir == tmp_path


# ---- 选环境 -----------------------------------------------------------------
def test_pick_env_honours_declaration(root: Path) -> None:
    sc = _scenario(root, "pick", "envs: [dev, prod]\ndefault_env: dev\n")
    assert QaAgent.pick_env(sc, "prod") == "prod"
    assert QaAgent.pick_env(sc, None) == "dev"
    assert QaAgent.pick_env(sc, "  ") == "dev"


def test_pick_env_refuses_undeclared(root: Path) -> None:
    """`envs: [dev]` 的场景不该因为一个手打的参数就去打 prod——落回默认，不是照单全收。"""
    sc = _scenario(root, "dev-only", "envs: [dev]\ndefault_env: dev\n")
    assert QaAgent.pick_env(sc, "prod") == "dev"


def test_pick_env_on_scenario_without_envs(root: Path) -> None:
    sc = _scenario(root, "bare", "")
    assert QaAgent.pick_env(sc, "prod") == ""


# ---- 任务说明里点名环境 --------------------------------------------------------
def test_env_named_in_task(project_settings, root: Path) -> None:
    sc = _scenario(root, "named", "envs: [dev, prod]\ndefault_env: dev\n")
    prompts = TaskPrompts(PromptLibrary(project_settings.prompts_dir))
    assert "prod" in prompts.for_scenario(sc, "prod")
    # 没选环境的场景整段不出现：它们的配置里本来就只有一组值，多说一句只会让模型去找不存在的分组
    assert "本次跑的环境" not in prompts.for_scenario(sc, "")


# ---- context 变量说明 ---------------------------------------------------------
EXPECT = textwrap.dedent("""\
    goal: 证明登录态真的建立了
    steps:
      - go: 打开 context.app_base + /zh/onboarding
        check:
          - 邮箱等于 context.account_email
          - 拿到的是 context.missing_key
    """)


def test_describe_lists_keys_with_last_values(root: Path, tmp_path: Path) -> None:
    sc = _scenario(root, "ctx", "envs: [dev, prod]\ndefault_env: dev\n", expect=EXPECT,
                   intent_body="## 留给裁判的事实\n\n```yaml\napp_base: <本次域名>\n```\n")
    facts = tmp_path / "facts"
    facts.mkdir()
    (facts / "ctx.yaml").write_text(
        "# 最近一次跑批 20260923-151140-ctx 留下的事实\n"
        "app_base: https://app-dev.example.ai\naccount_email: a@b.c\n", encoding="utf-8")

    out = describe(sc, facts)
    by_key = {v["key"]: v for v in out["variables"]}
    assert set(by_key) == {"app_base", "account_email", "missing_key"}
    assert by_key["app_base"]["last_value"] == "https://app-dev.example.ai"
    assert by_key["app_base"]["known"] and by_key["app_base"]["declared"]
    # 意图书没点名的键仍然要列出来——少一份人写的交代是个信号，不是隐藏的理由
    assert by_key["account_email"]["known"] and not by_key["account_email"]["declared"]
    # 从来没跑出来过的键必须显式标出来：这一步大概率会判 BLOCK
    assert not by_key["missing_key"]["known"]
    assert out["last_run"] == "20260923-151140-ctx"
    assert out["envs"] == ["dev", "prod"]


def test_describe_without_any_history(root: Path, tmp_path: Path) -> None:
    """一次都没跑过的场景也要能打开——全列出来、全标成「还没跑出来过」。"""
    sc = _scenario(root, "fresh", "", expect=EXPECT)
    out = describe(sc, tmp_path / "nowhere")
    assert [v["key"] for v in out["variables"]] == ["account_email", "app_base", "missing_key"]
    assert not any(v["known"] for v in out["variables"])
    assert out["last_run"] == ""


def test_declared_but_unused_is_surfaced(root: Path, tmp_path: Path) -> None:
    """验收路线删过一步之后，intent.md 常常忘了跟着删——runner 会一直白交那几个值。"""
    sc = _scenario(root, "stale", "", expect="goal: g\nsteps:\n  - go: 看 context.app_base\n    check:\n      - ok\n",
                   intent_body="## 留给裁判的事实\n\n```yaml\napp_base: x\ndraft_id: y\n```\n")
    out = describe(sc, tmp_path)
    assert out["unused_declared"] == ["draft_id"]


# ---- 给人看的标题 --------------------------------------------------------------
def test_title_comes_from_h1_without_prefix(root: Path) -> None:
    sc = _scenario(root, "titled", "", intent_body="")
    # _scenario 写的正文是「# titled」，没有前缀
    assert sc.title == "titled"
    d = root / "scenarios" / "prefixed"
    d.mkdir()
    (d / "intent.md").write_text("---\nname: prefixed\ndescription: 够长的描述够长的描述够长的描述够长的描述\n---\n\n"
                                 "# 场景：邮箱验证码登录（dev / prod 同一份）\n\n正文", encoding="utf-8")
    loader = SkillLoader.__new__(SkillLoader)
    loader.directory = root.parent
    assert loader.load(root.name).scenario("prefixed").title == "邮箱验证码登录（dev / prod 同一份）"
    assert "title" in sc.to_dict()
