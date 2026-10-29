"""用自然语言建/改场景。

给非技术人员的入口：他们描述"想验什么"，同一个 agent 把它翻译成技能能跑的资产
（intent.md + expect.yaml）。这一步 ToolContext.browser 是 None——不是靠提示词让它
"别碰浏览器"，是物理上没有浏览器可碰，浏览器工具会明确拒绝。边界是代码给的。
跑不跑得通由后面的探索模式回答，这一步只管写资产。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any

from tools import ToolContext

from .config import Settings
from .expect import load_expect, normalize_steps
from .llm import LLM
from .loop import NO_TOOL_CALL_NOTE, AgentLoop
from .prompts import PromptLibrary, TaskPrompts
from .session import MAIN_AGENT
from .skills import Skill
from .subagents import SubAgentLoader
from .traces import Step

WRITE_TOOL = "write_scenario_file"   # 生成 agent 用的写场景工具名


@dataclass
class AuthorResult:
    ok: bool
    summary: str
    notes: str = ""
    files: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    # 成败以磁盘上两份文件的确定性校验为准；agent 调没调 finish 只是内部信号
    status: str = "ok"                       # ok | partial | failed
    health: dict[str, bool] = field(default_factory=dict)
    finished_via_finish: bool = True
    elapsed_s: float = 0.0

    @property
    def scenario(self) -> str | None:
        # 分组场景（team/login）要返回完整相对路径，只取首段会丢掉场景本身
        return self.files[0].rsplit("/", 1)[0] if self.files else None




def _collect_written(steps: list[Step]) -> list[str]:
    """只计真正写入成功的场景文件，按首次写入顺序去重。"""
    out: list[str] = []
    for step in steps:
        if step.tool != WRITE_TOOL:
            continue
        if not (step.result_preview or "").startswith(("已创建", "已覆盖")):
            continue
        full = "/".join(x for x in (str(step.args.get("scenario") or ""),
                                    str(step.args.get("filename") or "")) if x)
        if full and full not in out:
            out.append(full)
    return out


def _status_of(health: dict[str, bool]) -> str:
    if health.get("intent") and health.get("expect"):
        return "ok"
    if not health.get("intent") and not health.get("expect"):
        return "failed"
    return "partial"


def _check_health(scenarios_dir: Path, scenario: str) -> dict[str, bool]:
    """以磁盘为准的成功判据：两份文件存在、可解析、不含占位标记。

    这是生成成败的唯一权威判定——agent 调没调 finish 只是内部信号，
    不该让 PM 看到"吓人的失败"或"安静的假成功"。"""
    from .domain import Scenario
    from .intent import load_intent

    sc = Scenario(name=scenario, description="", dir=scenarios_dir / scenario,
                  has_playbook=False, has_expect=False, start_url=None)
    health = {"intent": False, "expect": False, "placeholder": False}
    intent_file = scenarios_dir / scenario / "intent.md"
    expect_file = scenarios_dir / scenario / "expect.yaml"
    if intent_file.exists():
        text = intent_file.read_text(encoding="utf-8", errors="replace")
        health["placeholder"] = "⏳ 场景生成中" in text
        try:
            parsed = load_intent(sc)
            health["intent"] = bool(parsed.get("conforms") or parsed.get("sections"))
        except Exception:
            health["intent"] = False
    if expect_file.exists():
        try:
            parsed = load_expect(sc)
            health["expect"] = bool(normalize_steps(parsed.get("steps") or []))
        except Exception:
            health["expect"] = False
    return health

class Authoring:
    def __init__(self, settings: Settings):
        self.prompts = TaskPrompts(PromptLibrary(settings.prompts_dir))
        self.agents = SubAgentLoader(settings)
        self.llm = LLM(settings)

    def author(self, skill: Skill, ask: str, *, scenario: str | None = None,
               url: str | None = None, name: str | None = None,
               project: str | None = None,
               config_hints: dict[str, str] | None = None,
               on_progress: Callable[[dict], None] | None = None) -> AuthorResult:
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
        t0 = monotonic()

        def on_step(step: Step) -> None:
            steps.append(step)
            for full in _collect_written([step]):
                if on_progress:
                    on_progress({"type": "file", "file": full.rsplit("/", 1)[-1], "ok": True})
            if step.tool == WRITE_TOOL and on_progress:
                on_progress({"type": "file", "file": "", "ok": False})
            if step.tool == "read_resource" and on_progress:
                on_progress({"type": "sample", "file": str(step.args.get("path") or "")})
            if on_progress and step.tool not in (WRITE_TOOL, "read_resource"):
                on_progress({"type": "step", "tool": step.tool})

        def on_llm(info: object) -> None:
            if on_progress:
                on_progress({"type": "llm"})

        loop = AgentLoop(self.agents.load(MAIN_AGENT), self.llm, on_step=on_step,
                         on_llm_call=(lambda info: on_progress({"type": "llm"}) if on_progress else None))
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
        finished_via_finish = any(s.tool == "finish" for s in steps)
        scenario_name = written[0].rsplit("/", 1)[0] if written else (full_name or "")
        health = _check_health(skill.scenarios_dir, scenario_name) if scenario_name else {
            "intent": False, "expect": False, "placeholder": False}
        status = _status_of(health)
        # 没调 finish 时的内部备注（loop 的 NO_TOOL_CALL_NOTE）不该流到 PM 面板——
        # 成败已由磁盘校验判定，这句只是过程信号
        notes = result.notes
        if NO_TOOL_CALL_NOTE in notes:
            notes = ""
        return AuthorResult(
            ok=status == "ok", summary=result.summary or "（没有产出）",
            notes=notes, files=written,
            steps=[s.to_dict() for s in steps],
            status=status, health=health, finished_via_finish=finished_via_finish,
            elapsed_s=monotonic() - t0,
        )
