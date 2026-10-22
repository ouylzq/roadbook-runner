"""用假 LLM 锁住循环层的契约——这些都是本次开发里真踩过的坑。"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from runtime.cancel import CancelledError
from runtime.domain import Outcome
from runtime.llm import LlmCallInfo
from runtime.loop import NEEDS_INPUT_WITHOUT_QUESTIONS, REF_STALE_AFTER_MUTATION, AgentLoop
from runtime.subagents import SubAgent
from tools import ToolContext


def call(name: str, cid: str = "c1", **args):
    return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def reply(*calls, content: str = "", stop_reason: str = "tool_calls"):
    return SimpleNamespace(content=content, tool_calls=list(calls), stop_reason=stop_reason)


class ScriptedLLM:
    """假 LLM。真实的 `LLM.chat` 会在拿到回复后调一次 on_call，这里照做——
    循环层要靠它累计 token（预算闸）、判断输出是不是被截断，所以"有没有真的被调到、
    带没带对 stop_reason"是要测的契约。"""

    def __init__(self, *replies, prompt_tokens: int = 0):
        self.replies = list(replies)
        self.prompt_tokens = prompt_tokens
        self.seen: list[list[dict]] = []
        self.on_call_seen: list[object] = []

    def chat(self, messages, tools, *, on_call=None):
        self.seen.append([dict(m) for m in messages])
        self.on_call_seen.append(on_call)
        nxt = self.replies[0]
        if on_call:
            on_call(LlmCallInfo(model="fake", prompt_tokens=self.prompt_tokens,
                                completion_tokens=0, latency_ms=1, stop_reason=nxt.stop_reason))
        return self.replies.pop(0)


AGENT = SubAgent(name="t", description="", prompt="P", tools=("read_resource",), max_iterations=5)


def test_needs_input_without_questions_is_rejected_then_accepted(skill):
    llm = ScriptedLLM(
        reply(call("finish", outcome="needs_input", summary="卡住")),
        reply(call("finish", "c2", outcome="needs_input", summary="卡住", questions=["选 a 还是 b？"])),
    )
    result = AgentLoop(AGENT, llm).run(ToolContext(skill=skill), "S", "T")
    assert result.outcome is Outcome.NEEDS_INPUT and result.questions == ["选 a 还是 b？"]
    rejection = llm.seen[1][-1]
    assert rejection["role"] == "tool" and rejection["content"] == NEEDS_INPUT_WITHOUT_QUESTIONS


def test_finish_leaves_well_formed_transcript(skill):
    """每个 tool_use 后必须紧跟 tool_result，否则续跑时 Bedrock 直接 400。"""
    llm = ScriptedLLM(reply(call("finish", outcome="done", summary="ok")))
    result = AgentLoop(AGENT, llm).run(ToolContext(skill=skill), "S", "T")
    last, prev = result.transcript[-1], result.transcript[-2]
    assert prev["role"] == "assistant" and prev["tool_calls"][0]["id"] == "c1"
    assert last["role"] == "tool" and last["tool_call_id"] == "c1"


def test_truncated_reply_is_nudged_and_retried_not_treated_as_natural_end(skill):
    """输出撞 max_tokens 被腰斩时 tool_calls 是空的，跟"模型真决定不调工具了"长得一样——
    2026-09-24 实测栽过：裁判倒数第二轮被截断，整场 29 次调用被判"模型直接结束"，
    判分脚本因为没等到 judgment 把每条 check 都写成 BLOCK。区分靠 stop_reason。"""
    llm = ScriptedLLM(
        reply(stop_reason="length"),                            # 被截断，没有 tool_calls
        reply(call("finish", outcome="done", summary="ok")),    # 重试之后正常收口
    )
    result = AgentLoop(AGENT, llm).run(ToolContext(skill=skill), "S", "T")
    assert result.outcome is Outcome.DONE and result.summary == "ok"
    assert len(llm.seen) == 2
    # 截断那轮没有产出任何工具调用，不该在历史里留下一条没有 tool_result 配对的 assistant 消息
    nudge = llm.seen[1][-1]
    assert nudge["role"] == "user" and "截断" in nudge["content"]
    assert not any(m.get("role") == "assistant" and not m.get("content") for m in llm.seen[1])


