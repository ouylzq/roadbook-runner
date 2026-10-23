"""Observe → Act → Observe 主循环。

循环刻意做得很薄：不预设任何业务分支，路由全交给模型（扁平 agent 循环，不是预先声明的图）。
控制流只管三件事——喂上下文、调度工具、判断该不该停。
"""
from __future__ import annotations

import contextlib
import itertools
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from tools import BROWSER_TOOLS, REGISTRY, ToolContext, ToolRegistry, parse_tool_args_result

from .cancel import CancelledError
from .domain import Outcome
from .llm import LLM, LlmCallHook, LlmCallInfo
from .subagents import SubAgent
from .traces import Step

Message = dict[str, Any]
StepListener = Callable[[Step], None]

STEP_PREVIEW_CHARS = 300
TOOL_RESULT_MAX_CHARS = 12000

NEEDS_INPUT_WITHOUT_QUESTIONS = (
    "finish 被拒绝：outcome=needs_input 时 questions 不能为空。"
    "请重新调用 finish，把你需要人回答的问题逐条放进 questions 数组，"
    "每条说清你卡在哪一页、看到了什么、有哪几个选项、你倾向哪个。"
)
NO_TOOL_CALL_NOTE = "⚠️ 没有调用 finish，缺结构化产物"


def missing_facts_message(missing: list[str]) -> str:
    return (
        "finish 被拒绝：验收路线要引用这些事实，但 facts 里没有——"
        + "、".join(missing)
        + "。请把它们作为 facts 的顶层键补上再调一次 finish"
        "（值要是你这次实际跑出来的，不是猜的）。"
        "现在浏览器还开着，读不到就先去页面上取。"
    )

# 收口保险：撞到 max_iterations 是最差的结局——一份空产物、零解释。
# 2026-09-17 实测：裁判在页面上找不到人群卡，连写 19 个 eval_js 硬扒，30 轮烧光没调 finish，
# judgment.yaml 空的，judge.py 只能报「有 0 步」。协议明明写着「找不到 → blocked」，
# 但模型不会自己看轮数。剩 5 轮时提醒一次，最后一轮再拦一次。
LANDING_TURNS = 5
LANDING_WARNING = (
    "⏳ 轮数提醒：还剩 {n} 轮。立刻停止读资料、改路书和非必要取证，只走达成业务目标的最短路径。"
    "页面上已经有明确下一步且在用户授权范围内，就继续操作，不能仅因轮数提醒提前报 BLOCK。"
    "只有确实被环境/权限/缺失信息挡住或动作不安全时才 finish(blocked/needs_input)。"
)
LAST_TURN_WARNING = "⛔ 这是最后一轮：只能调 finish，别再调任何其它工具。"

# 输出撞到 max_tokens 被腰斩：SDK 解不出完整的 tool_calls，`reply.tool_calls` 直接是空的——
# 跟"模型真的想通了、决定不调任何工具"长得一模一样，但意思完全相反：前者是没说完，
# 后者是说完了。2026-09-24 实测栽过：裁判倒数第二轮 4096 completion token 全部打满、
# 卡在生成 finish 的参数中途，被当成「模型直接结束」，整场 29 次调用白跑，
# 判分脚本因为没等到 judgment 把每一条 check 都判成 BLOCK——其实裁判前面已经核实过好几条。
# 连续撞两次才真的放弃：一次多半是偶发的长输出，两次说明这一步确实需要的篇幅超了。
MAX_TRUNCATIONS_IN_A_ROW = 2
TRUNCATED_NUDGE = (
    "⚠️ 上一条回复没说完——输出撞到了长度上限，被从中间截断了，不是你决定结束。"
    "这次换个更省字数的写法重新给出这一步该调的工具：能拆的拆成几个小一点的调用；"
    "要写大段内容（比如整份路书）就先只写这一步验证过的部分，别想着一次写完。"
)
TRUNCATED_GIVE_UP_NOTE = (
    "⚠️ 连续 {n} 次输出撞到长度上限（当前 QA_AGENT_MAX_TOKENS=4096），换了写法还是说不完这一步，"
    "不是偶发的——这一步要交的内容本身就超过了当前上限，硬等下去只会一直重复。"
    "典型场景是把整个文件的 base64 内联进一条调用（如 docx/pdf 注入）。"
    "解法在服务端配置：.env 里调大 QA_AGENT_MAX_TOKENS（如 16384）后重启服务再跑。"
)
REF_STALE_AFTER_MUTATION = (
    "已跳过：这一批里前面的动作已经让页面引用重新编号了，"
    "你手上这个 ref 现在很可能指向别的元素。先 observe 一次拿到新引用，再重新调用这个工具。"
)

