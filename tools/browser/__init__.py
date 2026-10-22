"""浏览器工具：一个工具一个文件，文件名就是工具名。

动作型（mutates=True）的工具执行完会把最新页面观察拼进返回里——Observe→Act→Observe
是结构性保证，模型不可能凭记忆操作。驱动实现统一在 driver.py。
"""
from .base import NO_BROWSER, Browser, Observation
from .click import CLICK_TOOL
from .click_xy import CLICK_XY_TOOL
from .eval_js import EVAL_JS_TOOL
from .find import FIND_TOOL
from .grep_page import GREP_PAGE_TOOL
from .navigate import NAVIGATE_TOOL
from .observe import OBSERVE_TOOL
from .scroll import SCROLL_TOOL
from .type_text import TYPE_TEXT_TOOL
from .wait import WAIT_TOOL

BROWSER_TOOLS = [
    OBSERVE_TOOL,      # 只读：重新观察页面
    NAVIGATE_TOOL,     # 动作：打开网址
    CLICK_TOOL,        # 动作：按引用点击
    TYPE_TEXT_TOOL,    # 动作：打字
    FIND_TOOL,         # 只读：按文案搜引用
    GREP_PAGE_TOOL,    # 只读：按文字搜可见正文，取证首选
    EVAL_JS_TOOL,      # 只读：页面里求值取证
    WAIT_TOOL,         # 动作：等待
    SCROLL_TOOL,       # 动作：滚动
    CLICK_XY_TOOL,     # 动作：按坐标点击（退守）
]

__all__ = ["BROWSER_TOOLS", "NO_BROWSER", "Browser", "Observation"]