def test_truncated_reply_with_partial_tool_call_is_never_executed(skill):
    """真实事故：stop_reason=length 仍带着一个缺 content 的半截 write 调用，不能进 handler。"""
    partial = call("write_scenario_file", scenario="with-playbook", filename="intent.md")
    llm = ScriptedLLM(
        reply(partial, stop_reason="length"),
        reply(call("finish", "c2", outcome="done", summary="ok")),
    )
    before = (skill.root / "scenarios" / "with-playbook" / "intent.md").read_text()
    result = AgentLoop(AGENT, llm).run(ToolContext(skill=skill), "S", "T")
    assert result.outcome is Outcome.DONE
    assert (skill.root / "scenarios" / "with-playbook" / "intent.md").read_text() == before
    assert not any(step.tool == "write_scenario_file" for step in result.steps)


def test_truncated_twice_in_a_row_gives_up_as_blocked_not_silently_done(skill):
    llm = ScriptedLLM(reply(stop_reason="length"), reply(stop_reason="length"))
    result = AgentLoop(AGENT, llm).run(ToolContext(skill=skill), "S", "T")
    assert result.outcome is Outcome.BLOCKED
    assert "2" in result.notes and "上限" in result.notes
    assert len(llm.seen) == 2          # 撞够次数就不再重试，不然会一直提醒到轮数耗尽


def test_truncation_streak_resets_after_a_normal_call(skill):
    """截断一次、正常一次、又截断一次——不该被当成"连续两次"合并计数，
    每次正常调用之间的截断都要重新给够重试机会。"""
    llm = ScriptedLLM(
        reply(stop_reason="length"),
        reply(call("read_resource", "c1", path="scenarios/with-playbook/playbook.md")),
        reply(stop_reason="length"),
        reply(call("finish", "c2", outcome="done", summary="ok")),
    )
    result = AgentLoop(AGENT, llm).run(ToolContext(skill=skill), "S", "T")
    assert result.outcome is Outcome.DONE
    assert len(llm.seen) == 4


def test_tool_output_is_fed_back_and_recorded(skill):
    llm = ScriptedLLM(
        reply(call("read_resource", path="scenarios/with-playbook/playbook.md")),
        reply(call("finish", "c2", outcome="done", summary="读到了")),
    )
    result = AgentLoop(AGENT, llm).run(ToolContext(skill=skill), "S", "T")
    assert [s.tool for s in result.steps] == ["read_resource", "finish"]
    fed_back = llm.seen[1][-1]
    assert fed_back["role"] == "tool" and "# 路书" in fed_back["content"]


def test_tool_exception_is_returned_not_raised(skill, monkeypatch):
    from tools import REGISTRY

    def boom(ctx, name, args):
        raise RuntimeError("browser died")

    monkeypatch.setattr(REGISTRY, "dispatch", boom)
    llm = ScriptedLLM(reply(call("read_resource", path="x")), reply(call("finish", "c2", outcome="done", summary="")))
    result = AgentLoop(AGENT, llm).run(ToolContext(skill=skill), "S", "T")
    assert "工具执行失败" in result.steps[0].result_preview


def test_no_tool_calls_ends_with_note(skill):
    result = AgentLoop(AGENT, ScriptedLLM(reply(content="我说完了"))).run(ToolContext(skill=skill), "S", "T")
    assert result.outcome is Outcome.DONE and result.summary == "我说完了" and "finish" in result.notes


def test_max_iterations_is_enforced(skill):
    loops = [reply(call("read_resource", f"c{i}", path="nope")) for i in range(5)]
    result = AgentLoop(AGENT, ScriptedLLM(*loops)).run(ToolContext(skill=skill), "S", "T")
    assert result.outcome is Outcome.MAX_ITERATIONS and len(result.steps) == 5


def test_zero_max_iterations_has_no_landing_warning_or_hard_cap(skill):
    agent = SubAgent(name="unlimited", description="", prompt="P",
                     tools=("read_resource",), max_iterations=0,
                     max_wall_seconds=0, max_total_tokens=0)
    replies = [reply(call("read_resource", f"c{i}", path="nope")) for i in range(1, 66)]
    replies.append(reply(call("finish", "done", outcome="done", summary="65轮后完成")))
    llm = ScriptedLLM(*replies)

    result = AgentLoop(agent, llm).run(ToolContext(skill=skill), "S", "T")

    assert result.outcome is Outcome.DONE and result.summary == "65轮后完成"
    assert len(llm.seen) == 66
    assert not any("轮数提醒" in str(message) for turn in llm.seen for message in turn)


