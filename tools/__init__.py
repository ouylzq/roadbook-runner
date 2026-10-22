"""工具层入口：进程内唯一的工具表。

一个工具一个文件，文件名 = 工具名，所以看 `tools/` 和 `tools/browser/` 的目录列表
就是看工具清单。这里只负责把它们汇总成 `REGISTRY`。

子代理在自己的 md 里声明工具白名单，注册表据此裁剪——裁判拿不到 click/type_text
是工具层的硬约束，不是提示词里的君子协定。

依赖方向：`tools` 不 import `runtime`。
"""
from __future__ import annotations

from .base import (
    Tool,
    ToolContext,
    ToolRegistry,
    parse_tool_args,
    parse_tool_args_result,
    repair_leaked_args,
)
from .browser import BROWSER_TOOLS, Browser, Observation
from .finish import FINISH_TOOL
from .propose_atom import PROPOSE_ATOM_TOOL
from .read_resource import READ_RESOURCE_TOOL
from .run_skill_script import RUN_SKILL_SCRIPT_TOOL
from .upsert_playbook_step import UPSERT_PLAYBOOK_STEP_TOOL
from .vote_atom import VOTE_ATOM_TOOL
from .write_scenario_file import WRITE_SCENARIO_FILE_TOOL

REGISTRY = ToolRegistry([
    *BROWSER_TOOLS,             # 9 个：observe/navigate/click/type_text/find/eval_js/wait/scroll/click_xy
    READ_RESOURCE_TOOL,         # 读当前技能里的资源（渐进式披露）
    RUN_SKILL_SCRIPT_TOOL,      # 跑当前技能 scripts/ 下的脚本
    WRITE_SCENARIO_FILE_TOOL,   # 写 intent / expect；整份 playbook 不再走它
    UPSERT_PLAYBOOK_STEP_TOOL,  # 单步结构化更新 playbook
    PROPOSE_ATOM_TOOL,          # 原子记忆层：提一条建议（写不写 atoms/ 由代码数票定）
    VOTE_ATOM_TOOL,             # 原子记忆层：给候选投票
    FINISH_TOOL,                # 终止型：交结构化产物，循环拦截它
])

__all__ = [
    "REGISTRY",
    "Browser",
    "Observation",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "parse_tool_args",
    "parse_tool_args_result",
    "repair_leaked_args",
]