# 原地踏步：2026-09-21 实测 launch-project-deep-noscreener——AI 在对话里问了两个问题却没给出
# 回答的控件，agent 从此对着一张**一个像素都没变**的页面又跑了 35 轮（24 张截图 md5 完全相同），
# 烧掉 3.4M prompt token（占整趟 80%），最后是人手动杀进程才停。
#
# 注意它为什么没被现有机制拦住：轮数上限（55/60）还没到；而主流那套「同动作+同观测重复 N 次」
# 的检测也拦不住——这个 agent 每轮换着花样试（wait 5 → wait 8 → observe → grep → eval_js…），
# 动作指纹永不重复，而且**一次报错都没有**，全是「成功地什么都没发生」。
# 所以指纹要取在环境这一侧：动作可以千变万化，页面没变就是没进展。
BROWSER_TOOL_NAMES = frozenset(t.name for t in BROWSER_TOOLS)

STALL_NUDGE = (
    "🧊 页面已经连续 {n} 轮没有任何变化了——你这几轮的操作没有让它产生任何进展。"
    "你在等的东西很可能不会来了（改版、流程变了、或者它压根不是以你预期的形态出现）。"
    "别再等、也别再换着花样探查同一处。二选一："
    "① 换一条路走（比如照页面上真实存在的控件操作，而不是找路书里写的那个）；"
    "② 直接调 finish：outcome 用 blocked，summary 说清你卡在哪一步、页面上实际显示的是什么、"
    "你期望出现但没出现的是什么。一份写着 blocked 的结论远好过把轮数耗光。"
)
STALL_ABORT = (
    "页面连续 {n} 轮没有任何变化，agent 也没有收口——harness 判定卡死并终止。"
    "最后停在：{where}"
)
COMPACTED = (
    "⤵️ 上下文已被压缩（前面 {n} 轮的对话没了）。下面是你自己写的交接——"
    "再往下是**最近 {kept} 轮的原始记录**，那几轮里的页面观测和引用编号仍然有效。\n\n{summary}"
)
COMPACTED_NO_TAIL = (
    "⤵️ 上下文已被清空并重建（原来的 {n} 轮对话没了）。下面是你自己写的交接，"
    "它现在是你对之前发生过什么的**唯一记录**——页面要重新 observe，引用编号全部作废。\n\n{summary}"
)

# 保留最近几轮**原文**，不只留摘要。抄的是 OpenCode 的 preserveRecentBudget：
# 它压缩后留约 25% 上下文预算的近期消息，按 turn 边界切。
# 在这个域里这一条尤其要紧——最后几条消息装着当前页面的完整观测（结构树、可见文本、
# ref_N 编号），摘要再好也复述不出来。不留的话，压缩之后必然浪费一轮重新 observe。
KEEP_RECENT_TURNS = 2
WALL_CLOCK_EXCEEDED = "超过墙上时钟上限 {n} 秒，harness 终止。最后停在：{where}"
TOKENS_EXCEEDED = "累计 prompt token 超过上限 {n}，harness 终止。最后停在：{where}"


@dataclass
class LoopResult:
    outcome: Outcome
    summary: str
    facts: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    questions: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    transcript: list[Message] = field(default_factory=list)   # 冻结现场，供之后接着跑


