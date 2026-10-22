"""组合根：把配置、加载器、存储、沉淀钩子装配成一个门面。

CLI 和看板后端都只跟这个类打交道，不各自拼装一套加载器、不各自扫 storage 目录。
"""
from __future__ import annotations

import threading
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from tools import REGISTRY

from .atoms import AtomMemory
from .authoring import Authoring, AuthorResult
from .config import Settings, get_settings
from .domain import RunStatus, Scenario
from .events import FrameStore
from .intent import parse_intent
from .knowledge import KnowledgeStore
from .report import ReportWriter
from .session import EventListener, FinishedHook, RunSession, new_run_id, resume_run_id
from .skills import Skill, SkillLoader
from .store import Store
from .traces import Run


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
        self._authoring = Authoring(self.settings)

    # ---- 资产 -----------------------------------------------------------------
    def skill(self, name: str | None = None) -> Skill:
        return self.skills.load(name or self.settings.default_skill)

    def scenarios(self, skill: Skill) -> list[Scenario]:
        return skill.scenarios()

    # ---- 跑批 -----------------------------------------------------------------
    def _hooks(self, skill: Skill) -> Sequence[FinishedHook]:
        # 收尾三件事：记事实/流水、写报告、给原子候选数票。数票要知道写到哪个技能的 atoms/，
        # 所以 hook 按技能现做一个。
        return (self.knowledge.absorb, self.reports.write,
                lambda _record, _run_dir: self.atoms.settle(skill))

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

    def author(self, skill: Skill, ask: str, *, scenario: str | None = None,
               url: str | None = None) -> AuthorResult:
        return self._authoring.author(skill, ask, scenario=scenario, url=url)

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
        import yaml
        path = self.run_dir(run_id) / filename
        return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
