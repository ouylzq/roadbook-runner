"""浏览器工具的基座：不是工具，是 9 个浏览器工具共用的地基。

`driver.py` 是 Browser（Playwright 封装，唯一碰浏览器的地方），`observation.py` 是它
产出的观察结果，`snapshot.js` 是注入页面取结构的脚本。换一个浏览器运行时只动这里。
"""
from .driver import NO_BROWSER, Browser
from .observation import Observation

__all__ = ["NO_BROWSER", "Browser", "Observation"]
