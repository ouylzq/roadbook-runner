"""任务说明的拼装。

模板是数据，住在 prompts/*.md，跟 agents/*.md 同一套约定——改措辞不用碰 Python。
分支判断在代码里做完（有没有路书、是建还是改），每个模板只覆盖一种情形，
互不包含"如果……否则……"，模型拿到手的永远是单一路径、已经代入好内容的任务。

用 string.Template 而不是 str.format：模板里有 {"task_titles": [...]} 这种字面花括号，
format 会把它当占位符。$ 在模板里不会自然出现，`${var}` 更安全。
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from functools import lru_cache
from pathlib import Path
from string import Template

from runtime.domain import Scenario
from runtime.expect import required_context_keys
from runtime.intent import contract as intent_contract

# 「这条路最近几次跑得怎么样」的取数口子。做成回调而不是直接 import KnowledgeStore：
# 提示词层不该知道记忆存在哪、怎么存，换个记忆实现不用动这里。
Briefing = Callable[[str], str]
# 原子记忆层的候选列表，同一个理由做成回调。返回空串 = 记忆层关着，整段不出现。
MemoryBrief = Callable[[Scenario], str]


class PromptLibrary:
    """从目录加载模板；缺变量直接抛 KeyError，不静默留空。"""

    def __init__(self, directory: Path):
        self.directory = directory

    @lru_cache(maxsize=64)  # noqa: B019 —— 模板文件在进程生命周期内不会变
    def _template(self, name: str) -> Template:
        path = self.directory / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(f"没有这个提示词模板: {path}")
        return Template(path.read_text(encoding="utf-8"))

    def raw(self, name: str) -> str:
        return self._template(name).template

    def render(self, name: str, **variables: str) -> str:
        return self._template(name).substitute(**variables)


class TaskPrompts:
    """按情形拼任务说明。每个方法对应一个模板、一种情形，参数就是那个模板需要的全部变量。"""

    def __init__(self, library: PromptLibrary, briefing: Briefing | None = None,
                 memory_brief: MemoryBrief | None = None):
        self._lib = library
        # 「这条路最近怎么样」。裁判**不接**这个——它的独立性靠只读 expect + context。
        self._briefing = briefing or (lambda _scenario: "")
        # 原子候选。裁判同样不接：它不该知道别人提了什么建议。
        self._memory_brief = memory_brief or (lambda _scenario: "")

    def for_scenario(self, scenario: Scenario, env: str = "") -> str:
        """一个模板，没有分支。

        以前这里按 `has_playbook` 分成「回放」和「探索」两套提示词。那个二元分叉是
        「路书只在跑通之后一次性写」造出来的假象——路书一旦变成边勘边记，进度就是连续的：
        已记 0 步、4 步、全部，是同一件事的不同位置。SKILL.md 2026-09-22 已经改成一段
        统一指令，这里跟上；不跟的话 system 和任务说明会给出两套相反的指令。
        """
        return self._lib.render(
            "run_scenario",
            scenario=scenario.name,
            intent=scenario.read_or_empty("intent.md"),
            playbook=scenario.read_or_empty("playbook.md") or "（还没有——这条路一步都还没勘过，从第 1 步开始记）",
            facts_contract=self._facts_contract(scenario),
            recent_runs=self._briefing(scenario.name),
            env_line=self._env_line(env),
            atom_memory=self._atom_block(scenario),
        )

    def _atom_block(self, scenario: Scenario) -> str:
        """原子记忆层那一段：两个工具怎么用 + 当前候选。记忆层关着时回调返回空串，整段不出现，
        模型就不会去找一个不存在的工具。"""
        candidates = self._memory_brief(scenario)
        return f"{self._lib.render('atom_memory', candidates=candidates)}\n" if candidates else ""

    def _env_line(self, env: str) -> str:
        """人在看板上选了哪个环境就点名哪个。没选（场景没声明 envs）就整段不出现——
        那些场景的配置里本来也只有一组值，多说一句只会让模型去找不存在的分组。"""
        return f"{self._lib.render('env', env=env)}\n" if env else ""

    def _facts_contract(self, scenario: Scenario) -> str:
        """通用契约 + 这次点名要交的键。

        以前只讲「facts 是 context.yaml 的顶层键」这个形状，要交哪些键全靠 runner 自己
        从 intent.md 里猜——20 次历史跑批里「context.yaml 里没有 xxx」占了 4 次失败。
        键在 expect.yaml 里是静态可知的，这里直接列出来，不让它猜。
        """
        contract = self._lib.raw("facts_contract")
        keys = required_context_keys(scenario)
        if not keys:
            return contract
        listed = "\n".join(f"- `{k}`" for k in keys)
        return (f"{contract}\n**这次的验收路线会引用下面这些事实，finish 的 facts 必须全部交齐"
                f"（少一个就会被打回来重交）：**\n{listed}\n")

    def author_new(self, ask: str, existing: list[str], url: str | None) -> str:
        url_hint = (f"被测页面地址：{url}\n" if url
                    else "他没给页面地址——从现有场景里找出这个技能默认测的是哪个地址，沿用它。\n")
        return self._lib.render(
            "author_new",
            ask=ask.strip(),
            existing=", ".join(existing) or "（还没有）",
            url_hint=url_hint,
            intent_contract=intent_contract(),
        )

    def author_edit(self, scenario: Scenario, ask: str) -> str:
        return self._lib.render(
            "author_edit",
            scenario=scenario.name,
            ask=ask.strip(),
            intent=scenario.read_or_empty("intent.md"),
            expect=scenario.read_or_empty("expect.yaml"),
            intent_contract=intent_contract(),
        )

    def judge(self, scenario: Scenario, protocol: str, context_yaml: str,
              origin: str | None = None) -> str:
        """origin 是 runner 最后停在的站点。验收路线只写相对路径，不给这个裁判就会猜域名——
        实测两次都先猜默认域名撞 404。拿不到时明说去 config/env.json 取，别让它猜。"""
        return self._lib.render(
            "judge",
            scenario=scenario.name,
            protocol=protocol,
            expect=scenario.read("expect.yaml"),
            context=context_yaml,
            origin=origin or "（未知——用 read_resource 读 config/env.json，按当前环境的 researcher_web 取，不要猜）",
        )

    def compact(self, required_facts: Sequence[str] = ()) -> str:
        """上下文要被清空时，让模型自己写一份交接。

        为什么不由 harness 自动摘要：关键事实（页面上读到的 id、数值）只存在于消息里，
        harness 只能看到工具返回的前 300 字预览，拼不出来。模型自己抄最可靠。
        """
        listed = "、".join(f"`{k}`" for k in required_facts)
        note = f"这次验收路线要引用的是：{listed}——这几个必须抄上。" if listed else ""
        return self._lib.render("compact", facts_note=note)

    def resume(self, answers: list[str]) -> str:
        bullet = "\n".join(f"- {a.strip()}" for a in answers if a.strip())
        return self._lib.render("resume", answers=bullet)