def test_resume_appends_reply_to_transcript(skill):
    llm = ScriptedLLM(reply(call("finish", outcome="done", summary="接上了")))
    frozen = [{"role": "system", "content": "S"}, {"role": "user", "content": "T"}]
    AgentLoop(AGENT, llm).resume(ToolContext(skill=skill), frozen, "人回答：选 a")
    assert llm.seen[0][-1] == {"role": "user", "content": "人回答：选 a"}
    assert len(llm.seen[0]) == 3


@pytest.mark.parametrize("raw,expected", [("done", Outcome.DONE), ("blocked", Outcome.BLOCKED)])
def test_known_outcome_is_accepted(skill, raw, expected):
    llm = ScriptedLLM(reply(call("finish", outcome=raw, summary="")))
    assert AgentLoop(AGENT, llm).run(ToolContext(skill=skill), "S", "T").outcome is expected


def test_unknown_outcome_is_rejected_before_finish_handler(skill):
    llm = ScriptedLLM(
        reply(call("finish", outcome="garbage", summary="")),
        reply(call("finish", "c2", outcome="done", summary="fixed")),
    )
    result = AgentLoop(AGENT, llm).run(ToolContext(skill=skill), "S", "T")
    assert result.outcome is Outcome.DONE and result.summary == "fixed"
    assert "值无效" in llm.seen[1][-1]["content"]


def test_on_llm_call_still_reaches_the_caller_hook(skill):
    """循环层自己要截一道调用记录来累计 token（预算闸），但调用方传进来的 hook
    必须照样收到原样的 LlmCallInfo——截这一道不能把外面的记录弄丢。"""
    llm = ScriptedLLM(reply(call("finish", outcome="done", summary="ok")), prompt_tokens=123)
    got: list[LlmCallInfo] = []
    AgentLoop(AGENT, llm, on_llm_call=got.append).run(ToolContext(skill=skill), "S", "T")
    assert [i.prompt_tokens for i in got] == [123]


def test_no_on_llm_call_hook_is_fine(skill):
    """调用方不关心调用记录时，循环自己那道计数照跑，不能因为没 hook 就炸。"""
    llm = ScriptedLLM(reply(call("finish", outcome="done", summary="ok")), prompt_tokens=50)
    result = AgentLoop(AGENT, llm).run(ToolContext(skill=skill), "S", "T")
    assert result.outcome is Outcome.DONE


def test_finish_mid_batch_does_not_orphan_later_tool_calls(skill):
    """模型在同一轮里把 finish 和别的工具调用混在一起、finish 不是最后一个：
    2026-09-17 实测过，后面的调用会被静默丢弃，且遗留一个没有 tool_result 的 tool_use，
    冻结后续跑会直接 400。这里用同样的场景锁住修复。"""
    llm = ScriptedLLM(reply(
        call("read_resource", "c1", path="scenarios/with-playbook/intent.md"),
        call("finish", "c2", outcome="done", summary="做完了"),
        call("read_resource", "c3", path="scenarios/with-playbook/playbook.md"),
    ))
    result = AgentLoop(AGENT, llm).run(ToolContext(skill=skill), "S", "T")

    tool_use_ids = {tc["id"] for m in result.transcript if m["role"] == "assistant"
                   for tc in m.get("tool_calls", [])}
    tool_result_ids = {m["tool_call_id"] for m in result.transcript if m["role"] == "tool"}
    assert tool_use_ids == tool_result_ids == {"c1", "c2", "c3"}   # 一个都不能少

    assert result.outcome is Outcome.DONE
    assert [s.tool for s in result.steps] == ["read_resource", "finish", "read_resource"]
    assert "playbook.md" not in "".join(s.args.get("path", "") for s in result.steps[:1])  # c1 先跑
    # c3 真的执行了，不是被跳过——它的 tool_result 里能看到 playbook 内容
    c3_result = next(m["content"] for m in result.transcript if m.get("tool_call_id") == "c3")
    assert "# 路书" in c3_result


