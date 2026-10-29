"""垃圾回收点：有副作用场景的清理任务台账与定时执行。

runner 永远不执行清理——提前清=毁掉裁判要验的证据（2026-10-26 生产 BLOCK 的教训）。
verdict 落地后，场景 intent「清理」小节里声明的机器块被登记成任务，
由调度线程到期执行。全程纯代码，不调 LLM。

任务状态机：
    pending ──执行──▶ done（回读确认） / retry（冷却期等，延后再来） / failed（进看板队列）
    retry ──到期──▶ 再执行；超过最大次数 ▶ failed
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .config import Settings
from .store import Store

COOLDOWN_EXIT = 75      # 清理脚本约定：暂时清不了（冷却期/资源忙），稍后重试
MAX_ATTEMPTS = 5
RETRY_DELAY_MIN = 15
BLOCK_RE = re.compile(r"^```yaml\s+cleanup\s*\n(.*?)^```", re.S | re.M)


def read_facts(run_dir: Path) -> dict[str, Any]:
    """读本次跑批的 context.yaml 实值（给清理参数解析用）。"""
    try:
        data = yaml.safe_load((Path(run_dir) / "context.yaml").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except OSError:
        return {}


def parse_cleanup_block(intent_text: str) -> list[dict[str, Any]]:
    """解析 intent「清理」小节里的 ```yaml cleanup 机器块。没有或解析失败都返回空——
    解析不了不能猜，说明文字部分本来就够人读。"""
    if not intent_text:
        return []
    m = BLOCK_RE.search(intent_text)
    if not m:
        return []
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and e.get("run")]


def resolve_args(arg_refs: list[Any], facts: dict[str, Any]) -> tuple[list[str], list[str]]:
    """把 [context.project_id, ...] 解析成本次跑批的实值。返回 (实值列表, 缺失键列表)。"""
    values, missing = [], []
    for ref in arg_refs:
        key = str(ref).removeprefix("context.").strip()
        v = facts.get(key)
        if v is None or str(v).strip() == "":
            missing.append(key)
        else:
            values.append(str(v).strip())   # 判空和传参同一套口径：脚本不该收到带空格的 id
    return values, missing


class CleanupManager:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.scripts_dir = settings.skills_dir / "roadbook" / "scripts"

    # ---- 登记（verdict 落地后，由跑批收尾钩子调用） -----------------------------
    def register_from_run(self, run_id: str, scenario_name: str, intent_text: str,
                          facts: dict[str, Any], verdict: str = "") -> list[dict[str, Any]]:
        entries = parse_cleanup_block(intent_text)
        if not entries:
            return []
        # 结论非 PASS 就进保持窗口：判据写错、裁判卡死都还有「只重跑裁判」的补救机会，
        # 证据先别清。晚几个小时回收的代价远小于重扣一遍 runner 的积分。
        hold_until = ""
        next_base = datetime.now()
        if str(verdict).strip() not in ("", "PASS", "EXPLORED"):
            hold_until = (next_base + timedelta(hours=self.settings.cleanup_hold_hours)
                          ).isoformat(timespec="seconds")
        out = []
        for i, e in enumerate(entries):
            args, missing = resolve_args(e.get("args") or [], facts)
            task_id = f"{run_id}#{i}"
            script = str(e.get("run") or "")
            # 深度防御：同一 script+args 只保留一条活着（非 failed）的任务。
            # 正常情况下重判钩子只调整 root 名下的任务、不会走到这里，但两个 run
            # 产出同一个 project_id 时能避免把不幂等的清理脚本跑两遍。
            alive = [t for t in self.store.all_cleanup_tasks(500)
                     if t["script"] == script and t["args_json"] == json.dumps(args, ensure_ascii=False)
                     and t["state"] != "failed"]
            if alive:
                out.append({"task_id": alive[0]["id"], "script": script,
                            "state": alive[0]["state"], "missing": [], "dedup": True})
                continue
            next_at = max(next_base + timedelta(minutes=int(e.get("not_before") or 0)),
                          datetime.fromisoformat(hold_until) if hold_until else next_base
                          ).isoformat(timespec="seconds")
            state, note = ("pending", "") if not missing else ("failed", f"缺事实键: {'、'.join(missing)}")
            self.store.upsert_cleanup_task(
                task_id=task_id, run_id=run_id, scenario=scenario_name,
                script=script, args=args, state=state,
                attempts=0, last_error=note, next_at=next_at, hold_until=hold_until)
            out.append({"task_id": task_id, "script": script, "state": state, "missing": missing})
        return out

    # ---- 与仅重判（rejudge）的联动 ----------------------------------------------
    def hold(self, run_id: str, hours: float | None = None) -> int:
        """续租：重判一开跑就把 root 名下的任务推远，清理不会在重判中途把证据掀了。"""
        h = self.settings.cleanup_hold_hours if hours is None else hours
        until = (datetime.now() + timedelta(hours=h)).isoformat(timespec="seconds")
        return self.store.hold_cleanup(run_id, until)

    def after_rejudge(self, root_run_id: str, verdict: str, *, scenario: str,
                      intent_text: str, facts: dict[str, Any]) -> list[dict[str, Any]]:
        """重判出结论后调整 root 名下的清理任务。刻意**不**新登记（任务 id 固定 root#i）：
        清理脚本不幂等，跑两次会把「刚 terminate 就 delete」做出来。"""
        tasks = self.store.cleanup_tasks_for_run(root_run_id)
        if not tasks:
            # 老跑批没登记过（比如 hold 机制上线前跑的）：按 root 补登记一次
            return self.register_from_run(root_run_id, scenario, intent_text, facts, verdict)
        if str(verdict).strip() == "PASS":
            n = self.store.release_cleanup(root_run_id, datetime.now().isoformat(timespec="seconds"))
            return [{"released": n}]
        self.hold(root_run_id)
        return [{"held": True}]

    def evidence_state(self, root_run_id: str) -> str:
        """重判 preflight 用：证据还在（intact）/ 清理中（cleaning）/ 已被清掉（cleaned）。"""
        states = {t["state"] for t in self.store.cleanup_tasks_for_run(root_run_id)}
        if "done" in states:
            return "cleaned"
        if "running" in states:
            return "cleaning"
        return "intact"

    # ---- 执行与调度 -------------------------------------------------------------
    def run_due(self, limit: int = 10) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        tasks = self.store.due_cleanup_tasks(now, limit=limit)
        ran = 0
        for task in tasks:
            # 原子占位：抢不到说明刚好被续租/释放改了状态，这一轮跳过
            if not self.store.claim_cleanup_task(task["id"], now):
                continue
            self._execute(task)
            ran += 1
        return ran

    def _execute(self, task: dict[str, Any]) -> None:
        # scripts_dir 本身就是 <skills>/roadbook/scripts，任务里的 script 是相对它的路径。
        # 2026-10-28 发现的老写法又拼了一次 roadbook/scripts，任何任务真执行都会 127。
        script = self.scripts_dir / task["script"]
        args = json.loads(task.get("args_json") or "[]")
        attempts = int(task.get("attempts") or 0) + 1
        try:
            proc = subprocess.run(["bash", str(script), *args], capture_output=True,
                                  text=True, timeout=600, cwd=str(self.scripts_dir.parent))
        except (OSError, subprocess.TimeoutExpired) as e:
            self._bump(task, "failed", attempts, f"{type(e).__name__}: {e}")
            return
        detail = (proc.stdout or proc.stderr or "").strip()[-300:]
        if proc.returncode == COOLDOWN_EXIT and attempts < MAX_ATTEMPTS:
            retry_at = (datetime.now() + timedelta(minutes=RETRY_DELAY_MIN)).isoformat(timespec="seconds")
            self.store.set_cleanup_state(task["id"], "retry", attempts, detail, retry_at)
            return
        if proc.returncode == 0:
            self.store.set_cleanup_state(task["id"], "done", attempts, detail[-300:], "")
            return
        self._bump(task, "failed", attempts, detail)

    def _bump(self, task: dict[str, Any], state: str, attempts: int, detail: str) -> None:
        self.store.set_cleanup_state(task["id"], state, attempts, detail[:300], "")

    def all_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.all_cleanup_tasks(limit)

    def requeue(self, task_id: str) -> None:
        """看板「一键重试/立即清理」：回 pending，下次调度立即执行，保持窗口一并解除。"""
        next_at = datetime.now().isoformat(timespec="seconds")
        self.store.set_cleanup_state(task_id, "pending", 0, "", next_at)
        task = next((t for t in self.store.all_cleanup_tasks(500) if t["id"] == task_id), None)
        if task:
            self.store.hold_cleanup(task["run_id"], "")   # hold_until 清空（next_at MAX 不早于空串）
