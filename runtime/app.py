"""组合根：把配置、加载器、存储、沉淀钩子装配成一个门面。

CLI 和看板后端都只跟这个类打交道，不各自拼装一套加载器、不各自扫 storage 目录。
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from tools import REGISTRY

from .atoms import AtomMemory
from .authoring import Authoring, AuthorResult
from .cleanup import CleanupManager, read_facts
from .config import Settings, get_settings
from .domain import RunStatus, Scenario
from .events import FrameStore
from .expect import load_expect, required_context_keys, save_expect
from .intent import parse_intent
from .knowledge import KnowledgeStore
from .notify import notify_finished
from .report import ReportWriter
from .session import (
    EventListener,
    FinishedHook,
    RunSession,
    new_run_id,
    resume_run_id,
)
from .skills import Skill, SkillLoader
from .store import Store
from .traces import Run


class RejudgeRefusedError(Exception):
    """仅重判的 preflight 拒绝。status 是建议的 HTTP 状态码，message 是给人看的理由。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class QaAgent:
    def __init__(self, settings: Settings | None = None, *, frames: FrameStore | None = None):
        self.settings = settings or get_settings()
        self.frames = frames
        self.skills = SkillLoader(self.settings)
        self.knowledge = KnowledgeStore(self.settings.knowledge_dir)
        s = self.settings
        # 原子记忆层。禁用词直接用运行时自己的工具名——资产层不许出现它们，运行时最清楚自己叫什么
        self.atoms = AtomMemory(s.knowledge_dir, enabled=s.atom_memory,
                                confirms_append=s.atom_confirms_append,
                                confirms_canonical=s.atom_confirms_canonical,
                                refutes_drop=s.atom_refutes_drop, refutes_demote=s.atom_refutes_demote,
                                ttl_days=s.atom_pending_ttl_days, banned_words=REGISTRY.names())
        self.reports = ReportWriter(self.settings.reports_dir)
        self.store = Store(self.settings.database_path)
        self.cleanup = CleanupManager(self.settings, self.store)
        self._authoring = Authoring(self.settings)

    # ---- 资产 -----------------------------------------------------------------
    def skill(self, name: str | None = None) -> Skill:
        return self.skills.load(name or self.settings.default_skill)

    def scenarios(self, skill: Skill) -> list[Scenario]:
        return skill.scenarios()

    # ---- 跑批 -----------------------------------------------------------------
    @staticmethod
    def _archived_intent(run_dir: Path, skill: Skill, scenario_name: str) -> str:
        """intent 优先读这次跑批的存档（task/intent.md）——登记清理该以「当时声明」为准，
        跑完后有人改了场景目录的 intent 也不影响这次该清什么。"""
        archived = run_dir / "task" / "intent.md"
        if archived.exists():
            return archived.read_text(encoding="utf-8")
        return skill.scenario(scenario_name).read_or_empty("intent.md")

    def _hooks(self, skill: Skill) -> Sequence[FinishedHook]:
        # 收尾四件事：记事实/流水、写报告、给原子候选数票、推飞书。数票要知道写到哪个
        # 技能的 atoms/，所以 hook 按技能现做一个；飞书通知与技能无关，全局一份。
        return (self.knowledge.absorb, self.reports.write,
                lambda _record, _run_dir: self.atoms.settle(skill),
                lambda record, _run_dir: notify_finished(self.settings, record),
                lambda record, run_dir: self.cleanup.register_from_run(
                    record.run_id, record.scenario,
                    self._archived_intent(run_dir, skill, record.scenario),
                    read_facts(run_dir), verdict=str(record.verdict)))

    def run(self, skill: Skill, scenario_name: str, *, run_id: str | None = None,
            headless: bool = True, emit: EventListener | None = None,
            cancel: threading.Event | None = None, env: str | None = None) -> Run:
        scenario = skill.scenario(scenario_name)
        session = RunSession(skill, scenario, run_id or new_run_id(scenario.name), self.settings,
                             headless=headless, emit=emit, frames=self.frames, store=self.store,
                             on_finished=self._hooks(skill), cancel=cancel,
                             briefing=self.knowledge.briefing,
                             env=self.pick_env(scenario, env), memory=self.atoms)
        return session.execute()

    @staticmethod
    def pick_env(scenario: Scenario, asked: str | None) -> str:
        """人选了就用人选的，但只认场景自己声明过的那几个——`envs: [dev]` 的场景
        不该因为一个手打的 query 参数就去打 prod。没声明 envs 的场景一律留空。"""
        wanted = (asked or "").strip()
        if wanted and wanted in scenario.envs:
            return wanted
        return scenario.default_env

    def resume(self, skill: Skill, previous_run_id: str, answers: list[str], *,
               run_id: str | None = None, headless: bool = True,
               emit: EventListener | None = None,
               cancel: threading.Event | None = None) -> Run:
        previous_dir = self.settings.storage_dir / previous_run_id
        previous = Run.load(previous_dir)
        if previous is None:
            raise FileNotFoundError(f"没有这次跑批: {previous_run_id}")
        transcript = RunSession.load_transcript(previous_dir)
        scenario = skill.scenario(previous.scenario)
        session = RunSession(skill, scenario, run_id or resume_run_id(previous_run_id), self.settings,
                             headless=headless, emit=emit, frames=self.frames, store=self.store,
                             on_finished=self._hooks(skill), cancel=cancel,
                             briefing=self.knowledge.briefing,
                             # 接着跑要留在原来那个环境，别掉回默认
                             env=self.pick_env(scenario, previous.env), memory=self.atoms)
        return session.resume(transcript, answers)

    # ---- 仅重判 ---------------------------------------------------------------
    def rejudge_preflight(self, rid: str) -> tuple[Run, Path, Skill, Scenario, list[str]]:
        """仅重判的准入检查。通过返回 (原始记录, 原始目录, 技能, 场景, 软警告)，
        不通过抛 RejudgeRefusedError——HTTP 层直接翻译成状态码。"""
        src = self.record(rid)
        if src is None:
            raise RejudgeRefusedError(404, f"没有这次跑批: {rid}")
        # 对重判记录再点一次重判，等价于对 root 重判——输入源一律回到 root 目录
        root = src.rejudge_of or src.run_id
        root_rec = self.record(root)
        if root_rec is None:
            raise RejudgeRefusedError(404, f"原始跑批目录不在了: {root}")
        if root_rec.external:
            raise RejudgeRefusedError(400, "外部迁移的记录只有文字产物，没有登录态和事实现场，不能只重判")
        if src.status is RunStatus.RUNNING:
            raise RejudgeRefusedError(409, "这条跑批还在跑，等它出结论再重判")
        if root_rec.runner_outcome in ("blocked", "needs_input"):
            raise RejudgeRefusedError(409, f"原跑批的 runner 没交棒（{root_rec.runner_outcome}），"
                                     "没有可复核的事实和登录态——只能整跑或回答问题续跑")
        try:
            skill = self.skill(root_rec.skill or None)
            scenario = skill.scenario(root_rec.scenario)
        except FileNotFoundError as e:
            raise RejudgeRefusedError(404, f"场景已不存在: {e}") from e
        if not scenario.has_expect:
            raise RejudgeRefusedError(409, "这个场景没有验收路线（expect.yaml），没有可重判的内容")
        root_dir = self.run_dir(root)
        facts = read_facts(root_dir)
        if not facts:
            raise RejudgeRefusedError(409, "原跑批没留下事实（context.yaml 缺失或为空），裁判没有可对照的现场")
        state_file = root_dir / "storage_state.json"
        if not state_file.exists():
            raise RejudgeRefusedError(409, "原跑批没留下登录态快照（storage_state.json），只能整跑")
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or not (state.get("cookies") or state.get("origins")):
                raise ValueError("结构不完整")
        except (ValueError, OSError) as e:
            raise RejudgeRefusedError(409, f"登录态快照损坏（{e}），裁判会直接撞登录墙——只能整跑") from e
        # 新判据引用了这次没留下的事实：重判时裁判拿不到，只能整跑
        missing = [k for k in required_context_keys(scenario) if not str(facts.get(k, "")).strip()]
        if missing:
            raise RejudgeRefusedError(409, "新判据引用了本次没留下的事实："
                                 + "、".join(missing) + "——只能整跑重新采集")
        evidence = self.cleanup.evidence_state(root)
        if evidence == "cleaned":
            raise RejudgeRefusedError(409, "证据已被清理（研究已终止/删除），页面取证无从谈起——只能整跑")
        if evidence == "cleaning":
            raise RejudgeRefusedError(409, "清理正在执行，等这一轮结束再看（正常情况下结论非 PASS 会保持 4 小时窗口）")
        warnings: list[str] = []
        age_h = (datetime.now().timestamp() - state_file.stat().st_mtime) / 3600
        if age_h > self.settings.cleanup_hold_hours:
            warnings.append(f"登录态快照已 {age_h:.0f} 小时旧（token 可能过期，裁判撞登录墙的话结论会照实记 blocked）")
        failed = [t for t in self.store.cleanup_tasks_for_run(root) if t["state"] == "failed"]
        if failed:
            warnings.append(f"这个研究之前有 {len(failed)} 个清理任务失败过，页面状态未知")
        return root_rec, root_dir, skill, scenario, warnings

    def _rejudge_hooks(self, skill: Skill, root: str) -> Sequence[FinishedHook]:
        """重判的收尾钩子：不挂 atoms.settle（没有 runner 就没有新动作可投票），
        也不挂 register_from_run（清理任务只调整 root 名下那批，不重复登记）。"""
        return (self.knowledge.absorb, self.reports.write,
                lambda record, _run_dir: notify_finished(self.settings, record),
                lambda record, run_dir: self.cleanup.after_rejudge(
                    root, str(record.verdict), scenario=record.scenario,
                    intent_text=self._archived_intent(run_dir, skill, record.scenario),
                    facts=read_facts(run_dir)))

    def rejudge(self, skill: Skill, rid: str, *, run_id: str, headless: bool = True,
                emit: EventListener | None = None,
                cancel: threading.Event | None = None) -> Run:
        """对一个已结束的跑批只重跑裁判。判据读当前版本（改判据就是为此），
        现场（登录态+事实）从 root 目录继承，环境也沿用 root 当时的。"""
        src, root_dir, sk, _scenario, _warnings = self.rejudge_preflight(rid)
        root = src.rejudge_of or src.run_id
        session = RunSession(skill, sk.scenario(src.scenario), run_id, self.settings,
                             headless=headless, emit=emit, frames=self.frames, store=self.store,
                             on_finished=self._rejudge_hooks(skill, root), cancel=cancel,
                             env=src.env, rejudge_of=(src, root_dir))
        return session.rejudge()

    # ---- 裁判质疑判据：人裁决 ---------------------------------------------------
    def decide_criteria(self, rid: str, action: str,
                        items: list[dict[str, Any]] | None = None) -> Run:
        """对一条挂着「判据质疑」的跑批做裁决。

        action="keep"：维持原判——恢复按原判据算出的 FAIL/BLOCK，质疑归档进备注。
        action="apply"：采纳修订（可带人改过的文本）——写回场景 expect.yaml，
        挂起记录归档为已采纳，随后的重判用新判据出真结论。判据是人的预期，
        裁判只有建议权，落不落盘由这里决定。
        """
        record = self.record(rid)
        if record is None:
            raise FileNotFoundError(f"没有这次跑批: {rid}")
        if not record.criteria_issues:
            raise ValueError("这条跑批没有待裁决的判据质疑")
        if action not in ("keep", "apply"):
            raise ValueError("action 只能是 keep 或 apply")

        if action == "keep":
            record.status = RunStatus(record.pending_status or "failed")
            record.verdict = record.pending_verdict or "FAIL"
            record.notes = (record.notes + "\n\n" + "裁判质疑过判据，人维持原判（"
                            + "；".join(f"{i.get('check_ref', '')}：{i.get('problem', '')}"
                                       for i in record.criteria_issues) + "）").strip()
            record.criteria_issues = []
            record.save(self.run_dir(rid))
            self.store.upsert_run(
                run_id=record.run_id, scenario=record.scenario, skill=record.skill,
                status=str(record.status), verdict=str(record.verdict),
                started_at=record.started_at, finished_at=record.finished_at)
            return record

        # apply：逐条把建议文本写进当前 expect.yaml。position + current 双重校验——
        # 判据在人挂起期间被人改过就不落，避免把修订写进错误的行。
        # 前端可以只发 {index, new_text}（index 指向挂起的 criteria_issues 顺序），
        # step/check/current 从存档的质疑里取。
        skill = self.skill(record.skill or None)
        scenario = skill.scenario(record.scenario)
        expect = load_expect(scenario)
        steps = expect.get("steps") or []
        edits = items if items else [{"new_text": i.get("proposed", "")} for i in record.criteria_issues]
        applied, skipped = [], []
        for pos, e in enumerate(edits):
            issue = (record.criteria_issues[pos] if pos < len(record.criteria_issues) else {})
            si = int(e["step"]) if e.get("step") is not None else int(issue.get("step", -1))
            ci = int(e["check"]) if e.get("check") is not None else int(issue.get("check", -1))
            new_text = str(e.get("new_text") or "").strip()
            if not (0 <= si < len(steps) and 0 <= ci < len(steps[si].get("check") or [])):
                skipped.append(f"第{si+1}.{ci+1}条：位置不存在")
                continue
            current = str(steps[si]["check"][ci])
            claimed = str(issue.get("current", ""))
            if claimed and claimed != current:
                skipped.append(f"第{si+1}.{ci+1}条：判据在挂起期间被改过，请手动核对")
                continue
            if not new_text:
                skipped.append(f"第{si+1}.{ci+1}条：修订文本为空")
                continue
            steps[si]["check"][ci] = new_text
            applied.append(f"第{si+1}.{ci+1}条")
        if not applied:
            raise ValueError("没有任何一条能落盘：" + "；".join(skipped))
        save_expect(scenario, expect.get("goal", ""), steps)

        record.notes = (record.notes + "\n\n" + "裁判质疑判据，人已采纳修订（"
                        + "；".join(applied) + "）"
                        + ("；部分未落盘：" + "；".join(skipped) if skipped else "")
                        + "。最终结论见随后的重判。").strip()
        record.criteria_issues = []
        record.status = RunStatus(record.pending_status or "failed")
        record.verdict = record.pending_verdict or "FAIL"
        record.save(self.run_dir(rid))
        self.store.upsert_run(
            run_id=record.run_id, scenario=record.scenario, skill=record.skill,
            status=str(record.status), verdict=str(record.verdict),
            started_at=record.started_at, finished_at=record.finished_at)
        return record

    def author(self, skill: Skill, ask: str, *, scenario: str | None = None,
               url: str | None = None, name: str | None = None,
               project: str | None = None,
               config_hints: dict[str, str] | None = None,
               on_progress: Callable[[dict], None] | None = None) -> AuthorResult:
        return self._authoring.author(skill, ask, scenario=scenario, url=url, name=name,
                                      project=project, config_hints=config_hints,
                                      on_progress=on_progress)

    # ---- 查历史 ---------------------------------------------------------------
    def run_dir(self, run_id: str) -> Path:
        return self.settings.storage_dir / run_id

    def record(self, run_id: str) -> Run | None:
        return Run.load(self.run_dir(run_id))

    def sweep_orphans(self) -> list[str]:
        """服务刚起来时，把还标着 running 的旧记录收掉。

        跑批是进程内的线程，服务一重启它们就不存在了——所以启动这一刻
        **没有任何跑批可能真的在跑**，凡是还写着 running 的都是上次没收尾留下的。
        2026-09-21 实测：kill -9 掉一个在跑的跑批，那条记录就永远停在 running，
        看板上的卡片从此一直显示"跑着"，而且实时画面怎么等都不会来。
        """
        orphans = []
        for run in self.records(limit=10_000):
            if run.status is not RunStatus.RUNNING:
                continue
            run.status = RunStatus.ERROR
            run.error = run.error or "服务重启前没有收尾（进程被杀或崩了），状态由启动清扫补齐"
            run.finished_at = run.finished_at or datetime.now().isoformat(timespec="seconds")
            run.save(self.run_dir(run.run_id))
            self.store.upsert_run(
                run_id=run.run_id, scenario=run.scenario, skill=run.skill,
                status=str(run.status), verdict=str(run.verdict),
                started_at=run.started_at, finished_at=run.finished_at,
            )
            orphans.append(run.run_id)
        return orphans

    def records(self, limit: int = 60) -> list[Run]:
        storage = self.settings.storage_dir
        if not storage.is_dir():
            return []
        out: list[Run] = []
        for d in sorted(storage.iterdir(), reverse=True):
            if len(out) >= limit:
                break
            if d.is_dir() and (r := Run.load(d)):
                out.append(r)
        return out

    def latest_by_scenario(self) -> dict[str, Run]:
        """一次扫描给出每个场景最近一次的记录——列表页要用，别每个场景扫一遍目录。"""
        latest: dict[str, Run] = {}
        for record in self.records(limit=10_000):
            latest.setdefault(record.scenario, record)
        return latest

    def report_text(self, run_id: str) -> str | None:
        path = self.reports.path_for(run_id)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def llm_calls(self, run_id: str) -> list[dict[str, Any]]:
        """展示在跑批详情里的调用日志：这次跑批每一次真正调模型的记录。"""
        return self.store.llm_calls_for_run(run_id)

    def scenario_runs(self, scenario_name: str, limit: int = 20) -> list[dict[str, Any]]:
        """每个场景自己的运行记录——按场景查历史，走索引不扫文件。"""
        return self.store.runs_for_scenario(scenario_name, limit)

    def task_of(self, run_id: str, scenario_name: str, skill_name: str | None = None) -> dict[str, Any]:
        """这次跑批被要求做什么：优先读跑批目录里的存档，没有再退回场景目录的当前版本。

        路书 agent 跑完会回写，所以「当前版本」未必是当时执行的那一份——退回时要标出来，
        别让人拿着新路书去对旧跑批的步骤。
        """
        snapshot = self.run_dir(run_id) / "task"
        files = {name: (snapshot / name) for name in ("intent.md", "playbook.md")}
        task: dict[str, Any]
        if any(p.exists() for p in files.values()):
            task = {"archived": True,
                    **{n.split(".")[0]: (p.read_text(encoding="utf-8") if p.exists() else "")
                       for n, p in files.items()}}
        else:
            try:
                scenario = self.skill(skill_name).scenario(scenario_name)
            except FileNotFoundError:
                return {"archived": False, "intent": "", "playbook": "", "intent_doc": None}
            task = {"archived": False,
                    "intent": scenario.read_or_empty("intent.md"),
                    "playbook": scenario.read_or_empty("playbook.md")}
        # 结构化的 intent 给看板画卡片用；存档和当前版本走同一个解析器
        task["intent_doc"] = parse_intent(task["intent"]) if task["intent"] else None
        return task

    def read_yaml(self, run_id: str, filename: str) -> Any:
        path = self.run_dir(run_id) / filename
        return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
