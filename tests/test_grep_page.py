"""grep_page 的输出契约。真浏览器行为（跳过 script、能搜折叠区）已在真实页面上验过，
这里用假驱动锁住 Python 这一侧：参数怎么传、没命中怎么说、出错怎么回。
"""
from __future__ import annotations

from typing import Any

from tools.base import ToolContext
from tools.browser.grep_page import GREP_PAGE_TOOL, _grep_page


class FakeBrowser:
    def __init__(self, result: Any):
        self.result = result
        self.seen: dict[str, Any] | None = None

    def call_js(self, fn: str, arg: Any) -> Any:
        self.seen = arg
        return self.result


def test_hits_are_rendered_with_context():
    b = FakeBrowser({"hits": [{"match": "28 元", "context": "…超过 28 元后的流失原因…"}],
                     "total_chars": 1200})
    out = _grep_page(ToolContext(browser=b), {"query": "28 元"})
    assert "命中 1 处" in out and "1. 「28 元」" in out and "超过 28 元后的流失原因" in out


def test_no_hit_says_how_many_chars_were_searched():
    """没命中时要给出「搜了多少字」——不然分不清是页面没加载还是真没有。
    2026-09-17 那次裁判就是分不清，连试了二十几条 JS。"""
    out = _grep_page(ToolContext(browser=FakeBrowser({"hits": [], "total_chars": 0})), {"query": "62%"})
    assert "没找到「62%」" in out and "0 字" in out and "wait" in out


def test_defaults_are_passed_through():
    b = FakeBrowser({"hits": [], "total_chars": 1})
    _grep_page(ToolContext(browser=b), {"query": "x"})
    assert b.seen == {"q": "x", "ctx": 120, "max": 12, "regex": False}
    _grep_page(ToolContext(browser=b), {"query": "y", "regex": True, "context": 20, "max_hits": 3})
    assert b.seen == {"q": "y", "ctx": 20, "max": 3, "regex": True}


def test_bad_regex_is_reported_not_raised():
    out = _grep_page(ToolContext(browser=FakeBrowser({"error": "正则不合法: 缺右括号"})), {"query": "("})
    assert "正则不合法" in out


def test_empty_query_and_no_browser():
    assert "需要 query" in _grep_page(ToolContext(browser=FakeBrowser({})), {"query": "  "})
    assert "没有浏览器" in _grep_page(ToolContext(browser=None), {"query": "x"})


def test_tool_is_read_only_so_judge_can_use_it():
    """取证工具必须是只读的——动作型工具裁判拿不到，而且它不该改被测系统的状态。"""
    assert GREP_PAGE_TOOL.mutates is False and GREP_PAGE_TOOL.terminal is False
