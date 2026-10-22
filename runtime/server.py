"""看板后端：列技能与场景、触发跑批、实时推进度与画面、放录屏。

handler 只做三件事：解析请求、调门面、整理响应。业务全在 runtime 里。
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from tools import BROWSER_TOOLS, REGISTRY, Tool

from . import recorder
from .app import QaAgent
from .cancel import CancelRegistry
from .concurrency import RunGate
from .context_vars import describe as describe_context
from .events import EventStream, FrameStore
from .expect import EmptyRouteError, load_expect, save_expect
from .intent import IntentError, load_intent, save_intent
from .intent import spec as intent_spec
from .playbook import PLAYBOOK_FILE, PlaybookError, load_playbook, save_playbook
from .session import JUDGE_PROTOCOL, new_run_id, resume_run_id
from .skills import Skill

frames = FrameStore()
stream = EventStream()
cancels = CancelRegistry()
core = QaAgent(frames=frames)
gate = RunGate(core.settings.max_concurrent_runs)   # 同时最多几个跑批在跑，见 concurrency.py
app = FastAPI(title="Roadbook Runner")

# 跑批是进程内的线程，服务一重启就全没了——所以此刻还标着 running 的记录一定是上次没收尾的。
# 不收掉的话，看板上那张卡会永远显示"跑着"，实时画面却永远不来。
if _orphans := core.sweep_orphans():
    print(f"[启动清扫] {len(_orphans)} 条没收尾的跑批记录已标为 error: {', '.join(_orphans)}")
# 原子候选的过期是读的时候现算的，这里只是顺手把早就「删了」的行从文件里清掉
if _expired := core.atoms.sweep():
    print(f"[启动清扫] {_expired} 条过期的原子候选已清掉")

SSE_POLL_SECONDS = 0.4
SSE_MAX_POLLS = 7200
MJPEG_BOUNDARY = "qaframe"
MJPEG_POLL_SECONDS = 0.1
MJPEG_IDLE_POLLS = 600           # 连续 60s 没新帧就收流


# ---- 请求模型 ----------------------------------------------------------------
class RunRequest(BaseModel):
    scenario: str
    skill: str | None = None
    headless: bool = True
    env: str | None = None      # 跑哪个环境；只认场景 frontmatter 里声明过的值


class AuthorRequest(BaseModel):
    ask: str
    skill: str | None = None
    scenario: str | None = None    # 给了就是改，不给就是新建
    url: str | None = None
    project: str | None = None     # 新建时指定放进哪个项目；None = 不挪，agent 放哪算哪


class ExpectRequest(BaseModel):
    skill: str | None = None
    goal: str = ""
    steps: list[dict[str, Any]] = []


class PlaybookRequest(BaseModel):
    """结构化改时给 steps，纯文本改时给 raw——两条路互斥。"""

    skill: str | None = None
    preamble: str = ""
    steps: list[dict[str, Any]] | None = None
    trailing: str = ""
    raw: str | None = None


class IntentRequest(BaseModel):
    """结构化存时给 meta/title/sections，老文件按纯文本存时给 raw。"""

    skill: str | None = None
    meta: dict[str, Any] = {}
    title: str = ""
    sections: dict[str, str] = {}
    extra: list[dict[str, Any]] = []
    raw: str | None = None


class AnswerRequest(BaseModel):
    answers: list[str]
    skill: str | None = None
    headless: bool = True


class MemoryVoteRequest(BaseModel):
    verdict: str                 # confirm | refute
    skill: str | None = None


# ---- 小工具 --------------------------------------------------------------------
def _skill(name: str | None) -> Skill:
    try:
        return core.skill(name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e


def _spawn(run_id: str, work: Callable[[], object], first_phase: str) -> None:
    """后台跑一件事，进度通过 SSE 频道 run_id 推出去；炸了也要把 done 事件发出去。

    位子不够就排队等 gate 空出来——抢位/占位都在工作线程里做，这次 HTTP 请求立即返回，
    发起方不用重试。队列里等的时候先推一条「排队中」，真正开跑再推 first_phase。
    """
    stream.open(run_id)
    queued = gate.is_full()
    stream.emit(run_id, "phase", f"排队中…（同时最多 {gate.limit} 个，等一个空位）" if queued else first_phase)

    def guarded() -> None:
        # 排队期间也要能响应「停止」——不然点了停止还得等一个空位、开一次浏览器才收口。
        if not gate.acquire(run_id, cancels.get(run_id)):
            stream.emit(run_id, "done", {"status": "stopped"})
            cancels.drop(run_id)
            return
        if queued:
            stream.emit(run_id, "phase", first_phase)
        try:
            work()
        except Exception as e:
            stream.emit(run_id, "done", {"status": "error", "error": str(e)})
        finally:
            gate.release(run_id)
            cancels.drop(run_id)   # 跑批结束（不管哪种收场）就不再需要停止信号了

    threading.Thread(target=guarded, daemon=True).start()


def _emitter(run_id: str) -> Callable[[str, Any], None]:
    return lambda kind, payload: stream.emit(run_id, kind, payload)


@app.get("/api/tools")
def api_tools() -> dict[str, str]:
    """工具清单：名字 → 类别。看板用它给轨迹上色，类别从注册表里的真实元数据推导，
    不让前端自己硬编码一份工具名单（两边一定会漂）。"""
    browser = {t.name for t in BROWSER_TOOLS}

    def kind(tool: Tool) -> str:
        if tool.terminal:
            return "end"                                  # finish
        if tool.name not in browser:
            return "asset"                                # 读资源 / 跑脚本 / 写场景文件
        return "act" if tool.mutates else "look"          # 改页面 / 只看页面

    return {name: kind(t) for name in REGISTRY.names() if (t := REGISTRY.get(name))}


# ---- 资产 ---------------------------------------------------------------------
def _safe_name(name: str | None) -> str:
    """项目名就是 scenarios/ 下的目录名：非空、不带路径分隔符、不玩路径把戏。"""
    name = (name or "").strip()
    if not name or name in {".", ".."} or name.startswith(".") \
            or "/" in name or "\\" in name:
        raise HTTPException(400, f"非法名称: {name!r}")
    return name


def _project_dirs(skill_root: Path) -> list[str]:
    """scenarios/ 下的项目目录（不带 intent.md 的子目录）。空项目也算，看板要能显示它。"""
    scen = skill_root / "scenarios"
    if not scen.is_dir():
        return []
    return sorted(d.name for d in scen.iterdir()
                  if d.is_dir() and not d.name.startswith(".") and not (d / "intent.md").exists())


def _move_scenario_dir(skill, scen_name: str, project: str) -> None:
    """把场景目录挪进 scenarios/<project>/；project 为空串 = 移回根（退出分组）。"""
    sc = skill.scenario(scen_name)
    target_parent = (skill.scenarios_dir / project) if project else skill.scenarios_dir
    if project and not target_parent.is_dir():
        raise HTTPException(400, f"项目目录不存在: {project}")
    target = target_parent / sc.name
    if target.exists():
        raise HTTPException(400, f"目标已存在: {target.relative_to(skill.root)}")
    shutil.move(str(sc.dir), str(target))


@app.get("/api/skills")
def api_skills() -> list[dict[str, Any]]:
    latest = core.latest_by_scenario()
    out = []
    for skill in core.skills.list_all():
        scenarios = []
        for sc in skill.scenarios():
            last = latest.get(sc.name)
            scenarios.append({
                **sc.to_dict(),
                "last": ({"run_id": last.run_id, "status": str(last.status), "verdict": str(last.verdict),
                          "finished_at": last.finished_at or last.started_at}
                         if last else None),
                "trend": core.knowledge.trend(sc.name, 5),
            })
        out.append({"name": skill.name, "description": skill.description,
                    "resources": len(skill.resources()), "scenarios": scenarios,
                    "projects": _project_dirs(skill.root)})
    return out


class ProjectRequest(BaseModel):
    skill: str | None = None
    name: str | None = None
    to: str | None = None            # 改名时的新名字


@app.post("/api/projects")
def api_project_create(req: ProjectRequest) -> dict[str, Any]:
    name = _safe_name(req.name)
    root = _skill(req.skill).root / "scenarios" / name
    if root.exists():
        raise HTTPException(400, f"已存在: scenarios/{name}")
    root.mkdir(parents=True)
    return {"ok": True, "project": name}


@app.post("/api/projects/rename")
def api_project_rename(req: ProjectRequest) -> dict[str, Any]:
    name = _safe_name(req.name)
    to = _safe_name(req.to)
    root = _skill(req.skill).root / "scenarios" / name
    if not root.is_dir():
        raise HTTPException(404, f"项目不存在: {name}")
    if (root / "intent.md").exists():
        raise HTTPException(400, f"{name} 是平铺场景不是项目")
    target = root.parent / to
    if target.exists():
        raise HTTPException(400, f"目标已存在: scenarios/{to}")
    root.rename(target)
    return {"ok": True, "project": to}


@app.delete("/api/projects/{name}")
def api_project_delete(name: str, skill: str | None = None) -> dict[str, Any]:
    name = _safe_name(name)
    root = _skill(skill).root / "scenarios" / name
    if not root.is_dir():
        raise HTTPException(404, f"项目不存在: {name}")
    try:
        root.rmdir()                                 # 只删得动空的；有场景就报错
    except OSError:
        raise HTTPException(400, f"项目 {name} 下还有场景，先移走再删")
    return {"ok": True}


class MoveRequest(BaseModel):
    skill: str | None = None
    project: str = ""                # 空串 = 移回根，退出分组


@app.post("/api/scenarios/{name}/move")
def api_scenario_move(name: str, req: MoveRequest) -> dict[str, Any]:
    project = _safe_name(req.project) if req.project else ""
    skill = _skill(req.skill)
    _move_scenario_dir(skill, name, project)
    return {"ok": True, "scenario": name, "project": project}


@app.post("/api/scenarios/author")
def api_author(req: AuthorRequest) -> dict[str, Any]:
    """用自然语言建或改一个场景。同步返回——这一步不碰浏览器，通常几十秒。"""
    result = core.author(_skill(req.skill), req.ask, scenario=req.scenario, url=req.url)
    # 新建时指了项目：agent 落笔的位置不归它管，生成完把目录挪进去。
    if req.scenario is None and req.project and result.ok and result.scenario:
        _move_scenario_dir(_skill(req.skill), result.scenario, _safe_name(req.project))
    return {"ok": result.ok, "scenario": req.scenario or result.scenario, "summary": result.summary,
            "notes": result.notes, "files": result.files, "steps": result.steps}


@app.get("/api/scenarios/{name}/runs")
def api_scenario_runs(name: str, limit: int = 20) -> list[dict[str, Any]]:
    """这个场景自己的运行记录，走索引查，不扫 storage 目录。"""
    return core.scenario_runs(name, limit)


@app.get("/api/scenarios/{name}/expect")
def api_get_expect(name: str, skill: str | None = None) -> dict[str, Any]:
    try:
        return load_expect(_skill(skill).scenario(name))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e


@app.get("/api/scenarios/{name}/context")
def api_context_vars(name: str, skill: str | None = None) -> dict[str, Any]:
    """验收路线里那些 `context.xxx` 分别是什么、上次的真实取值是多少。

    这是给人看的，不进任何提示词——裁判仍然只读 expect.yaml + 本次的 context.yaml。
    """
    scenario = _skill(skill).scenario(name)
    return describe_context(scenario, core.settings.knowledge_dir / "facts")


@app.put("/api/scenarios/{name}/expect")
def api_put_expect(name: str, req: ExpectRequest) -> dict[str, Any]:
    try:
        scenario = _skill(req.skill).scenario(name)
        return save_expect(scenario, req.goal, req.steps)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except EmptyRouteError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/scenarios/{name}/playbook")
def api_get_playbook(name: str, skill: str | None = None) -> dict[str, Any]:
    try:
        return load_playbook(_skill(skill).scenario(name))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e


@app.put("/api/scenarios/{name}/playbook")
def api_put_playbook(name: str, req: PlaybookRequest) -> dict[str, Any]:
    """结构化存（steps）或原样存（raw）。解析不了的路书只能走 raw，不然会被改坏。"""
    try:
        scenario = _skill(req.skill).scenario(name)
        if req.raw is not None:
            (scenario.dir / PLAYBOOK_FILE).write_text(req.raw, encoding="utf-8")
            return {"ok": True, "path": f"scenarios/{name}/{PLAYBOOK_FILE}", "raw": True}
        return save_playbook(scenario, req.preamble, req.steps or [], req.trailing)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except PlaybookError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/intent-spec")
def api_intent_spec() -> list[dict[str, Any]]:
    """intent.md 的书写标准，前端照它画表单。规范全文见 references/intent-spec.md，
    这里给的是同一套骨架的机器可读版——两边同源于 runtime/intent.py 的 SECTIONS。"""
    return intent_spec()


@app.get("/api/scenarios/{name}/intent")
def api_get_intent(name: str, skill: str | None = None) -> dict[str, Any]:
    try:
        return load_intent(_skill(skill).scenario(name))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e


@app.put("/api/scenarios/{name}/intent")
def api_put_intent(name: str, req: IntentRequest) -> dict[str, Any]:
    """按骨架存（meta/sections，校验必填小节和 description 长度），
    或对规范落地之前的老文件按纯文本原样存（raw）。"""
    try:
        scenario = _skill(req.skill).scenario(name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    if req.raw is not None:                      # 不合规的老文件：原样存，不套骨架
        if not req.raw.strip():
            raise HTTPException(400, "intent.md 不能存成空的")
        (scenario.dir / "intent.md").write_text(req.raw, encoding="utf-8")
        return {"ok": True, "path": f"scenarios/{name}/intent.md", "raw": True}
    try:
        return save_intent(scenario, {"meta": req.meta, "title": req.title,
                                      "sections": req.sections, "extra": req.extra})
    except IntentError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/skills/{skill}/judge-protocol")
def api_judge_protocol(skill: str) -> dict[str, Any]:
    """裁判取证协议。只读——它是 skill-core 文件，改了要回开源版改再同步，
    在这儿就地改会让内部仓库和开源版分叉。"""
    try:
        return {"text": _skill(skill).reference(JUDGE_PROTOCOL), "editable": False}
    except (FileNotFoundError, OSError) as e:
        raise HTTPException(404, str(e)) from e


@app.get("/api/memory")
def api_memory() -> dict[str, Any]:
    """原子记忆层给人看的一面：建议区里有什么、每条几票、最近账本。"""
    return {"enabled": core.atoms.enabled, "candidates": core.atoms.candidates(),
            "promoted": core.atoms.promoted_entries()[-20:][::-1], "recent": core.atoms.recent(40),
            "thresholds": {"append": core.atoms.confirms_append, "canonical": core.atoms.confirms_canonical,
                           "drop": core.atoms.refutes_drop, "demote": core.atoms.refutes_demote,
                           "ttl_days": core.atoms.ttl.days}}


@app.post("/api/memory/{cid}/vote")
def api_memory_vote(cid: str, req: MemoryVoteRequest) -> dict[str, Any]:
    """人在看板上投的票是终审：确认直接落盘，否决直接丢。改「它长什么样」只有这条路能落。"""
    if req.verdict not in ("confirm", "refute"):
        raise HTTPException(400, "verdict 只能是 confirm 或 refute")
    return {"ok": True, "message": core.atoms.human_vote(_skill(req.skill), cid, req.verdict)}


@app.get("/api/skills/{skill}/config")
def api_skill_config(skill: str) -> dict[str, Any]:
    """技能的环境配置原文（config/env.json）。看板拿它把 intent / 路书里
    `environments.dev.researcher_web` 这类引用就地解释成实际值——产品经理不用去翻 json。"""
    return _skill(skill).config()


# ---- 跑批 ---------------------------------------------------------------------
@app.post("/api/runs")
def api_start(req: RunRequest) -> dict[str, str]:
    skill = _skill(req.skill)
    run_id = new_run_id(req.scenario)
    cancel = cancels.create(run_id)
    env = core.pick_env(skill.scenario(req.scenario), req.env)
    _spawn(run_id, lambda: core.run(skill, req.scenario, run_id=run_id, headless=req.headless,
                                    emit=_emitter(run_id), cancel=cancel, env=req.env), "启动中")
    return {"run_id": run_id, "scenario": req.scenario, "env": env}


@app.post("/api/runs/{rid}/answer")
def api_answer(rid: str, req: AnswerRequest) -> dict[str, Any]:
    """agent 卡住问了问题，人回答完从冻结的现场接着跑。

    SSE 频道和新跑批的 run_id 是同一个值——以前一个是 `+resume`、一个带时间戳，对不上。
    """
    skill = _skill(req.skill)
    run_id = resume_run_id(rid)
    cancel = cancels.create(run_id)
    _spawn(run_id, lambda: core.resume(skill, rid, req.answers, run_id=run_id, headless=req.headless,
                                       emit=_emitter(run_id), cancel=cancel), "接上冻结的现场…")
    return {"ok": True, "events": run_id, "run_id": run_id}


@app.post("/api/runs/{rid}/stop")
def api_stop(rid: str) -> dict[str, Any]:
    """叫停一个在跑的跑批。不是杀进程——只是把信号置位，工作线程自己在下一个安全点收口，
    浏览器和录屏走正常关闭流程（见 runtime/cancel.py）。信号发出后 UI 上还会看到几秒钟
    running，属于正常——真正收口要等它跑到下一次查询点。"""
    if not cancels.cancel(rid):
        raise HTTPException(404, "这个跑批不存在或已经结束")
    return {"ok": True}


@app.get("/api/runs")
def api_runs(limit: int = 60) -> list[dict[str, Any]]:
    return [r.to_dict() for r in core.records(limit)]


@app.get("/api/runs/{rid}")
def api_run(rid: str) -> dict[str, Any]:
    record = core.record(rid)
    if record is None:
        raise HTTPException(404, "没有这次跑批")
    data = record.to_dict()
    # 结构化返回，让前端能可视化渲染而不是甩一坨 YAML
    data["context"] = core.read_yaml(rid, "context.yaml")
    data["judgment"] = core.read_yaml(rid, "judgment.yaml")
    data["report"] = core.report_text(rid)
    data["llm_calls"] = core.llm_calls(rid)   # 日志：这次跑批每一次调模型的记录，展示在详情里
    data["task"] = core.task_of(rid, record.scenario, record.skill or None)   # 当初要求它做什么
    # 验收路线（输入）也一起给，它才是人该编辑的那份
    try:
        data["expect"] = load_expect(core.skill(record.skill or None).scenario(record.scenario))
    except FileNotFoundError:
        data["expect"] = None
    return data


# ---- 实时 ---------------------------------------------------------------------
@app.get("/api/runs/{rid}/events")
def api_events(rid: str) -> StreamingResponse:
    def generate():
        cursor = 0
        for _ in range(SSE_MAX_POLLS):
            for event in stream.since(rid, cursor):
                cursor += 1
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event["kind"] == "done":
                    stream.close(rid)
                    return
            time.sleep(SSE_POLL_SECONDS)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/runs/{rid}/live.mjpeg")
def api_live(rid: str) -> StreamingResponse:
    """实时画面：multipart/x-mixed-replace，浏览器 <img> 原生就能放，前端零 JS。"""

    def generate():
        last_seq, idle = -1, 0
        while idle < MJPEG_IDLE_POLLS:
            frame, seq = frames.get(rid)
            if frame and seq != last_seq:
                last_seq, idle = seq, 0
                header = f"--{MJPEG_BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n"
                yield header.encode() + frame + b"\r\n"
            else:
                idle += 1
            time.sleep(MJPEG_POLL_SECONDS)

    return StreamingResponse(generate(), media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
                             headers={"Cache-Control": "no-store"})


@app.get("/api/runs/{rid}/video")
def api_video(rid: str, which: str = "runner") -> FileResponse:
    path = recorder.video_of(core.run_dir(rid), which)
    if not path:
        raise HTTPException(404, "没有录屏")
    return FileResponse(path, media_type="video/webm")


@app.get("/api/runs/{rid}/poster")
def api_poster(rid: str, which: str = "runner") -> FileResponse:
    path = recorder.poster_of(core.run_dir(rid), which)
    if not path:
        raise HTTPException(404, "没有截图")
    return FileResponse(path, media_type="image/png")


app.mount("/", StaticFiles(directory=str(core.settings.frontend_dir), html=True), name="frontend")