def test_batched_ref_after_mutation_is_skipped_not_executed(skill):
    """2026-09-17 实测过：驱动实现每做一次动作就把页面全部引用重新编号，
    同一批里第二个 click 用的 ref 早已指向别的元素了。这里锁住修复：
    批次内一旦有动作型工具跑过，后面再用 ref 的调用一律跳过，不会真的去点。"""
    browser_agent = SubAgent(name="b", description="", prompt="P",
                             tools=("click", "type_text"), max_iterations=5)
    llm = ScriptedLLM(
        reply(
            call("click", "c1", ref="ref_3"),
            call("type_text", "c2", ref="ref_5", text="hi"),
        ),
        reply(call("finish", "c3", outcome="done", summary="ok")),
    )
    result = AgentLoop(browser_agent, llm).run(ToolContext(skill=skill), "S", "T")

    assert [s.tool for s in result.steps[:2]] == ["click", "type_text"]
    click_result = next(m["content"] for m in result.transcript if m.get("tool_call_id") == "c1")
    skipped_result = next(m["content"] for m in result.transcript if m.get("tool_call_id") == "c2")
    assert click_result != REF_STALE_AFTER_MUTATION   # 第一个真的执行了（没浏览器，返回提示语）
    assert skipped_result == REF_STALE_AFTER_MUTATION  # 第二个被跳过，不是真的点了


def test_multiple_finish_calls_in_one_batch_all_get_tool_results(skill):
    """极端情况：模型一次性调了两个 finish。只认第一个当结果，但两个都要有 tool_result。"""
    llm = ScriptedLLM(reply(
        call("finish", "c1", outcome="done", summary="第一个"),
        call("finish", "c2", outcome="blocked", summary="第二个"),
    ))
    result = AgentLoop(AGENT, llm).run(ToolContext(skill=skill), "S", "T")
    assert result.summary == "第一个"   # 以第一个为准
    tool_result_ids = {m["tool_call_id"] for m in result.transcript if m["role"] == "tool"}
    assert tool_result_ids == {"c1", "c2"}   # 两个都有回应，没有遗留


def test_landing_warning_is_injected_before_budget_runs_out(skill):
    """2026-09-17 实测：裁判 30 轮烧光没调 finish，judgment 是空的。
    剩 LANDING_TURNS 轮时要提醒一次「必须收口」，最后一轮再拦一次「只能 finish」。"""
    from runtime.loop import LANDING_TURNS, LAST_TURN_WARNING

    agent = SubAgent(name="t", description="", prompt="P", tools=("read_resource",), max_iterations=6)
    llm = ScriptedLLM(*[reply(call("read_resource", f"c{i}", path="nope")) for i in range(6)])
    AgentLoop(agent, llm).run(ToolContext(skill=skill), "S", "T")

    def user_notes(k):   # 第 k 次调模型时，对话里有哪些 user 消息（去掉最初的任务）
        return [m["content"] for m in llm.seen[k] if m["role"] == "user"][1:]

    # 第 1 轮还剩 6，没提醒；第 2 轮剩 5 → 提醒；最后一轮（第 6）→ 拦
    assert user_notes(0) == []
    assert any(f"还剩 {LANDING_TURNS} 轮" in n for n in user_notes(1))
    assert user_notes(5)[-1] == LAST_TURN_WARNING
    # 提醒各只出现一次，不是每轮都刷
    all_notes = user_notes(5)
    assert sum("还剩" in n for n in all_notes) == 1 and all_notes.count(LAST_TURN_WARNING) == 1


def test_finish_missing_required_facts_is_rejected_then_accepted(skill):
    """验收路线引用了 context.draft_id，runner 却没在 facts 里交——harness 当场打回重交。

    20 次历史跑批里「context.yaml 里没有 xxx」是最大单一失败原因（4 次）。以前要等整趟跑完、
    起完裁判、判分脚本才报出来，那时浏览器早关了。现在退回去时浏览器还开着，补一个值就行。
    """
    llm = ScriptedLLM(
        reply(call("finish", outcome="done", summary="跑完了", facts={"project_id": "1"})),
        reply(call("finish", "c2", outcome="done", summary="跑完了",
                   facts={"project_id": "1", "draft_id": "2"})),
    )
    loop = AgentLoop(AGENT, llm, required_facts=["draft_id", "project_id"])
    result = loop.run(ToolContext(skill=skill), "S", "T")

    assert result.outcome is Outcome.DONE
    assert result.facts == {"project_id": "1", "draft_id": "2"}
    rejection = llm.seen[1][-1]
    assert rejection["role"] == "tool" and "draft_id" in rejection["content"]
    assert "project_id" not in rejection["content"]          # 只点名缺的那个


def test_blocked_finish_does_not_require_facts(skill):
    """blocked / needs_input 本来就是没跑到位，不该拿事实清单卡它。"""
    llm = ScriptedLLM(reply(call("finish", outcome="blocked", summary="环境挂了")))
    result = AgentLoop(AGENT, llm, required_facts=["draft_id"]).run(ToolContext(skill=skill), "S", "T")
    assert result.outcome is Outcome.BLOCKED


