"""工具层的基座：一个工具 = 一个 `Tool` 对象，同时拥有 schema 和实现。

以前 schema（dict）和 handler（函数）分开定义、靠工具名字符串在两条 if/elif 链里缝合，
加一个工具要改三处。现在注册表按名字查表分发，加工具 = 往列表里多放一个对象。

依赖方向：`tools` 不 import `runtime`。运行时需要的类型（Skill）只在类型检查时引用。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

if TYPE_CHECKING:
    from runtime.atoms import AtomMemory
    from runtime.skills import Skill
    from tools.browser.base import Browser

logger = logging.getLogger(__name__)

JsonSchema = dict[str, Any]

# Handler 是可调用对象（Callable），例如函数、lambda、绑定方法，或实现了 __call__() 的对象
Handler = Callable[["ToolContext", dict[str, Any]], str]

@dataclass
class ToolContext:
    """一次工具调用能摸到的东西。哪个为 None，对应的工具就会明确拒绝，而不是 AttributeError。"""

    browser: Browser | None = None
    skill: Skill | None = None
    scenario: str | None = None      # 不为空时，写工具只准写这一个场景
    run_id: str | None = None        # 原子记忆层要它做来源：谁提的、谁投的
    memory: AtomMemory | None = None # 为 None 时 propose_atom / vote_atom 明确拒绝（裁判、建场景都拿不到）
    read_only: bool = False          # 裁判：可以点，但不许拿 JS 做点击（见 eval_js）
    playbook_step_sink: Callable[[dict[str, Any]], str] | None = None


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: Handler
    parameters: JsonSchema = field(default_factory=dict)
    required: tuple[str, ...] = ()
    terminal: bool = False           # 终止型工具：循环拦截它，不会真的 dispatch
    mutates: bool = False            # 执行后会让页面引用重新编号（浏览器动作型工具）

    @property
    def schema(self) -> JsonSchema:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": list(self.required),
                    "additionalProperties": False,
                },
            },
        }


class ToolRegistry:
    """按名字查表；子代理的白名单在这里裁剪成实际可用的 schema 列表。"""

    def __init__(self, tools: Iterable[Tool]):
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"工具重名: {tool.name}")
            self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def is_terminal(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.terminal)

    def schemas(self, allowed: Iterable[str]) -> list[JsonSchema]:
        """按白名单裁剪。终止型工具永远保留——没有它循环就没法正常收口。

        白名单是安全边界，所以这里不接受"空 = 全部"这种宽松默认：
        想要全部就显式把全部名字写进 agent 的 frontmatter。

        **白名单里有不认识的名字时只警告、不抛异常。** 2026-09-20 实测：给 agents/judge.md
        加了个新工具，而当时跑着的服务进程里的 REGISTRY 还是启动时那份，裁判阶段直接
        ValueError，一次已经跑了 48 步的跑批整个报废。少一个工具最多让 agent 绕点路，
        为这个把跑到一半的现场炸掉，代价完全不成比例。名字写错的情况由 `unknown_tools()`
        在开跑前一次性报出来（早失败），这里只负责**别把在飞的跑批弄掉下来**。
        """
        keep = set(allowed)
        for name in sorted(keep - set(self._tools)):
            logger.warning("白名单里有不存在的工具 %r，已跳过（检查 agents/*.md 是否和当前进程的工具表对得上）", name)
        return [t.schema for t in self._tools.values() if t.name in keep or t.terminal]

    def unknown_tools(self, allowed: Iterable[str]) -> list[str]:
        """白名单里有哪些名字这个进程不认识。开跑前用它一次性报出来，别等跑到一半才发现。"""
        return sorted(set(allowed) - set(self._tools))

    def validate_args(self, name: str, args: dict[str, Any]) -> str | None:
        """执行前按 Draft 2020-12 完整校验；失败时 handler 绝不运行。"""
        tool = self._tools.get(name)
        if tool is None:
            return f"未知工具: {name}"
        schema = tool.schema["function"]["parameters"]
        errors = sorted(Draft202012Validator(schema).iter_errors(args), key=lambda e: list(e.path))
        return _validation_message(name, errors[0]) if errors else None

    def dispatch(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
        chosen_tool = self._tools.get(name)
        # tool代表某个具体的工具
        if chosen_tool is None:
            return f"未知工具: {name}"
        if chosen_tool.terminal:
            return f"{name} 是终止型工具，不应被直接执行"
        if problem := self.validate_args(name, args):
            return f"工具参数无效：{problem}。handler 未执行，请只修正参数后重试。"
        # 被选择的工具的handler执行
        return chosen_tool.handler(ctx, args)


def _validation_message(name: str, error: ValidationError) -> str:
    path = ".".join([name, *(str(part) for part in error.absolute_path)])
    if error.validator == "required":
        missing = next((key for key in error.validator_value if key not in error.instance), "参数")
        expected = error.schema.get("properties", {}).get(missing, {}).get("type", "value")
        return f"{path}.{missing} 缺失（必填 {expected}）"
    if error.validator == "additionalProperties":
        known = set(error.schema.get("properties", {}))
        unknown = sorted(set(error.instance) - known)
        return f"{path} 包含未知参数：{'、'.join(unknown)}"
    if error.validator == "type":
        return f"{path} 类型错误：需要 {error.validator_value}，实际是 {type(error.instance).__name__}"
    if error.validator == "enum":
        return f"{path} 值无效：{error.instance!r}，只允许 {error.validator_value}"
    if error.validator == "minLength":
        return f"{path} 不能为空"
    return f"{path} 不符合 schema：{error.message}"
