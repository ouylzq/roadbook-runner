from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from runtime.config import PROJECT_ROOT, Settings
from runtime.skills import Skill, SkillLoader


@pytest.fixture
def project_settings() -> Settings:
    """指向真实项目目录：读 prompts/ 和 agents/，不写任何东西。"""
    return Settings.from_env(PROJECT_ROOT)


@pytest.fixture
def skill_root(tmp_path: Path) -> Path:
    """一个最小可用的技能：一个有路书的场景、一个没路书的场景、一个脚本。"""
    root = tmp_path / "skills" / "demo"
    (root / "references").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "SKILL.md").write_text(textwrap.dedent("""\
        ---
        name: demo-skill
        description: 测试用技能
        ---
        # demo
        照路书跑。
        """), encoding="utf-8")
    (root / "references" / "judge-protocol.md").write_text("只信页面原文。", encoding="utf-8")
    (root / "scripts" / "echo.py").write_text("import sys; print('echo:' + ' '.join(sys.argv[1:]))",
                                              encoding="utf-8")

    with_pb = root / "scenarios" / "with-playbook"
    with_pb.mkdir(parents=True)
    (with_pb / "intent.md").write_text(textwrap.dedent("""\
        ---
        name: with-playbook
        description: 有路书的场景
        ---
        入口 http://localhost:4173/ 。
        """), encoding="utf-8")
    (with_pb / "playbook.md").write_text("# 路书\n1. 打开页面", encoding="utf-8")
    (with_pb / "expect.yaml").write_text("goal: g\nsteps:\n  - go: 打开\n    check:\n      - 看到标题\n",
                                         encoding="utf-8")

    no_pb = root / "scenarios" / "no-playbook"
    no_pb.mkdir()
    (no_pb / "intent.md").write_text("---\nname: no-playbook\ndescription: 待探索\n---\n目标。",
                                     encoding="utf-8")
    return root


@pytest.fixture
def skill(skill_root: Path) -> Skill:
    loader = SkillLoader.__new__(SkillLoader)
    loader.directory = skill_root.parent
    return loader.load(skill_root.name)