def _browsing_agent(**kw) -> SubAgent:
    return SubAgent(name="b", description="", prompt="P",
                    tools=("observe", "wait"), max_iterations=12, **kw)


def test_stall_nudges_then_aborts_when_the_page_never_changes(skill):
    """2026-09-21 实测过的失败形态：页面定格，agent 换着花样探查了 35 轮。
    动作指纹每轮都不同、一次报错都没有，所以「同动作重复」那套检测抓不到它——
    指纹必须取在环境这一侧。这里锁住：页面不变就先提醒、再终止。"""
    llm = ScriptedLLM(*[reply(call("wait", f"c{i}", seconds=5)) for i in range(12)])
    agent = _browsing_agent(stall_nudge_after=2, stall_abort_after=4)
    loop = AgentLoop(agent, llm, fingerprint=lambda: "同一张页面")
    result = loop.run(ToolContext(skill=skill), "S", "T")

    assert result.outcome is Outcome.BLOCKED
    assert "连续 4 轮没有任何变化" in result.summary
    assert "wait" in result.summary                      # 说清停在哪一步
    nudges = [m for m in result.transcript
              if m.get("role") == "user" and "页面已经连续" in str(m.get("content"))]
    assert len(nudges) == 1                              # 提醒只插一次，不是每轮都刷


def test_stall_counter_resets_when_the_page_actually_moves(skill):
    """页面动了就得重新计数，否则跑得久一点的场景迟早被误杀。"""
    pages = iter(["a", "a", "b", "b", "c", "c", "d", "d"])   # 每两轮推进一次，始终没连着不动 3 轮
    llm = ScriptedLLM(*[reply(call("wait", f"c{i}", seconds=1)) for i in range(6)],
                      reply(call("finish", "cz", outcome="done", summary="走完了")))
    agent = _browsing_agent(stall_nudge_after=2, stall_abort_after=3)
    loop = AgentLoop(agent, llm, fingerprint=lambda: next(pages))
    result = loop.run(ToolContext(skill=skill), "S", "T")
    assert result.outcome is Outcome.DONE   # 中途变过，连续计数没攒够 3


def test_non_browser_turns_do_not_count_as_stalled(skill):
    """连读十份资料时页面本来就不会变，那不是卡死——只有真碰过浏览器的轮次才计数。"""
    llm = ScriptedLLM(*[reply(call("read_resource", f"c{i}", path="nope")) for i in range(6)],
                      reply(call("finish", "cz", outcome="done", summary="读完了")))
    agent = SubAgent(name="r", description="", prompt="P",
                     tools=("read_resource",), max_iterations=12,
                     stall_nudge_after=2, stall_abort_after=3)
    loop = AgentLoop(agent, llm, fingerprint=lambda: "页面一直没动")
    assert loop.run(ToolContext(skill=skill), "S", "T").outcome is Outcome.DONE


def test_no_fingerprint_provider_disables_stall_detection(skill):
    """没浏览器就没有环境指纹，检测整个关掉，不能凭空误判。"""
    llm = ScriptedLLM(*[reply(call("wait", f"c{i}", seconds=1)) for i in range(12)])
    agent = _browsing_agent(stall_nudge_after=2, stall_abort_after=3)
    result = AgentLoop(agent, llm).run(ToolContext(skill=skill), "S", "T")
    assert result.outcome is Outcome.MAX_ITERATIONS     # 老行为：烧完轮数


def test_token_budget_stops_the_loop(skill):
    llm = ScriptedLLM(*[reply(call("wait", f"c{i}", seconds=1)) for i in range(12)],
                      prompt_tokens=400)
    agent = _browsing_agent(max_total_tokens=1000)
    result = AgentLoop(agent, llm).run(ToolContext(skill=skill), "S", "T")
    assert result.outcome is Outcome.BLOCKED and "prompt token" in result.summary
    assert len(llm.seen) == 3        # 第 3 轮开头累计 1200 > 1000，不再调第 4 次


def test_budgets_default_to_off(skill):
    """SubAgent 不填这些字段时一律是 0 = 不限，老行为不变。"""
    agent = SubAgent(name="x", description="", prompt="P", tools=("wait",), max_iterations=2)
    assert agent.max_total_tokens == 0 and agent.max_wall_seconds == 0
    assert agent.stall_nudge_after == 0 and agent.stall_abort_after == 0
    llm = ScriptedLLM(*[reply(call("wait", f"c{i}", seconds=1)) for i in range(2)],
                      prompt_tokens=10**9)
    result = AgentLoop(agent, llm, fingerprint=lambda: "不变").run(ToolContext(skill=skill), "S", "T")
    assert result.outcome is Outcome.MAX_ITERATIONS


