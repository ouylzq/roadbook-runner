
from runtime.subagents import SubAgentLoader
from tools import REGISTRY, ToolContext


def _names(schemas):
    return [s["function"]["name"] for s in schemas]


def test_terminal_tool_is_always_kept():
    assert _names(REGISTRY.schemas(["observe"])) == ["observe", "finish"]


def test_unknown_tool_in_whitelist_does_not_kill_a_running_batch():
    """这条契约 2026-09-23 改过：以前抛 ValueError，现在只警告并跳过。
    原因见下面那个用例——抛异常会把跑到一半的跑批整个炸掉，代价不成比例。"""
    names = {s["function"]["name"] for s in REGISTRY.schemas(["observe", "teleport"])}
    assert "observe" in names and "teleport" not in names


def test_terminal_tool_is_not_dispatched():
    assert "终止型" in REGISTRY.dispatch(ToolContext(), "finish", {"outcome": "done"})


def test_unknown_tool_dispatch_is_a_message():
    assert "未知工具" in REGISTRY.dispatch(ToolContext(), "nope", {})


def test_judge_can_click_but_cannot_type_or_write(project_settings):
    """2026-09-24 改过口径：裁判**可以点**，不能打字、不能写。

    以前连 click 一起禁，禁的是工具名，不是能力——弹层和懒加载的内容不点就不渲染，
    裁判照样得进去取证，于是它拿 eval_js 写 `.click()` 绕过去了（20260921-110553
    那次 choice-question，判了 PASS，跑批记录里只剩一句求值，看不出它动过页面）。
    现在边界画在「这一下会不会写」上：到达性的点击走 click（记录里留得下点了哪个元素），
    打字、勾选、写资产、写记忆一概不给。
    """
    judge = SubAgentLoader(project_settings).load("judge")
    names = set(_names(REGISTRY.schemas(judge.tools)))
    assert "click" in names
    assert {"type_text", "click_xy", "write_scenario_file", "upsert_playbook_step", "run_skill_script",
            "propose_atom", "vote_atom"}.isdisjoint(names)
    assert {"observe", "navigate", "find", "grep_page", "read_resource", "finish"} <= names


def test_judge_cannot_click_through_eval_js():
    """堵上那条绕行：read_only 的调用方用 JS 点击会被当场拒绝，并被指回 click。

    挡的是顺手，不是对抗——拼字符串取方法名照样能绕，这里只保证「想点就得走有记录的那条路」。
    """
    from tools.browser.eval_js import EVAL_JS_TOOL

    class _Page:
        def eval_js(self, expr):
            return f"求值了: {expr}"

    judge = ToolContext(_Page(), read_only=True)
    refused = EVAL_JS_TOOL.handler(judge, {"expr": "document.querySelector('button').click()"})
    assert "拒绝" in refused and "click" in refused

    # 读取照常放行
    assert "求值了" in EVAL_JS_TOOL.handler(judge, {"expr": "document.title"})
    # 执行 agent 不受影响：它本来就该能用 JS 派发事件写表单值
    runner = ToolContext(_Page())
    assert "求值了" in EVAL_JS_TOOL.handler(runner, {"expr": "el.click()"})


def test_main_agent_has_full_toolset(project_settings):
    agent = SubAgentLoader(project_settings).load("agent")
    assert set(_names(REGISTRY.schemas(agent.tools))) == set(REGISTRY.names())


def test_unknown_tool_in_whitelist_warns_instead_of_raising(caplog):
    """2026-09-20 实测：给 agents/judge.md 加了个新工具，而跑着的服务进程里的工具表
    还是启动时那份，裁判阶段直接 ValueError，一次已经跑了 48 步的跑批整个报废。

    少一个工具最多让 agent 绕点路；为这个把跑到一半的现场炸掉，代价完全不成比例。
    名字写错由 unknown_tools() 在开跑前报（早失败），这里只负责别把在飞的跑批弄掉下来。"""
    import logging

    from tools import REGISTRY

    with caplog.at_level(logging.WARNING):
        schemas = REGISTRY.schemas(["read_resource", "根本不存在的工具"])

    names = {s["function"]["name"] for s in schemas}
    assert "read_resource" in names            # 认识的照常给
    assert "finish" in names                   # 终止型永远保留，否则循环收不了口
    assert "根本不存在的工具" not in names
    assert "根本不存在的工具" in caplog.text     # 但要留下痕迹，不能静默吞掉


def test_unknown_tools_reports_for_early_failure():
    """开跑前的自检用这个一次性把名字对不上的报出来。"""
    from tools import REGISTRY

    assert REGISTRY.unknown_tools(["read_resource", "finish"]) == []
    assert REGISTRY.unknown_tools(["read_resource", "zzz", "aaa"]) == ["aaa", "zzz"]


def test_registry_uses_full_json_schema_before_handler():
    from tools import Tool, ToolContext, ToolRegistry

    called = []
    tool = Tool(
        name="nested",
        description="test",
        parameters={"payload": {"type": "object", "additionalProperties": False,
                                "properties": {"name": {"type": "string", "minLength": 1}},
                                "required": ["name"]}},
        required=("payload",),
        handler=lambda _ctx, _args: called.append(1) or "ok",
    )
    out = ToolRegistry([tool]).dispatch(ToolContext(), "nested", {"payload": {"extra": 1}})
    assert "handler 未执行" in out and called == []
