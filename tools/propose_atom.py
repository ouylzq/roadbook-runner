"""propose_atom：往原子记忆层提一条建议。

模型学到了关于产品某个节点的操作知识，用这个工具交出来。它**不写 atoms/*.md**——
写不写、什么时候写，由 runtime/atoms.py 按票数定：这个节点已经有原子 → 进建议区，
等另一次跑批验证；还没有 → 没人依赖它，代码直接落盘。边界在代码里，不靠提示词。
"""
from __future__ import annotations

from typing import Any

from .base import Tool, ToolContext

NO_MEMORY = "这个会话没有原子记忆层（裁判和建场景的 agent 都没有），不能提议。"


def _propose(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.memory is None or ctx.skill is None or not ctx.run_id:
        return NO_MEMORY
    return ctx.memory.propose(
        ctx.skill,
        run_id=ctx.run_id,
        scenario=ctx.scenario or "",
        node=str(args.get("node") or ""),
        kind=str(args.get("kind") or ""),
        text=str(args.get("text") or ""),
        description=args.get("description"),
        looks_like=args.get("looks_like"),
    )


PROPOSE_ATOM_TOOL = Tool(
    name="propose_atom",
    description=(
        "学到了关于产品**某一个具体节点**（一个按钮、一个弹层、一个输入框）的操作知识时用。"
        "只有三种事值得提：踩了坑后来绕过去了；界面跟预期不一样、弄清了为什么；人纠正过你的操作。"
        "整条流程的顺序不归这里（改路书）；换个产品也会撞的工具坑也不归这里。"
        "这个节点已经有原子 → 你提的进建议区，另一次跑批验证过才落盘；还没有原子 → 值得就直接建。"
        "写法：教训，不是日志——不写日期、不写「我刚才」，不写任何工具名。"
    ),
    parameters={
        "node": {"type": "string", "description": "节点名，小写连字符，像 outline-edit、account-menu。已有原子就用它的文件名"},
        "kind": {
            "type": "string",
            "enum": ["variant", "failure_mode", "new"],
            "description": (
                "variant=这个节点还有别的入口 / 别的文案 / 改版后的形态；"
                "failure_mode=已知会失败的方式，写成「现象 → 机制 → 对策」；"
                "new=这个地方还没有原子，新建一个（要给 description 和 looks_like）"
            ),
        },
        "text": {"type": "string", "description": "一条教训，不超过 3 行。failure_mode 必须带 →。new 时可以顺带给第一条"},
        "description": {"type": "string", "description": "new 时必填：一句话 25～90 字，哪个应用的哪个节点、长什么样、什么时候来查它"},
        "looks_like": {"type": "string", "description": "new 时必填：它长什么样——锚点在哪、正常路径是什么，让失败者能确认自己站在对的地方"},
    },
    required=("node", "kind", "text"),
    handler=_propose,
)