def test_cancel_before_next_llm_call_stops_the_loop(skill):
    """点了「停止」：下一次要调模型之前先查一眼信号，查到就不再调了。"""
    cancel = threading.Event()
    cancel.set()   # 模拟：调用方在第一轮开始前就已经喊停
    llm = ScriptedLLM(reply(call("finish", outcome="done", summary="不该跑到这")))
    loop = AgentLoop(AGENT, llm, cancel=cancel)
    with pytest.raises(CancelledError):
        loop.run(ToolContext(skill=skill), "S", "T")
    assert llm.seen == []   # 模型压根没被调用


def test_cancel_mid_batch_stops_before_later_tool_calls(skill):
    """一批里排了好几个工具调用：查到停止信号要立刻收手，不能等这一批跑完。"""
    cancel = threading.Event()

    def boom(ctx, name, args):
        cancel.set()   # 第一个工具调用一执行完，外部就喊停（模拟点了「停止」按钮）
        return "ok"

    from tools import REGISTRY
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(REGISTRY, "dispatch", boom)
        browser_agent = SubAgent(name="b", description="", prompt="P",
                                 tools=("read_resource",), max_iterations=5)
        llm = ScriptedLLM(reply(
            call("read_resource", "c1", path="x"),
            call("read_resource", "c2", path="y"),
        ))
        loop = AgentLoop(browser_agent, llm, cancel=cancel)
        with pytest.raises(CancelledError):
            loop.run(ToolContext(skill=skill), "S", "T")


def _compacting_agent(**kw) -> SubAgent:
    return SubAgent(name="c", description="", prompt="P", tools=("read_resource",),
                    max_iterations=8, **kw)


def test_compaction_rebuilds_context_at_threshold(skill):
    """上下文顶到阈值就整段重建。为什么是「阈值触发、一次性重建」而不是「持续裁剪」：
    缓存命中的前提是前缀逐字节相同，每轮回头改写历史会让缓存全程落空。"""
    llm = ScriptedLLM(
        reply(call("read_resource", "c1", path="x")),          # 第 1 轮：报 9999 tokens
        reply(content="交接：已完成第 1 步，project_id=625226294103158784，下一步点提交"),  # 压缩调用
        reply(call("finish", "c2", outcome="done", summary="ok")),
        prompt_tokens=9999,
    )
    loop = AgentLoop(_compacting_agent(), llm, compact_at=5000, compact_prompt="写交接")
    result = loop.run(ToolContext(skill=skill), "S", "T")

    assert result.outcome is Outcome.DONE
    # 第 3 次调模型时，上下文已经被换成 [system, 任务, 交接]
    rebuilt = llm.seen[2]
    assert len(rebuilt) == 3
    assert [m["role"] for m in rebuilt] == ["system", "user", "user"]
    assert "625226294103158784" in rebuilt[-1]["content"]      # 关键事实被带过来了
    assert "引用编号全部作废" in rebuilt[-1]["content"]           # 明确告诉它 ref 没了
    assert any(s.tool == "compact" for s in result.steps)      # 这件事要在轨迹里看得见


def test_compaction_never_leaves_a_dangling_tool_use(skill):
    """重建只发生在**轮与轮之间**——上一批工具调用已经全部有 tool_result 了。
    切在批中间会留下没有 tool_result 的 tool_use，续跑直接 400（2026-09-16 踩过）。"""
    llm = ScriptedLLM(
        reply(call("read_resource", "c1", path="x"), call("read_resource", "c2", path="y")),
        reply(content="交接"),
        reply(call("finish", "c3", outcome="done", summary="ok")),
        prompt_tokens=9999,
    )
    loop = AgentLoop(_compacting_agent(), llm, compact_at=5000, compact_prompt="写交接")
    result = loop.run(ToolContext(skill=skill), "S", "T")

    # 压缩发生前那一轮（第 1 次调用之后）：两个 tool_use 都配上了 tool_result
    before = llm.seen[1]          # 压缩调用看到的就是压缩前的完整历史
    uses = {tc["id"] for m in before if m.get("role") == "assistant" for tc in m.get("tool_calls", [])}
    results = {m["tool_call_id"] for m in before if m.get("role") == "tool"}
    assert uses == results == {"c1", "c2"}
    assert result.outcome is Outcome.DONE


