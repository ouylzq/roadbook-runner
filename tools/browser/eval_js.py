"""工具 eval_js：在页面里求值取证。

`read_only` 的调用方（裁判）会被挡掉 JS 里的点击和提交。白名单不给裁判 click 的那阵子，
它就是靠 `document.querySelector(...).click()` 绕过去的——2026-09-21 那次 choice-question
点开了两个面板，判了 PASS，而跑批记录里只留下一句求值，看不出它动过页面。现在裁判有 click，
点击就该走 click。这道拦截挡的是顺手，不是对抗：真要拼字符串取方法名，拦不住。
"""
from __future__ import annotations

import re
from typing import Any

from tools.base import Tool, ToolContext
from tools.browser.base import NO_BROWSER

SIDE_EFFECT = re.compile(r"\.\s*(click|submit|requestSubmit)\s*\(")
REFUSED = (
    "拒绝：不要用 eval_js 点击或提交。要点就调 click——它会在跑批记录里留下你点了哪个元素，"
    "别人才复核得了。提交 / 保存这类写操作一律不许做，够不着就判 blocked。"
)


def _eval_js(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.browser is None:
        return NO_BROWSER
    expr = args["expr"]
    if ctx.read_only and SIDE_EFFECT.search(expr):
        return REFUSED
    return ctx.browser.eval_js(expr)


EVAL_JS_TOOL = Tool(
    name="eval_js",
    description=(
        "在页面里求值取回结果（只读）。取表单 value、localStorage 这类文本层读不到的证据。传表达式。"
    ),
    parameters={"expr": {"type": "string"}},
    required=("expr",),
    handler=_eval_js,
)
