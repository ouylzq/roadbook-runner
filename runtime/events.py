"""看板要的两条实时通道：画面帧（FrameStore）和跑批事件（EventStream）。

两个都是工作线程写、HTTP 线程读，所以都带锁。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

Event = dict[str, Any]


class FrameStore:
    """实时画面帧的中转站。

    只存"每次跑批的最新一帧"，不做队列——看板要的是"现在长什么样"，
    不是回放（回放有录屏）。
    """

    def __init__(self) -> None:
        self._frames: dict[str, bytes] = {}
        self._seq: dict[str, int] = {}
        self._lock = threading.Lock()

    def put(self, run_id: str, jpeg: bytes) -> None:
        with self._lock:
            self._frames[run_id] = jpeg
            self._seq[run_id] = self._seq.get(run_id, 0) + 1

    def get(self, run_id: str) -> tuple[bytes | None, int]:
        with self._lock:
            return self._frames.get(run_id), self._seq.get(run_id, 0)

    def drop(self, run_id: str) -> None:
        with self._lock:
            self._frames.pop(run_id, None)
            self._seq.pop(run_id, None)

    def sink_for(self, run_id: str) -> Callable[[bytes], None]:
        """给浏览器驱动注入的回调：驱动只知道"有帧就调它"，不知道看板的存在。"""
        return lambda jpeg: self.put(run_id, jpeg)


class EventStream:
    """跑批事件缓冲。工作线程往里写，HTTP 线程按游标读，SSE 推给看板。"""

    def __init__(self) -> None:
        self._buf: dict[str, list[Event]] = {}
        self._lock = threading.Lock()

    def open(self, run_id: str) -> None:
        with self._lock:
            self._buf[run_id] = []

    def emit(self, run_id: str, kind: str, payload: Any) -> None:
        with self._lock:
            self._buf.setdefault(run_id, []).append({"kind": kind, "payload": payload})

    def since(self, run_id: str, index: int) -> list[Event]:
        with self._lock:
            return self._buf.get(run_id, [])[index:]

    def close(self, run_id: str) -> None:
        with self._lock:
            self._buf.pop(run_id, None)