def test_no_compact_prompt_disables_compaction(skill):
    """没给交接提示词就等于关掉——非浏览器场景和单测都走这条路，不能凭空压缩。"""
    llm = ScriptedLLM(*[reply(call("read_resource", f"c{i}", path="x")) for i in range(8)],
                      prompt_tokens=10**6)
    loop = AgentLoop(_compacting_agent(), llm, compact_at=1000)     # 阈值给了，提示词没给
    result = loop.run(ToolContext(skill=skill), "S", "T")
    assert result.outcome is Outcome.MAX_ITERATIONS
    assert not any(s.tool == "compact" for s in result.steps)


def test_compaction_call_counts_toward_the_token_budget(skill):
    """压缩那次发的是**完整历史**，是整趟里最贵的单次调用。
    它必须计进 spent_tokens——否则最贵的那次恰好逃过 max_total_tokens 这道闸。"""
    llm = ScriptedLLM(
        reply(call("read_resource", "c1", path="x")),
        reply(content="交接"),                      # ← 压缩调用
        reply(call("finish", "c2", outcome="done", summary="ok")),
        prompt_tokens=1000,
    )
    seen: list[LlmCallInfo] = []
    loop = AgentLoop(_compacting_agent(), llm, on_llm_call=seen.append,
                     compact_at=500, compact_prompt="写交接")
    loop.run(ToolContext(skill=skill), "S", "T")
    assert len(seen) == 3          # 压缩那次也走了记录钩子，不是记在账外


def test_compaction_call_still_gets_the_real_tools(skill):
    """压缩那次调用不能传空工具列表——2026-09-24 阈值第一次真正触发就实测炸了：
    Bedrock 的硬规则是"历史里只要有 toolUse/toolResult，这次请求就必须带 toolConfig"，
    跟这次要不要用工具无关；传 [] 在网关那层等于没传，直接 400，还没人接，
    会把整趟跑批带崩成 error。不让模型真的动手靠的是 compact.md 的提示词
    （"写完就停，不要调用任何工具"），不是把工具摘掉。"""
    tools_each_call: list[list] = []

    class Watching(ScriptedLLM):
        def chat(self, messages, tools, *, on_call=None):
            tools_each_call.append(tools)
            return super().chat(messages, tools, on_call=on_call)

    llm = Watching(
        reply(call("read_resource", "c1", path="x")),
        reply(content="交接"),
        reply(call("finish", "c2", outcome="done", summary="ok")),
        prompt_tokens=1000,
    )
    AgentLoop(_compacting_agent(), llm, compact_at=500, compact_prompt="写交接").run(
        ToolContext(skill=skill), "S", "T")
    assert tools_each_call[1] == tools_each_call[0] == tools_each_call[2]   # 三次给的是同一份工具表
    assert tools_each_call[1]                                              # 而且确实不是空的


def test_compaction_ignores_tool_calls_if_the_model_disobeys(skill):
    """压缩那次现在带着真工具表，理论上模型可能没听提示词、真调了个工具——
    交接只读 .content，多出来的 tool_calls 原样丢掉，不该让压缩本身崩掉。"""
    llm = ScriptedLLM(
        reply(call("read_resource", "c1", path="x")),
        reply(call("read_resource", "c2", path="y"), content="交接内容"),   # 没听话，但也写了交接
        reply(call("finish", "c3", outcome="done", summary="ok")),
        prompt_tokens=1000,
    )
    result = AgentLoop(_compacting_agent(), llm, compact_at=500, compact_prompt="写交接").run(
        ToolContext(skill=skill), "S", "T")
    assert result.outcome is Outcome.DONE
    assert any("交接内容" in str(m.get("content")) for m in result.transcript)


def test_compaction_re_renders_the_task_so_the_playbook_is_current(skill):
    """路书是 agent 边勘边记的资产：开跑时 4 步，压缩时可能已经 6 步了。
    原样复用开跑那份任务说明，等于把它自己刚记下的两步又藏起来。"""
    llm = ScriptedLLM(
        reply(call("read_resource", "c1", path="x")),
        reply(content="交接"),
        reply(call("finish", "c2", outcome="done", summary="ok")),
        prompt_tokens=1000,
    )
    loop = AgentLoop(_compacting_agent(), llm, compact_at=500, compact_prompt="写交接",
                     refresh_task=lambda: "任务说明（路书已经记到第 6 步）")
    loop.run(ToolContext(skill=skill), "S", "T")

    rebuilt = llm.seen[2]
    assert rebuilt[0]["role"] == "system"                  # 技能说明原样
    assert rebuilt[1]["content"] == "任务说明（路书已经记到第 6 步）"   # 任务说明是新渲染的
    assert "交接" in rebuilt[2]["content"]


