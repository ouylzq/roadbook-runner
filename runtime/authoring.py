"""用自然语言建/改场景。

给非技术人员的入口：他们描述"想验什么"，同一个 agent 把它翻译成技能能跑的资产
（intent.md + expect.yaml）。这一步 ToolContext.browser 是 None——不是靠提示词让它
"别碰浏览器"，是物理上没有浏览器可碰，浏览器工具会明确拒绝。边界是代码给的。
跑不跑得通由后面的探索模式回答，这一步只管写资产。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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
               url: str | None = None, name: str | None = None,
               project: str | None = None,
               config_hints: dict[str, str] | None = None) -> AuthorResult:
        if scenario:
            target = skill.scenario(scenario)
            task = self.prompts.author_edit(target, ask)
            write_scope: str | None = scenario            # 改场景时锁死只能写这一个
        else:
            task = self.prompts.author_new(ask, [s.name for s in skill.scenarios()], url)
            write_scope = None                             # 新建场景，名字还没定，暂不锁
        if config_hints:
            lines = "\n".join(f"- `{origin}` 即配置键 `{key}`" for origin, key in config_hints.items())
            task_addition = (f"\n\n**以下地址已登记为配置键，intent 和 expect 里引用键名（如 `<{next(iter(config_hints.values()))}>`），"
                             f"不要写死地址**：\n{lines}")
        else:
            task_addition = ""
        # 项目分组下没起名字 → 自动补叶子名，别拿分组名顶替（会写进分组目录根部）
        if project and not name:
            name = f"scene-{datetime.now():%H%M%S}"
        full_name = "/".join(p for p in (project, name) if p) or None
        if full_name:
            # 用户点名了场景目录（含项目分组）：写进任务当硬约束，跑完再兜底搬目录一次
            task += (f"\n\n**场景目录名必须用 `{full_name}`**：写场景文件的 scenario 参数照抄它，"
                     f"不要自创名字、不要加别的前缀。") + task_addition
        # 用户原话必须原样进 intent：「什么时候跑」开头用引用块逐字保留，不得概括改写——
        # 2026-10-23 实测：转述式改写让用户在生成的 intent 里找不到自己最初的域名要求，
        # 原话是唯一不可辩驳的需求基线，机器的结构化转述只能作为它的补充。
        task += ("\n\n**用户原话必须逐字保留**：intent.md「什么时候跑」小节的开头，"
                 "先用引用块逐字抄写用户的需求原文（`> 用户原话：<原文>`，一字不改，包括域名、"
                 "地址、数字），然后再写你的转述和勘路提示。被测地址若用户给了明确 URL，"
                 "环境小节必须如实记录该 URL，不得替换成别的域名。")

        written: list[str] = []
        steps: list[Step] = []

        def on_step(step: Step) -> None:
            steps.append(step)
            if step.tool == WRITE_TOOL:
                written.append(f"{step.args.get('scenario')}/{step.args.get('filename')}")

        loop = AgentLoop(self.agents.load(MAIN_AGENT), self.llm, on_step=on_step)
        result = loop.run(ToolContext(browser=None, skill=skill, scenario=write_scope),
                          skill.brief(), task)
        # 兜底搬目录：点了名/项目但模型写错了地方，就搬到点名的位置（目录内无内部引用，安全）
        if (project or name) and written:
            src_full = written[0].rsplit("/", 1)[0]        # 模型实际写入的场景目录（可能是平铺、可能带组）
            want_full = "/".join(p for p in (project, name) if p)
            if src_full != want_full:
                leaf = src_full.rsplit("/", 1)[-1]
                if project and name:
                    dst = skill.scenarios_dir / project / name
                elif project:
                    dst = skill.scenarios_dir / project / leaf
                else:
                    dst = skill.scenarios_dir / leaf
                dst.parent.mkdir(parents=True, exist_ok=True)
                src = skill.scenarios_dir / src_full
                if src.is_dir() and not dst.exists():
                    src.rename(dst)
                    dst_rel = str(dst.relative_to(skill.scenarios_dir))
                    written = [dst_rel + f[len(src_full):] for f in written]
        return AuthorResult(
            ok=result.outcome is Outcome.DONE and bool(written),
            summary=result.summary or "（没有产出）",
            notes=result.notes,
            files=written,
            steps=[s.to_dict() for s in steps],
        )
