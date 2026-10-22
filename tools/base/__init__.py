"""工具层的基座：不是工具，是所有工具共用的地基。

`registry.py` 定义「一个工具长什么样」（Tool / ToolContext）和「怎么按名字找到它」
（ToolRegistry）；`args.py` 负责把模型给的原始参数解析成 dict，顺带修模型偶尔漏出的
XML 语法。真正的工具在上一层目录里，一个文件一个。
"""
from .args import ParsedToolArgs, parse_tool_args, parse_tool_args_result, repair_leaked_args
from .registry import Handler, JsonSchema, Tool, ToolContext, ToolRegistry

__all__ = [
    "Handler",
    "JsonSchema",
    "ParsedToolArgs",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "parse_tool_args",
    "parse_tool_args_result",
    "repair_leaked_args",
]
