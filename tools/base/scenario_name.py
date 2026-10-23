"""场景名的合法性判定（写工具共用）。

场景支持一层项目分组（`scenarios/<项目>/<场景>/`，如 `team/login`），
所以合法名字 = 相对 scenarios/ 的向下路径；仍然要拦的是路径逃逸：`..`、绝对路径、
反斜杠、空片段。最终兜底在写文件处的 `_inside`（resolve 后必须落在 scenarios/ 内）。
"""
from __future__ import annotations


def legal_scenario_name(name: str) -> bool:
    if not name or name.startswith((".", "/")) or "\\" in name:
        return False
    return all(part and part != "." and set(part) != {"."} for part in name.split("/"))
