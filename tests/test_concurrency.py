"""并发闸门：同时最多几个跑批在跑，超过的排队等空位，排队期间也要能响应「停止」。"""
from __future__ import annotations

import threading
import time

from runtime.concurrency import RunGate


def test_limit_zero_means_unlimited():
    gate = RunGate(0)
    for i in range(50):
        assert gate.acquire(f"r{i}") is True
    assert gate.running_count() == 50
    assert gate.is_full() is False    # 不限的情况下永远不算满


def test_extra_acquire_blocks_until_a_slot_frees():
    gate = RunGate(1)
    assert gate.acquire("first") is True
    assert gate.is_full() is True

    started = threading.Event()
    acquired = threading.Event()

    def waiter():
        started.set()
        gate.acquire("second")
        acquired.set()

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    started.wait(1)
    time.sleep(0.3)
    assert not acquired.is_set()      # 位子还占着，第二个应该还在等

    gate.release("first")
    t.join(timeout=2)
    assert acquired.is_set()          # 释放之后排队的那个拿到了位子
    assert gate.running_count() == 1


def test_cancel_while_queued_gives_up_without_a_slot():
    """排队期间被叫停——不用真的抢到位子、跑一次，才走停止流程。"""
    gate = RunGate(1)
    assert gate.acquire("holder") is True

    cancel = threading.Event()
    result: list[bool] = []

    def waiter():
        result.append(gate.acquire("waiting", cancel))

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    time.sleep(0.3)
    cancel.set()
    t.join(timeout=2)

    assert result == [False]          # 放弃了这个位置
    assert gate.running_count() == 1  # 只有原来那个占着，排队的那个没有真的进来
    gate.release("holder")
    assert gate.running_count() == 0


def test_release_of_unknown_run_id_is_a_no_op():
    gate = RunGate(2)
    gate.release("never-acquired")    # 不该抛，也不该把信号量放过头
    assert gate.acquire("a") is True
    assert gate.acquire("b") is True
