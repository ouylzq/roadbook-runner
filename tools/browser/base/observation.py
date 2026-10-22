"""一次「观察」的结果，以及怎么渲染成喂给模型的上下文。

它是浏览器驱动的产出类型，所以住在驱动旁边，而不是 runtime 里——
否则 tools 就得反向 import runtime，形成循环依赖。

渲染规则体现取证原则：页面结构和文本是主通道，截图只留档。
所以 render() 里只有树和文本，截图路径只记在字段上，不进模型上下文。
"""
from __future__ import annotations

from dataclasses import dataclass, field

TREE_MAX_CHARS = 6000
TEXT_MAX_CHARS = 2500
CONSOLE_TAIL = 5


@dataclass
class Observation:
    url: str
    title: str
    tree: str
    text: str
    ref_count: int
    console_errors: list[str] = field(default_factory=list)
    screenshot_path: str | None = None

    def render(self, max_tree: int = TREE_MAX_CHARS, max_text: int = TEXT_MAX_CHARS) -> str:
        tree = _clip(self.tree, max_tree, "\n…（结构已截断，用 find 精确搜）")
        text = _clip(self.text, max_text, "\n…（文本已截断）")
        parts = [
            f"URL: {self.url}",
            f"标题: {self.title}",
            "",
            "页面结构（可交互元素带 {ref_N}）:",
            tree or "（空）",
            "",
            "页面可见文本:",
            text or "（空）",
        ]
        if self.console_errors:
            parts += ["", "控制台错误:", "\n".join(self.console_errors[-CONSOLE_TAIL:])]
        return "\n".join(parts)


def _clip(s: str, limit: int, marker: str) -> str:
    return s if len(s) <= limit else s[:limit] + marker
