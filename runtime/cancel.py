"""跑批的「停止」信号：一个 run_id 对应一个 threading.Event。

工作线程（AgentLoop）在自然边界上自己查这个 Event，查到了就自己收口——
不是从外面把线程/浏览器进程杀掉。这样 Browser 的 `__exit__` 照常跑，
录屏能正常落盘，Playwright/ffmpeg 子进程也是正常退出，不会留孤儿进程。
"""
from __future__ import annotations

import threading


class CancelledError(Exception):
    """AgentLoop 查到停止信号时抛出，一路不捕获地穿出到 RunSession，按「停止」收尾。"""


class CancelRegistry:
    def __init__(self) -> None:
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def create(self, run_id: str) -> threading.Event:
        event = threading.Event()
        with self._lock:
            self._events[run_id] = event
        return event

    def get(self, run_id: str) -> threading.Event | None:
        """只读地看一眼这个跑批的停止信号，不改它的生命周期。给排队中的等待逻辑用——
        队列里等位置的时候也要能响应「停止」，不用等真正抢到位子、开了浏览器才收口。"""
        with self._lock:
            return self._events.get(run_id)

    def cancel(self, run_id: str) -> bool:
        """标记停止。跑批不存在或已经结束（事件已被 drop）就返回 False，接口据此回 404。"""
        with self._lock:
            event = self._events.get(run_id)
        if event is None:
            return False
        event.set()
        return True

    def drop(self, run_id: str) -> None:
        with self._lock:
            self._events.pop(run_id, None)
