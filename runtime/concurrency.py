"""跑批的并发闸门：同时最多几个在跑，超过的排队等空位。

每个跑批是一个真实 Chromium 进程 + 一整套 LLM 调用，不是轻量任务。没有上限的话，
点了「全部跑一遍」或者手快多点几张卡片，机器会被同时起的几十个浏览器进程压垮；
共用固定账号的场景（login 那类）还会互相抢发码配额。

跟 CancelRegistry 一个风格：一把锁 + 一个 threading 原语，工作线程自己在安全点等，
不是从外面控制谁先跑——`acquire()` 在后台线程里阻塞等待完全正常，不影响接口的响应速度。
"""
from __future__ import annotations

import threading


class RunGate:
    def __init__(self, limit: int) -> None:
        # 0 = 不限：用一个「拿了不还」的假信号量，acquire 永远立即成功。
        self.limit = limit
        self._sem = threading.Semaphore(limit) if limit > 0 else None
        self._lock = threading.Lock()
        self._running: set[str] = set()

    POLL_SECONDS = 0.25   # 排队时多久看一眼「有没有人叫停」，不用太密

    def acquire(self, run_id: str, cancel: threading.Event | None = None) -> bool:
        """占一个位。位子不够就阻塞在这里——调用方应该在后台线程里调，不能在请求线程里调。

        给了 `cancel` 就一边排队一边听它：排队期间被叫停，直接放弃这个位置、返回 False，
        不用真的抢到位子、开一次浏览器，才走停止流程。没给（不限并发时用不上）就死等到底。
        """
        if self._sem is not None:
            if cancel is None:
                self._sem.acquire()
            else:
                while not self._sem.acquire(timeout=self.POLL_SECONDS):
                    if cancel.is_set():
                        return False
        with self._lock:
            self._running.add(run_id)
        return True

    def release(self, run_id: str) -> None:
        """放开一个位。对一个没真的占到位的 run_id 调用（比如排队时被取消、从没 acquire
        成功过）必须是空操作——`Semaphore.release()` 不检查配对，瞎放会把计数顶到超过
        `limit`，之后并发数就名不副实了。"""
        with self._lock:
            held = run_id in self._running
            self._running.discard(run_id)
        if held and self._sem is not None:
            self._sem.release()

    def is_full(self) -> bool:
        """开跑前问一句「现在要不要排队」，只用来决定第一条 phase 消息怎么写，不参与真正的抢位。"""
        with self._lock:
            return self.limit > 0 and len(self._running) >= self.limit

    def running_count(self) -> int:
        with self._lock:
            return len(self._running)
