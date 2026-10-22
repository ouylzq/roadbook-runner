"""工具调用参数的卫生处理。

某些网关/模型会把 XML 风格的工具参数语法泄进字符串参数里。
实测 2026-09-16：bedrock/claude 经 LiteLLM，notes 末尾出现
    ...换算退出码。</notes>\\n<parameter name="facts">{"task_titles": [...]}
结果 facts 整个被吞进 notes，下游拿不到结构化产物。这里把它抽回顶层。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_LEAK = re.compile(r'</\w+>\s*<parameter name="(\w+)">(.*)', re.S)
_TRAILING_CLOSE = re.compile(r"</\w+>\s*$")


def repair_leaked_args(args: dict[str, Any]) -> dict[str, Any]:
    """把泄漏进字符串参数的其它参数抽回顶层。已有的顶层键不会被覆盖。"""
    out = dict(args)
    for key, value in list(out.items()):
        if not isinstance(value, str):
            continue
        match = _LEAK.search(value)
        if not match:
            continue
        out[key] = value[: match.start()].rstrip()
        leaked_key = match.group(1)
        leaked_raw = _TRAILING_CLOSE.sub("", match.group(2).strip()).strip()
        try:
            leaked_value: Any = json.loads(leaked_raw)
        except json.JSONDecodeError:
            leaked_value = leaked_raw
        out.setdefault(leaked_key, leaked_value)
    return out


@dataclass(frozen=True)
class ParsedToolArgs:
    args: dict[str, Any]
    error: str | None = None


def parse_tool_args_result(raw: str | None) -> ParsedToolArgs:
    """解析工具参数；坏 JSON 与合法空对象必须区分，handler 执行前要能拦住前者。"""
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as error:
        return ParsedToolArgs({}, f"arguments 不是完整 JSON：{error.msg}（第 {error.pos} 字符）")
    if not isinstance(parsed, dict):
        return ParsedToolArgs({}, f"arguments 必须是 object，实际是 {type(parsed).__name__}")
    return ParsedToolArgs(repair_leaked_args(parsed))


def parse_tool_args(raw: str | None) -> dict[str, Any]:
    """兼容旧调用方；循环层应使用 parse_tool_args_result 保留错误信息。"""
    return parse_tool_args_result(raw).args