def test_refresh_failure_falls_back_to_the_original_task(skill):
    """重新渲染读的是磁盘文件，可能失败。失败就沿用旧的——
    为了刷新一份路书把整个压缩搞砸，不值得。"""
    def boom() -> str:
        raise OSError("磁盘读不出来")

    llm = ScriptedLLM(
        reply(call("read_resource", "c1", path="x")),
        reply(content="交接"),
        reply(call("finish", "c2", outcome="done", summary="ok")),
        prompt_tokens=1000,
    )
    loop = AgentLoop(_compacting_agent(), llm, compact_at=500, compact_prompt="写交接",
                     refresh_task=boom)
    result = loop.run(ToolContext(skill=skill), "S", "T")
    assert result.outcome is Outcome.DONE
    assert llm.seen[2][1]["content"] == "T"        # 沿用开跑时那份，压缩照常完成


def test_compaction_keeps_recent_turns_verbatim(skill):
    """只留摘要的话，当前页面的完整观测（结构树、可见文本、ref 编号）就没了——
    摘要复述不出来，压缩之后必然浪费一轮重新 observe。
    抄 OpenCode 的 preserveRecentBudget：按 turn 边界留最近几轮原文。"""
    llm = ScriptedLLM(
        reply(call("read_resource", "c1", path="x")),
        reply(call("read_resource", "c2", path="y")),
        reply(call("read_resource", "c3", path="z")),
        reply(content="交接"),
        reply(call("finish", "c9", outcome="done", summary="ok")),
        prompt_tokens=1000,
    )
    agent = SubAgent(name="c", description="", prompt="P", tools=("read_resource",),
                     max_iterations=8, keep_recent_turns=2)
    loop = AgentLoop(agent, llm, compact_at=500, compact_prompt="写交接")
    # 压缩发生在第 2 轮开头（第 1 轮报了 1000 tokens），那时只有 1 条 assistant，不够 2 轮 → 不留尾
    result = loop.run(ToolContext(skill=skill), "S", "T")
    assert result.outcome is Outcome.DONE


def test_recent_turns_cut_only_at_assistant_boundaries():
    """切点只能落在 assistant 消息上：一轮 = 一条带 tool_calls 的 assistant + 紧跟的 tool 结果。
    从中间切会留下没有 tool_use 的孤儿 tool_result，续跑直接 400。"""
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "T"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a1"}]},
        {"role": "tool", "tool_call_id": "a1", "content": "r1"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "b1"}, {"id": "b2"}]},
        {"role": "tool", "tool_call_id": "b1", "content": "r2"},
        {"role": "tool", "tool_call_id": "b2", "content": "r3"},
    ]
    tail = AgentLoop._recent_turns(msgs, 1)
    assert [m["role"] for m in tail] == ["assistant", "tool", "tool"]     # 从 assistant 起，配平
    uses = {tc["id"] for m in tail if m.get("role") == "assistant" for tc in m["tool_calls"]}
    results = {m["tool_call_id"] for m in tail if m.get("role") == "tool"}
    assert uses == results == {"b1", "b2"}

    # 要留 2 轮但一共只有 2 轮 → 只留 1 轮。**至少丢掉一轮**：一条都不丢的话重建后
    # 上下文还是那么大，下一轮又触发压缩，死循环。
    assert len(AgentLoop._recent_turns(msgs, 2)) == 3
    assert len(AgentLoop._recent_turns(msgs, 99)) == 3     # 想留多少都一样，至少丢一轮
    assert AgentLoop._recent_turns(msgs, 0) == []          # 关掉
    assert AgentLoop._recent_turns(msgs[:4], 1) == []      # 只有一轮时不留，否则等于没压缩

    # 要留 2 轮但一共只有 2 轮：只留 1 轮。**至少丢掉一轮**——一条都不丢的话，
    # 重建后上下文还是那么大，下一轮又触发压缩，死循环。
    assert len(AgentLoop._recent_turns(msgs, 2)) == 3
    assert AgentLoop._recent_turns(msgs, 0) == []                         # 关掉
    assert AgentLoop._recent_turns(msgs, 9) == [dict(m) for m in msgs[4:]]                                       # 想留 9 轮也只留得到 1 轮
