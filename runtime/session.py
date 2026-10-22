"""一次跑批的编排：执行 → 留事实 → 独立裁判 → 判分 → 落产物。

`execute()` 和 `resume()` 是两个入口，跑完 agent 之后走同一条收尾链 `_settle()`。
以前这两条路各复制一份收尾逻辑，抽 `_judge` 时复制代码漂移出过 NameError——
重复不是风格问题，是已经咬过人的 bug 源。
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from tools import Browser, ToolContext

from . import validator
from .asset_writer import AssetWriter
from .atoms import AtomMemory
from .cancel import CancelledError
from .config import Settings
from .domain import Outcome, RunStatus, Scenario, Verdict
from .events import FrameStore
from .expect import load_expect, required_context_keys
from .llm import LLM, LlmCallInfo
from .loop import AgentLoop, LoopResult
from .playbook import PLAYBOOK_FILE, count_steps
from .prompts import PromptLibrary, TaskPrompts
from .skills import Skill
from .store import Store
from .subagents import SubAgentLoader
from .traces import Run, Step

EventListener = Callable[[str, Any], None]
FinishedHook = Callable[[Run, Path], object]

MAIN_AGENT = "agent"
JUDGE_AGENT = "judge"
JUDGE_PROTOCOL = "judge-protocol.md"

TRANSCRIPT_FILE = "transcript.json"
CONTEXT_FILE = "context.yaml"
JUDGMENT_FILE = "judgment.yaml"
STORAGE_STATE_FILE = "storage_state.json"
BLOCKED_FILE = "blocked.txt"
TASK_DIR = "task"                       # 这次跑批「被要求做什么」的存档
TASK_FILES = ("intent.md", "playbook.md")


def new_run_id(scenario: str) -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{scenario}"


def resume_run_id(previous: str) -> str:
    return f"{previous}+resume{datetime.now():%H%M%S}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _origin_of(url: str | None) -> str | None:
    parts = urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None


class RunSession:
    def __init__(
        self,
        skill: Skill,
        scenario: Scenario,
        run_id: str,
        settings: Settings,
        *,
        headless: bool = True,
        emit: EventListener | None = None,
        frames: FrameStore | None = None,
        store: Store,
        on_finished: Sequence[FinishedHook] = (),
        cancel: threading.Event | None = None,
        briefing: Callable[[str], str] | None = None,
        env: str = "",
        memory: AtomMemory | None = None,
    ):
        self.skill = skill
        self.scenario = scenario
        self.settings = settings
        self.headless = headless
        self.emit: EventListener = emit or (lambda _kind, _payload: None)
        self.frames = frames
        self.store = store
        self.on_finished = tuple(on_finished)
        self.cancel = cancel
        self._llm_call_seq = 0
        self.env = env
        # 裁判基准域名来自环境配置，不从浏览器最后落点反推。否则靶子被登录墙重定向后，
        # 会把 vercel.com 之类的第三方域名误当成被测站点。
        self._origin: str | None = self._configured_origin()

        self.run_dir = settings.storage_dir / run_id
        self.memory = memory                 # 原子记忆层；只给 runner，裁判那条路不接
        self.asset_writer = AssetWriter(skill, scenario.name)
        self.record = Run(run_id, scenario.name, skill.name, scenario.mode.value,
                          env=env, started_at=_now())
        self._persist()
        self._snapshot_task()

        # briefing 只进 runner 的任务说明；裁判那条路（self.prompts.judge）压根不碰它
        self.prompts = TaskPrompts(PromptLibrary(settings.prompts_dir), briefing,
                                   memory_brief=self._memory_brief)
        self.agents = SubAgentLoader(settings)
        self.llm = LLM(settings)

    def _configured_origin(self) -> str | None:
        config = self.skill.config()
        env_config = (config.get("environments") or {}).get(self.env, {})
        if not isinstance(env_config, dict):
            return None
        return _origin_of(str(env_config.get("researcher_web") or ""))

    # ---- 入口 ---------------------------------------------------------------
    def execute(self) -> Run:
        task = self.prompts.for_scenario(self.scenario, self.env)
        result = self._guarded(task=task, start_url=self.scenario.start_url)
        return self._settle(result) if result else self.record

    def resume(self, transcript: list[dict[str, Any]], answers: list[str]) -> Run:
        reply = self.prompts.resume(answers)
        result = self._guarded(resume_transcript=transcript, resume_reply=reply)
        return self._settle(result) if result else self.record

    def _snapshot_task(self) -> None:
        """把这次跑批「被要求做什么」原样存一份进跑批目录。

        路书是 agent 自己会回写的资产——探索完就改了。事后翻旧跑批时读当前版本，
        看到的可能根本不是当时执行的那一份。存档一次就没有这个歧义。
        """
        target = self.run_dir / TASK_DIR
        target.mkdir(parents=True, exist_ok=True)
        for name in TASK_FILES:
            text = self.scenario.read_or_empty(name)
            if text:
                (target / name).write_text(text, encoding="utf-8")
        # 开跑时路书记到第几步。跑完再数一次，差值就是这次勘进了多少——
        # 渐进式路书之后，这是最直接的「进度」口径，不用 agent 额外汇报什么
        self.record.playbook_before = count_steps(self.scenario.read_or_empty(PLAYBOOK_FILE))

    @staticmethod
    def load_transcript(previous_run_dir: Path) -> list[dict[str, Any]]:
        path = previous_run_dir / TRANSCRIPT_FILE
        if not path.exists():
            raise FileNotFoundError(f"{previous_run_dir.name} 没有冻结现场，接不上")
        return json.loads(path.read_text(encoding="utf-8"))["messages"]

    # ---- 主 agent -------------------------------------------------------------
    def _guarded(
        self,
        *,
        task: str | None = None,
        resume_transcript: list[dict[str, Any]] | None = None,
        resume_reply: str | None = None,
        start_url: str | None = None,
    ) -> LoopResult | None:
        """跑主 agent；浏览器/模型异常统一在这里收成 error 状态，不散在各处。

        是续跑还是从头跑，靠 resume_transcript 有没有值来分——execute() 只传 task，
        resume() 只传 resume_transcript/resume_reply，两边不会同时传。
        """
        try:
            loop = AgentLoop(self.agents.load(MAIN_AGENT), self.llm,
                             on_step=lambda s: self._on_step(s, judge=False),
                             on_llm_call=lambda info: self._on_llm_call(info, judge=False),
                             required_facts=required_context_keys(self.scenario),
                             cancel=self.cancel,
                             compact_at=self.settings.compact_at_tokens,
                             compact_prompt=self.prompts.compact(required_context_keys(self.scenario)),
                             refresh_task=lambda: self.prompts.for_scenario(self.scenario, self.env))
            with self._browser("runner") as browser:
                loop.fingerprint = browser.fingerprint   # 原地踏步检测的环境指纹来源
                if start_url:
                    browser.navigate(start_url)
                ctx = ToolContext(browser, self.skill, self.scenario.name,
                                  run_id=self.record.run_id, memory=self.memory,
                                  playbook_step_sink=self.asset_writer.enqueue_playbook_step)
                if resume_transcript is not None:
                    result = loop.resume(ctx, resume_transcript, resume_reply or "")
                else:
                    result = loop.run(ctx, self.skill.brief(), task or "")
                # 把被测应用的落盘状态交给裁判继承（否则验持久化的场景必然误判）
                browser.save_storage_state(self.run_dir / STORAGE_STATE_FILE)
                # 老 skill 没有 environments 配置时才退守当前地址；配置存在时绝不让第三方
                # 重定向覆盖被测 origin。
                if self._origin is None:
                    self._origin = _origin_of(browser.current_url())
            return result
        except CancelledError:
            # with 块已经正常退出（Browser.__exit__ 照跑，录屏落盘、子进程正常退出）
            self._stopped()
            return None
        except Exception as e:
            self._fail(f"{type(e).__name__}: {e}")
            return None

    def _browser(self, role: str, *, storage_state: Path | None = None) -> Browser:
        return Browser(
            self.run_dir / role,
            headless=self.headless,
            storage_state=storage_state,
            vercel_bypass_secret=self.settings.vercel_bypass_secret,
            vercel_bypass_hosts=self.settings.vercel_bypass_hosts,
            on_frame=self.frames.sink_for(self.record.run_id) if self.frames else None,
            dedupe_observations=self.settings.dedupe_observations,
        )

    def _memory_brief(self, scenario: Scenario) -> str:
        """任务说明里的原子候选。没接记忆层就返回空串，提示词里整段不出现。"""
        return self.memory.brief_for(scenario) if self.memory else ""

    def _on_step(self, step: Step, *, judge: bool) -> None:
        if step.tool == "upsert_playbook_step":
            self.record.playbook_written = True
        (self.record.judge_steps if judge else self.record.steps).append(step.to_dict())
        self._persist()
        self.emit("judge_step" if judge else "step", step.to_dict())

    def _on_llm_call(self, info: LlmCallInfo, *, judge: bool) -> None:
        """日志：每次真正调了模型，就落一行——挂在这次跑批的 run_id 下面，
        跑批详情里能直接看到调了几次、每次多少 token、多久。"""
        self._llm_call_seq += 1
        self.store.record_llm_call(
            run_id=self.record.run_id, scenario=self.scenario.name, judge=judge,
            seq=self._llm_call_seq, requested_at=_now(), info=info,
        )

    def _persist(self) -> None:
        """文件是详情，库是索引——每次状态变化两边一起写，别让它们分叉。"""
        self.record.save(self.run_dir)
        self.store.upsert_run(
            run_id=self.record.run_id, scenario=self.record.scenario, skill=self.record.skill,
            status=str(self.record.status), verdict=str(self.record.verdict),
            started_at=self.record.started_at, finished_at=self.record.finished_at,
        )

    # ---- 收尾链 ---------------------------------------------------------------
    def _settle(self, result: LoopResult) -> Run:
        record = self.record
        record.summary, record.notes = result.summary, result.notes
        record.runner_outcome = str(result.outcome)

        if result.outcome is Outcome.NEEDS_INPUT:
            return self._freeze(result)

        if result.outcome is Outcome.BLOCKED:
            (self.run_dir / BLOCKED_FILE).write_text(result.summary, encoding="utf-8")
        facts = validator.flatten_facts(result.facts)
        (self.run_dir / CONTEXT_FILE).write_text(
            yaml.safe_dump(facts, allow_unicode=True, sort_keys=False), encoding="utf-8")

        # 环境已经挡住 runner 时没有业务事实可复核，起裁判只会二次访问错误页面、浪费模型，
        # 甚至把第三方重定向页当成被测产品。BLOCK 由 runner 的环境证据直接收口。
        if result.outcome is Outcome.BLOCKED:
            record.status, record.verdict = RunStatus.BLOCKED, Verdict.BLOCK
            return self._finish()

        if not self.scenario.has_expect:          # 没有验收路线：产物就是探索出来的路书
            record.status = RunStatus.PASSED if result.outcome is Outcome.DONE else RunStatus(result.outcome)
            record.verdict = Verdict.EXPLORED
            return self._finish()

        self.emit("phase", "执行完成，起独立裁判")
        return self._judge()

    def _freeze(self, result: LoopResult) -> Run:
        """探索卡住了：冻结现场、记下要问的问题，等人回答后接着跑。"""
        self.record.status, self.record.verdict = RunStatus.NEEDS_INPUT, Verdict.ASK
        self.record.questions, self.record.resumable = result.questions, True
        (self.run_dir / TRANSCRIPT_FILE).write_text(
            json.dumps({"messages": result.transcript}, ensure_ascii=False), encoding="utf-8")
        return self._finish()

    def _judge(self) -> Run:
        """独立裁判：全新会话 + 全新对话，但继承被测应用的落盘状态。"""
        try:
            task = self.prompts.judge(
                self.scenario,
                protocol=self.skill.reference(JUDGE_PROTOCOL),
                context_yaml=(self.run_dir / CONTEXT_FILE).read_text(encoding="utf-8"),
                origin=self._origin,
            )
            loop = AgentLoop(self.agents.load(JUDGE_AGENT), self.llm,
                             on_step=lambda s: self._on_step(s, judge=True),
                             on_llm_call=lambda info: self._on_llm_call(info, judge=True),
                             cancel=self.cancel,
                             compact_at=self.settings.compact_at_tokens,
                             compact_prompt=self.prompts.compact())
            with self._browser("judge", storage_state=self.run_dir / STORAGE_STATE_FILE) as browser:
                loop.fingerprint = browser.fingerprint
                result = loop.run(ToolContext(browser, self.skill, read_only=True),
                                  self.skill.brief(), task)
        except CancelledError:
            return self._stopped()
        except Exception as e:
            return self._fail(f"裁判阶段失败: {type(e).__name__}: {e}")

        # 裁判没交出 judgment 时，把"它为什么没交"记下来。否则下游只会看到
        # 「judgment.yaml 有 0 步」这种二手症状，真因（轮数耗尽 / 没调 finish）全丢了。
        # 2026-09-17 实测：裁判 31 个工具调用撞到 max_iterations，界面上完全看不出来。
        if not result.facts.get("judgment"):
            self.record.judge_incomplete = f"{result.outcome}｜{result.summary}".strip("｜")

        judgment = dict(result.facts.get("judgment") or self._unjudged(result))
        judgment.setdefault("scenario", self.scenario.name)
        judgment.setdefault("judged_at", _now())
        (self.run_dir / JUDGMENT_FILE).write_text(
            yaml.safe_dump(judgment, allow_unicode=True, sort_keys=False), encoding="utf-8")

        code, output = validator.score(self.skill.root, self.scenario.name, self.run_dir)
        self.record.verdict = validator.VERDICT.get(code, f"EXIT{code}")
        self.record.judge_output = output
        self.record.status = validator.STATUS.get(code, RunStatus.ERROR)
        return self._finish()

    def _unjudged(self, result: LoopResult) -> dict[str, Any]:
        """裁判没交答卷时，替它交一份「逐条 blocked」的镜像答卷。

        以前这里留空，judgment.yaml 只剩两行默认字段，判分脚本报的是
        「expect.yaml 有 2 步，judgment.yaml 有 0 步」——看的人根本不知道发生了什么。
        照 expect.yaml 镜像出来、每条标 blocked 并写明真因，判分脚本一样判 BLOCK，
        但报告里能逐条看到「这条没判，因为裁判轮数耗尽」。
        """
        why = f"裁判没走到 finish（{result.outcome}）：{result.summary}".strip()
        expect = load_expect(self.scenario)
        return {
            "scenario": self.scenario.name,
            "steps": [
                {"go": step.get("go", ""), "reached": False, "how": why,
                 "check": [{"fact": c, "result": "blocked", "note": why}
                           for c in (step.get("check") or [])]}
                for step in expect.get("steps") or []
            ],
        }

    def _fail(self, error: str) -> Run:
        self.record.status, self.record.error = RunStatus.ERROR, error
        return self._finish()

    def _stopped(self) -> Run:
        """人手动叫停，不是失败——跟 _fail 分开收尾，别让停止在界面上看着像出错了。"""
        self.record.status = RunStatus.STOPPED
        self.record.verdict = Verdict.STOPPED
        self.record.summary = self.record.summary or "被手动停止"
        return self._finish()

    def _finish(self) -> Run:
        written, asset_errors = self.asset_writer.flush()
        if written:
            self.record.playbook_written = True
        if asset_errors:
            warning = "资产沉淀失败（不改变业务结论）：" + "；".join(asset_errors)
            self.record.notes = (self.record.notes + "\n\n" + warning).strip()
        self.record.playbook_after = count_steps(self.scenario.read_or_empty(PLAYBOOK_FILE))
        if self.frames:
            self.frames.drop(self.record.run_id)   # 跑完清掉实时帧，看板自动切回录屏
        self.record.finished_at = _now()
        self._persist()
        for hook in self.on_finished:
            hook(self.record, self.run_dir)
        self.emit("done", self.record.to_dict())
        return self.record
