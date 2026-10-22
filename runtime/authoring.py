"""用自然语言建/改场景。

给非技术人员的入口：他们描述"想验什么"，同一个 agent 把它翻译成技能能跑的资产
（intent.md + expect.yaml）。这一步 ToolContext.browser 是 None——不是靠提示词让它
"别碰浏览器"，是物理上没有浏览器可碰，浏览器工具会明确拒绝。边界是代码给的。
跑不跑得通由后面的探索模式回答，这一步只管写资产。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tools import ToolContext

from .config import Settings
from .domain import Outcome
from .llm import LLM
from .loop import AgentLoop
from .prompts import PromptLibrary, TaskPrompts
from .session import MAIN_AGENT
from .skills import Skill
from .subagents import SubAgentLoader
from .traces import Step

WRITE_TOOL = "write_scenario_file"


@dataclass
class AuthorResult:
    ok: bool
    summary: str
    notes: str = ""
    files: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)

    @property
    def scenario(self) -> str | None:
        return self.files[0].split("/")[0] if self.files else None


class Authoring:
    def __init__(self, settings: Settings):
        self.prompts = TaskPrompts(PromptLibrary(settings.prompts_dir))
        self.agents = SubAgentLoader(settings)
        self.llm = LLM(settings)

    def author(self, skill: Skill, ask: str, *, scenario: str | None = None,
               url: str | None = None) -> AuthorResult:
        if scenario:
            target = skill.scenario(scenario)
            task = self.prompts.author_edit(target, ask)
            write_scope: str | None = scenario            # 改场景时锁死只能写这一个
        else:
            task = self.prompts.author_new(ask, [s.name for s in skill.scenarios()], url)
            write_scope = None                             # 新建场景，名字还没定，暂不锁

        written: list[str] = []
        steps: list[Step] = []

        def on_step(step: Step) -> None:
            steps.append(step)
            if step.tool == WRITE_TOOL:
                written.append(f"{step.args.get('scenario')}/{step.args.get('filename')}")

        loop = AgentLoop(self.agents.load(MAIN_AGENT), self.llm, on_step=on_step)
        result = loop.run(ToolContext(browser=None, skill=skill, scenario=write_scope),
                          skill.brief(), task)
        return AuthorResult(
            ok=result.outcome is Outcome.DONE and bool(written),
            summary=result.summary or "（没有产出）",
            notes=result.notes,
            files=written,
            steps=[s.to_dict() for s in steps],
        )
