from tools.base import parse_tool_args, parse_tool_args_result, repair_leaked_args

# 2026-09-16 实测抓到的泄漏样本：facts 被吞进 notes
LEAKED = {
    "outcome": "done",
    "summary": "按路书跑通",
    "notes": '路书全程一致。</notes>\n<parameter name="facts">{"task_titles": ["a", "b"], "done_title": "a"}',
}


def test_leaked_facts_are_pulled_back_to_top_level():
    fixed = repair_leaked_args(LEAKED)
    assert fixed["facts"] == {"task_titles": ["a", "b"], "done_title": "a"}
    assert fixed["notes"] == "路书全程一致。"


def test_existing_top_level_key_is_not_overwritten():
    fixed = repair_leaked_args({**LEAKED, "facts": {"keep": 1}})
    assert fixed["facts"] == {"keep": 1}


def test_non_json_leak_is_kept_as_string():
    fixed = repair_leaked_args({"notes": 'x</notes>\n<parameter name="hint">纯文本</parameter>'})
    assert fixed["hint"] == "纯文本"


def test_clean_args_untouched():
    assert repair_leaked_args({"a": "no leak", "b": 1}) == {"a": "no leak", "b": 1}


def test_parse_tool_args_tolerates_bad_json():
    assert parse_tool_args("{not json") == {}
    assert parse_tool_args(None) == {}
    assert parse_tool_args('["a list"]') == {}


def test_parse_tool_args_result_preserves_parse_failure():
    parsed = parse_tool_args_result('{"content":"half')
    assert parsed.args == {} and "不是完整 JSON" in (parsed.error or "")
