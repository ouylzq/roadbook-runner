"""非关键路径资产写入：runner 提交短事件，单线程后台顺序落盘。"""
from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from tools.upsert_playbook_step import apply_playbook_step

from .skills import Skill


class AssetWriter:
    def __init__(self, skill: Skill, scenario: str):
        self.skill = skill
        self.scenario = scenario
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qa-asset-writer")
        self._futures: list[Future[str]] = []
        self._lock = threading.Lock()

    def enqueue_playbook_step(self, args: dict[str, Any]) -> str:
        payload = dict(args)
        future = self._executor.submit(apply_playbook_step, self.skill.root, self.scenario, payload)
        with self._lock:
            self._futures.append(future)
        return f"已排队沉淀 playbook 第 {payload['step_number']} 步；runner 继续业务流程，不等待写盘"

    def flush(self) -> tuple[list[str], list[str]]:
        with self._lock:
            futures, self._futures = self._futures, []
        ok: list[str] = []
        errors: list[str] = []
        for future in futures:
            try:
                ok.append(future.result())
            except Exception as error:
                errors.append(f"{type(error).__name__}: {error}")
        self._executor.shutdown(wait=True)
        return ok, errors
