"""vote_atom：对建议区里一条候选投票。

只有**按它试过**才能投：老做法走通了就不用试候选，那是弃权，不是否决。
试完那一刻就投——跟「当场写路书」同一个理由，跑完凭记忆整理，细节已经丢了。
不能给自己这次跑批提的候选投票：独立验证的意思就是得换一次跑批。
"""
from __future__ import annotations

from typing import Any

from .base import Tool, ToolContext
from .propose_atom import NO_MEMORY


def _vote(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.memory is None or not ctx.run_id:
        return NO_MEMORY
    return ctx.memory.vote(
        run_id=ctx.run_id,
        scenario=ctx.scenario or "",
        candidate_id=str(args.get("candidate") or "").strip(),
        verdict=str(args.get("verdict") or ""),
        why=str(args.get("why") or ""),
    )


VOTE_ATOM_TOOL = Tool(
    name="vote_atom",
    description=(
        "任务说明里列了「候选」（别人踩坑后提的、还没被验证的建议）。老做法走不通、你按某条候选试了，"
        "就当场给它投一票：试通了 confirm，试了不对 refute。没试的不投——那是弃权。"
        "票够了收尾时代码会把它落进原子，不用你写。"
    ),
    parameters={
        "candidate": {"type": "string", "description": "候选 id，任务说明里方括号内那串"},
        "verdict": {"type": "string", "enum": ["confirm", "refute"], "description": "confirm=按它试了，是真的；refute=按它试了，不对"},
        "why": {"type": "string", "description": "一句话：你按它做了什么、看到了什么。账本上给人看的"},
    },
    required=("candidate", "verdict", "why"),
    handler=_vote,
)
