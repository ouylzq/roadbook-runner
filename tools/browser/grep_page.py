"""工具 grep_page：在页面可见文字里搜一段话，返回命中处的上下文。只读。

裁判的活就是「在页面文字里找证据」，但在这个工具出现之前它没有对应的工具：
`find` 只搜 `[data-qa-ref]`（**只认可交互元素**，而证据几乎都在 AI 生成的说明文字里），
`observe` 给的是可访问性树、长页面会截断。于是它只能现写 JS 一遍遍试——
2026-09-17 那次裁判 31 步里 25 步是 `eval_js`，轮数耗尽、产出为零。

搜的是**文本节点**，而且跳过 script/style/noscript/template：
那次失败里模型试过 `body.textContent` 和 `innerHTML`，命中的全是 Next.js 的内联脚本
（`self.__next_f.push(...)`）和 CSS 变量，白翻好几轮。
也不用 `innerText`——它只给渲染出来的部分，折叠区里的证据会整段读不到。
"""
from __future__ import annotations

from typing import Any

from tools.base import Tool, ToolContext
from tools.browser.base import NO_BROWSER

MAX_HITS = 12
CONTEXT_CHARS = 120

GREP_JS = """(args) => {
  const {q, ctx, max, regex} = args;
  const SKIP = {SCRIPT:1, STYLE:1, NOSCRIPT:1, TEMPLATE:1};
  const parts = [];
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode(n){
      return SKIP[n.parentNode && n.parentNode.nodeName]
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
    }
  });
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    const t = n.nodeValue.replace(/\\s+/g, ' ').trim();
    if (t) parts.push(t);
  }
  const text = parts.join(' ');
  let re;
  try { re = regex ? new RegExp(q, 'gi')
                   : new RegExp(q.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&'), 'gi'); }
  catch (e) { return {error: '正则不合法: ' + e.message}; }
  const hits = [];
  let m;
  while ((m = re.exec(text)) !== null && hits.length < max) {
    const a = Math.max(0, m.index - ctx), b = Math.min(text.length, m.index + m[0].length + ctx);
    hits.push({match: m[0], context: (a ? '…' : '') + text.slice(a, b) + (b < text.length ? '…' : '')});
    if (m.index === re.lastIndex) re.lastIndex++;
  }
  return {hits, total_chars: text.length};
}"""


def _grep_page(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.browser is None:
        return NO_BROWSER
    query = str(args.get("query") or "").strip()
    if not query:
        return "grep_page 需要 query。"
    result = ctx.browser.call_js(GREP_JS, {
        "q": query,
        "ctx": int(args.get("context") or CONTEXT_CHARS),
        "max": int(args.get("max_hits") or MAX_HITS),
        "regex": bool(args.get("regex")),
    })
    if not isinstance(result, dict):
        return str(result)
    if result.get("error"):
        return str(result["error"])

    hits = result.get("hits") or []
    if not hits:
        return (f"没找到「{query}」。页面可见文字共 {result.get('total_chars', 0)} 字——"
                "确认页面已经加载完（必要时先 wait 再搜），或者换个更短的关键词试。")
    lines = [f"命中 {len(hits)} 处（页面可见文字共 {result.get('total_chars', 0)} 字）："]
    lines += [f"{i}. 「{h['match']}」\n   {h['context']}" for i, h in enumerate(hits, 1)]
    return "\n".join(lines)


GREP_PAGE_TOOL = Tool(
    name="grep_page",
    description=(
        "在页面可见文字里搜关键词，返回每处命中的上下文原文（只读）。"
        "取证首选：要确认页面上有没有某段文案、某个数字、某个状态词，用它，别自己写 JS 翻 DOM。"
        "已自动跳过 script/style，不会搜到内联脚本里的噪音；折叠区里的文字也搜得到。"
    ),
    parameters={
        "query": {"type": "string", "description": "要找的文字；默认按普通文本匹配，不用转义"},
        "regex": {"type": "boolean", "description": "把 query 当正则用（如 `62%|15-25`），默认否"},
        "context": {"type": "integer", "description": f"每处命中前后各带多少字，默认 {CONTEXT_CHARS}"},
        "max_hits": {"type": "integer", "description": f"最多返回几处，默认 {MAX_HITS}"},
    },
    required=("query",),
    handler=_grep_page,
)