class AgentLoop:
    """把一个子代理定义跑成一次完整的工具调用循环。"""

    def __init__(
        self,
        agent: SubAgent,
        llm: LLM,
        *,
        registry: ToolRegistry = REGISTRY,
        on_step: StepListener | None = None,
        on_llm_call: LlmCallHook | None = None,
        required_facts: Sequence[str] = (),
        cancel: threading.Event | None = None,
        fingerprint: Callable[[], str | None] | None = None,
        compact_at: int = 0,
        compact_prompt: str = "",
        refresh_task: Callable[[], str] | None = None,
        keep_recent_turns: int | None = None,
    ):
        self.agent = agent
        self.llm = llm
        self.registry = registry
        self.required_facts = tuple(required_facts)
        self.on_step = on_step
        self.on_llm_call = on_llm_call
        self.cancel = cancel
        # 取环境指纹的回调。由调用方注入（会话层接到浏览器上），循环层不认识浏览器。
        # 没注入就等于关掉原地踏步检测——非浏览器场景和单测都走这条路。
        self.fingerprint = fingerprint
        # 上下文顶到 compact_at 就重建一次。0 或没给交接提示词 = 不压缩。
        self.compact_at = compact_at if compact_prompt else 0
        self.compact_prompt = compact_prompt
        # 重建时重新渲染任务说明。路书是 agent 边勘边记的资产——开跑时那份到压缩时
        # 可能已经多了几步，原样复用等于把它自己刚记下的东西又藏起来。
        # 没注入就沿用原来那条（续跑、非浏览器场景走这条路）。
        self.refresh_task = refresh_task
        # 没显式传就用子代理定义上的值（frontmatter → 全局默认两层）
        self.keep_recent_turns = (agent.keep_recent_turns if keep_recent_turns is None
                                  else keep_recent_turns)
        self._tools = registry.schemas(agent.tools)

    # ---- 入口 ----------------------------------------------------------------
    def run(self, ctx: ToolContext, system_prefix: str, task: str) -> LoopResult:
        messages: list[Message] = [
            {"role": "system", "content": f"{system_prefix}\n\n---\n\n{self.agent.prompt}"},
            {"role": "user", "content": task},
        ]
        return self._drive(ctx, messages)

    def resume(self, ctx: ToolContext, transcript: list[Message], reply: str) -> LoopResult:
        """从冻结的对话接着跑：历史整段带回来，agent 记得自己之前试过什么、卡在哪。"""
        return self._drive(ctx, [*transcript, {"role": "user", "content": reply}])

    # ---- 循环体 ---------------------------------------------------------------
    def _drive(self, ctx: ToolContext, messages: list[Message]) -> LoopResult:
        # 主循环：调模型→跑这批工具调用→喂结果→再调，直到收口
        steps: list[Step] = []
        started = time.monotonic()
        spent_tokens = 0
        context_tokens = 0                # 上一次调用时上下文有多大——压缩阈值看它
        compactions = 0
        last_print: str | None = None     # 上一轮的环境指纹
        stalled_turns = 0                 # 环境连续多少轮没变化
        last_stop_reason: str | None = None
        truncated_in_a_row = 0            # 连续几次「输出被截断」，跟「环境没变化」是两回事，分开计

        def count_tokens(info: LlmCallInfo) -> None:
            nonlocal spent_tokens, context_tokens, last_stop_reason
            spent_tokens += info.prompt_tokens
            context_tokens = info.prompt_tokens
            last_stop_reason = info.stop_reason
            if self.on_llm_call:
                self.on_llm_call(info)

        # max_iterations=0 时不设轮数上限，仍受墙钟、停滞检测和人工停止约束。
        iterations = (itertools.count(1) if self.agent.max_iterations <= 0
                      else range(1, self.agent.max_iterations + 1))
        for llm_seq in iterations:   # 第几次调模型，落进每一步
            if self.cancel and self.cancel.is_set():   # 每轮开头查一次，是最自然的收口点
                raise CancelledError()
            if over := self._over_budget(started, spent_tokens, steps, messages):
                return over
            # 上下文顶到阈值：在**轮与轮之间**重建一次。这里是唯一安全的切点——
            # 上一批工具调用已经全部有 tool_result 了，不会切出半个 tool_use。
            if 0 < self.compact_at <= context_tokens:
                messages = self._compact(messages, steps, llm_seq, count_tokens)
                compactions += 1
                context_tokens = 0
            # 第一轮不提醒：任务刚下发就催收口没意义（预算很小的子代理会撞上这种情况）
            if self.agent.max_iterations > 0:
                remaining = self.agent.max_iterations - llm_seq + 1
                if llm_seq > 1 and remaining == LANDING_TURNS:
                    messages.append({"role": "user", "content": LANDING_WARNING.format(n=remaining)})
                elif llm_seq > 1 and remaining == 1:
                    messages.append({"role": "user", "content": LAST_TURN_WARNING})
            reply = self.llm.chat(messages, self._tools, on_call=count_tokens)

            if last_stop_reason == "length":
                # 哪怕 SDK 勉强解析出半截 tool_call，也不能执行：参数很可能缺尾巴。重新数进
                # messages 会把半截 assistant 消息留在历史里
                # 污染后面的上下文，所以这里不追加它，只当这一轮没发生过，换个提醒重试。
                truncated_in_a_row += 1
                if truncated_in_a_row >= MAX_TRUNCATIONS_IN_A_ROW:
                    why = TRUNCATED_GIVE_UP_NOTE.format(n=truncated_in_a_row)
                    return LoopResult(Outcome.BLOCKED, why, notes=why, steps=steps, transcript=messages)
                messages.append({"role": "user", "content": TRUNCATED_NUDGE})
                continue
            truncated_in_a_row = 0

            if not reply.tool_calls:      # 终止一：模型不再调工具，自然结束（兜底）
                return LoopResult(
                    Outcome.DONE, (reply.content or "").strip() or "（模型直接结束，未调 finish）",
                    notes=NO_TOOL_CALL_NOTE, steps=steps, transcript=messages,
                )

            calls = _function_calls(reply)
            messages.append(_assistant_message(reply, calls))

            # 混批里的 finish 也要等同批全部调用都有 tool_result 才收口，不能中途 return
            finish_result: LoopResult | None = None
            refs_stale = False   # 批内一旦有动作型工具跑过，后面用 ref 的调用就得跳过重来
            for call in calls:
                if self.cancel and self.cancel.is_set():   # 一批里工具调用可能不少，逐条也查一遍
                    raise CancelledError()
                name = call.function.name
                parsed = parse_tool_args_result(call.function.arguments)
                args = parsed.args

                tool = self.registry.get(name)
                problem = parsed.error or self.registry.validate_args(name, args)
                if problem:
                    output = f"工具参数无效：{problem}。handler 未执行，请只修正参数后重试。"
                    step = Step(len(steps) + 1, name, args, output,
                                reply.content or "", elapsed_ms=0, llm_seq=llm_seq)
                    self._record(step, steps)
                    messages.append(_tool_message(call.id, output))
                    continue

                if self.registry.is_terminal(name):   # finish，带结构化产物（主路径）
                    result = self._handle_finish(call.id, args, reply.content or "",
                                                 steps, messages, llm_seq)
                    finish_result = finish_result or result   # 只认同一批里第一个有效的 finish
                    continue

                if refs_stale and tool is not None and "ref" in tool.parameters:
                    step = Step(len(steps) + 1, name, args, REF_STALE_AFTER_MUTATION,
                                reply.content or "", elapsed_ms=0, llm_seq=llm_seq)
                    self._record(step, steps)
                    messages.append(_tool_message(call.id, REF_STALE_AFTER_MUTATION))
                    continue   # 引用已重编号，跳过不执行

                # 调用工具
                output = self._call_tool(ctx, name, args)
                step = Step(len(steps) + 1, name, args, output[:STEP_PREVIEW_CHARS],
                            reply.content or "", elapsed_ms=self._last_elapsed_ms, llm_seq=llm_seq)
                self._record(step, steps)
                messages.append(_tool_message(call.id, output[:TOOL_RESULT_MAX_CHARS]))
                if tool is not None and tool.mutates:
                    refs_stale = True

            if finish_result is not None:
                return finish_result

            # 这一批跑完了，看看环境有没有被推动。只在这批真的碰过浏览器时才算数——
            # 一连读十份资料时页面本来就不会变，那不是卡死。
            touched = any(c.function.name in BROWSER_TOOL_NAMES for c in calls)
            print_now = self.fingerprint() if (self.fingerprint and touched) else None
            if print_now is not None and print_now == last_print:
                stalled_turns += 1
                if stalled_turns >= self.agent.stall_abort_after > 0:
                    return LoopResult(
                        Outcome.BLOCKED,
                        STALL_ABORT.format(n=stalled_turns, where=_where(steps)),
                        notes=STALL_ABORT.format(n=stalled_turns, where=_where(steps)),
                        steps=steps, transcript=messages)
                if stalled_turns == self.agent.stall_nudge_after > 0:
                    messages.append({"role": "user",
                                     "content": STALL_NUDGE.format(n=stalled_turns)})
            elif print_now is not None:
                stalled_turns = 0        # 页面动了，重新计数
            if print_now is not None:
                last_print = print_now

        note = f"（中途压缩过 {compactions} 次上下文）" if compactions else ""
        return LoopResult(Outcome.MAX_ITERATIONS,
                          f"到达最大轮数 {self.agent.max_iterations} 仍未结束{note}",
                          steps=steps, transcript=messages)

    @staticmethod
    def _recent_turns(messages: list[Message], turns: int) -> list[Message]:
        """从末尾往回取 `turns` 轮原文。切点只能落在 assistant 消息上——

        一轮 = 一条带 tool_calls 的 assistant 消息 + 紧跟它的那些 tool 结果。
        从中间切会留下没有 tool_use 的孤儿 tool_result，续跑直接 400。
        """
        if turns <= 0:
            return []
        starts = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
        # **至少丢掉一轮**：一条都没丢的话重建后上下文还是那么大，下一轮又触发压缩 → 死循环。
        keep = min(turns, len(starts) - 1)
        if keep <= 0:
            return []
        return [dict(m) for m in messages[starts[-keep]:]]

    def _compact(self, messages: list[Message], steps: list[Step], llm_seq: int,
                 on_call: LlmCallHook) -> list[Message]:
        """把长对话换成一份交接，重建上下文。

        为什么是「阈值触发、一次性重建」而不是「持续裁剪」：缓存命中的前提是前缀逐字节相同，
        每轮回头改写历史会让前缀永远对不上、缓存全程落空（Codex 源码里那句
        「Trim from the beginning to preserve cache (prefix-based)」就是冲这个来的）。
        所以平时一个字不动，顶到阈值才断一次——代价是这一次缓存要重新写。

        保留的是 system（技能说明，整趟不变）和第一条 user（任务说明，含路书），
        丢掉的是中间所有的助手消息和工具返回。关键事实靠模型自己抄进交接里：
        harness 只存了工具返回的前 300 字预览，拼不出完整事实。
        """
        # on_call 用循环自己那份计数器，不是原始 hook：这一次发的是**完整历史**，
        # 是整趟里最贵的单次调用。用原始 hook 的话它入了库却不计进 spent_tokens，
        # 等于最贵的那次恰好逃过 max_total_tokens 这道闸。
        #
        # tools 必须传真的那份，不能传 []——2026-09-24 压缩阈值第一次真正被触发就实测炸了：
        # Bedrock Converse API 的硬规则是"历史里只要有 toolUse/toolResult，这次请求就必须带
        # toolConfig"，跟这次要不要用工具无关。空列表在网关那层等于没传 toolConfig，直接 400，
        # 而且这个异常没人接，会把整趟跑批带崩成 error（不是优雅的 BLOCKED）。
        # 让模型真的调工具的风险靠提示词挡：compact.md 明写"写完就停，不要调用任何工具"；
        # 万一它还是调了，handoff 只读 .content，tool_calls 会被原样忽略，不会崩。
        handoff = self.llm.chat([*messages, {"role": "user", "content": self.compact_prompt}],
                                tools=self._tools, on_call=on_call)
        summary = (handoff.content or "").strip()
        keep = [dict(m) for m in messages[:2] if m.get("role") in ("system", "user")]
        # 任务说明里那份路书重新渲染一次，取磁盘上的当前版本
        if self.refresh_task and len(keep) > 1 and keep[1].get("role") == "user":
            with contextlib.suppress(Exception):    # 读不出来就沿用旧的，别为了刷新把压缩搞砸
                keep[1]["content"] = self.refresh_task()
        tail = self._recent_turns(messages, self.keep_recent_turns)
        fallback = "（模型没写出交接，只能靠任务说明和路书接着跑）"
        head = (COMPACTED.format(n=llm_seq - 1 - self.keep_recent_turns, kept=self.keep_recent_turns,
                                 summary=summary or fallback)
                if tail else
                COMPACTED_NO_TAIL.format(n=llm_seq - 1, summary=summary or fallback))
        rebuilt: list[Message] = [*keep, {"role": "user", "content": head}, *tail]
        self._record(Step(len(steps) + 1, "compact", {"轮": llm_seq, "原消息数": len(messages)},
                          f"上下文重建：{len(messages)} 条消息 → {len(rebuilt)} 条",
                          summary[:STEP_PREVIEW_CHARS], llm_seq=llm_seq), steps)
        return rebuilt

    def _over_budget(self, started: float, spent_tokens: int, steps: list[Step],
                     messages: list[Message]) -> LoopResult | None:
        """墙上时钟 / token 两道预算闸。都是兜底——真正分辨「卡死」的是原地踏步检测。"""
        elapsed = int(time.monotonic() - started)
        if 0 < self.agent.max_wall_seconds <= elapsed:
            why = WALL_CLOCK_EXCEEDED.format(n=self.agent.max_wall_seconds, where=_where(steps))
            return LoopResult(Outcome.BLOCKED, why, notes=why, steps=steps, transcript=messages)
        if 0 < self.agent.max_total_tokens <= spent_tokens:
            why = TOKENS_EXCEEDED.format(n=self.agent.max_total_tokens, where=_where(steps))
            return LoopResult(Outcome.BLOCKED, why, notes=why, steps=steps, transcript=messages)
        return None

    def _handle_finish(self, call_id: str, args: dict[str, Any], thinking: str,
                       steps: list[Step], messages: list[Message],
                       llm_seq: int = 0) -> LoopResult | None:
        outcome = _outcome_of(args.get("outcome"))
        questions = [str(q) for q in (args.get("questions") or [])]

        # 契约在 harness 层卡住：说要问人就必须真的把问题列出来，否则界面渲染不出回答框。
        # （实测 2026-09-16：模型把问题写进了 notes，questions 留空）
        if outcome is Outcome.NEEDS_INPUT and not questions:
            messages.append(_tool_message(call_id, NEEDS_INPUT_WITHOUT_QUESTIONS))
            return None

        # 同一个道理：说跑完了，就必须把验收路线要引用的事实交齐。缺了现在就退回去补，
        # 别等跑完起了裁判、判分脚本才报「context.yaml 里没有 xxx」——那时整趟都白跑了。
        # blocked / needs_input 不要求交，本来就是没跑到位。
        if outcome is Outcome.DONE and self.required_facts:
            facts = dict(args.get("facts") or {})
            missing = [k for k in self.required_facts if not str(facts.get(k, "")).strip()]
            if missing:
                messages.append(_tool_message(call_id, missing_facts_message(missing)))
                return None

        self._record(Step(len(steps) + 1, "finish", args, str(args.get("summary", "")),
                          thinking, llm_seq=llm_seq), steps)
        # finish 也要补一条 tool_result，否则冻结下来的对话是残的：每个 tool_use 后面必须紧跟
        # 对应的 tool_result，缺了之后就接不上（实测 2026-09-16 续跑时 Bedrock 直接 400）
        messages.append(_tool_message(call_id, f"已收到 finish（outcome={outcome}）"))
        return LoopResult(
            outcome, str(args.get("summary", "")),
            facts=dict(args.get("facts") or {}), notes=str(args.get("notes", "")),
            questions=questions, steps=steps, transcript=messages,
        )

    _last_elapsed_ms = 0

    def _call_tool(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
        started = time.perf_counter()
        try:
            output = self.registry.dispatch(ctx, name, args)
        except Exception as e:
            output = f"工具执行失败: {type(e).__name__}: {e}\n重新 observe 后换个方式。"
        self._last_elapsed_ms = int((time.perf_counter() - started) * 1000)
        return output

    def _record(self, step: Step, steps: list[Step]) -> None:
        steps.append(step)
        if self.on_step:
            self.on_step(step)


def _where(steps: list[Step]) -> str:
    """harness 主动终止时，把「停在哪一步」写进结论——否则报告里只有一句「被终止」。"""
    if not steps:
        return "（还没跑出任何一步）"
    last = steps[-1]
    return f"第 {last.n} 步 {last.tool} {last.args}"


def _outcome_of(raw: Any) -> Outcome:
    try:
        return Outcome(str(raw))
    except ValueError:
        return Outcome.DONE


def _function_calls(reply: Any) -> list[Any]:
    """只保留 function 型调用；openai SDK 的 tool_calls 还可能是 custom 型，我们不接。"""
    return [tc for tc in reply.tool_calls if getattr(tc, "type", "function") == "function"]


def _assistant_message(reply: Any, calls: list[Any]) -> Message:
    return {
        "role": "assistant",
        "content": reply.content,
        "tool_calls": [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in calls
        ],
    }


def _tool_message(call_id: str, content: str) -> Message:
    return {"role": "tool", "tool_call_id": call_id, "content": content}
