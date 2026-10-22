"""finish：带结构化产物的正常终止。

终止有两条路径：模型不再调工具 → 自然结束（兜底）；调 finish → 正常终止，
交出看板要的结构化结果（主路径）。它是终止型工具，由循环拦截，不会真的被 dispatch。
"""
from __future__ import annotations

from typing import Any

from .base import Tool, ToolContext


def _never(_: ToolContext, __: dict[str, Any]) -> str:
    raise RuntimeError("finish 是终止型工具，应由循环拦截")


FINISH_TOOL = Tool(
    name="finish",
    description="任务结束时调用，交出结构化产物。",
    terminal=True,
    parameters={
        "outcome": {
            "type": "string",
            "enum": ["done", "blocked", "needs_input"],
            "description": (
                "done=流程走完；"
                "blocked=环境原因跑不下去（服务挂了、页面 404），不是业务失败；"
                "needs_input=你已经认真试过，但缺的是只有人能给的业务信息"
                "（比如不知道该选哪个分支、需要一个账号、不确定哪个才是正确入口）。"
                "用这个状态不丢人——瞎试下去只会产出错的路书。"
            ),
        },
        "questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "outcome=needs_input 时必填：你需要人回答什么。一条一个问题，问得具体——"
                "说清你卡在哪个页面、看到了什么、有哪几个选项、你倾向哪个。"
            ),
        },
        "summary": {"type": "string", "description": "一句话说清发生了什么"},
        "facts": {
            "type": "object",
            "additionalProperties": True,
            "description": "留给下游的事实（执行时填裁判要的事实；裁判填 judgment）",
        },
        "notes": {"type": "string", "description": "风险点、退守原因、页面与路书不符之处"},
    },
    required=("outcome", "summary"),
    handler=_never,
)
