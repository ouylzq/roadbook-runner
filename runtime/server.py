"""看板后端：列技能与场景、触发跑批、实时推进度与画面、放录屏。

handler 只做三件事：解析请求、调门面、整理响应。业务全在 runtime 里。
"""
from __future__ import annotations

import json
import re
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
from tools.base import legal_scenario_name

from . import recorder
from .app import QaAgent
from .cancel import CancelRegistry
from .concurrency import RunGate
from .context_vars import describe as describe_context
from .doctor import Doctor
from .events import EventStream, FrameStore
from .expect import EmptyRouteError, load_expect, save_expect
from .intent import IntentError, load_intent, save_intent
from .intent import spec as intent_spec
from .playbook import PLAYBOOK_FILE, PlaybookError, load_playbook, save_playbook
from .public_config import (
    extract_origins,
    reverse_lookup,
    sanitize_skill_config,
    suggest_key,
    validate_entry,
)
from .session import JUDGE_PROTOCOL, new_run_id, resume_run_id
from .skills import Skill
from .traces import Run

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


def _prod_fence(env: str) -> None:
    """prod 围栏：线上跑批默认挡在服务端——前端只是体验，这里是真闸门。
    prod 建出来的用户删不掉，注册类用例还要人工点头，所以默认宁可跑不了。"""
    if (env or "").strip().lower() == "prod" and not core.settings.allow_prod:
        raise HTTPException(
            403,
            "prod 被围栏挡住了：看板默认不许触发线上跑批（prod 建出来的用户删不掉）。"
            "确实要对线上跑回归，在 .env 里设 QA_AGENT_ALLOW_PROD=on 再重启服务。")


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
def _latest_for(scenario_name: str, latest: dict[str, Run]) -> Run | None:
    """卡片归属的历史记录：先按全名精确匹配，对不上再按叶子名认领——
    历史记录是平铺命名（login），现在的卡是分组命名（team/login），名字对不上
    卡片就会对着一屋子历史说「还没跑过」。外部记录（roadbook/runs 迁来的文字产物）
    没有封面和录屏，不上卡片，只在历史列表里。"""
    exact = latest.get(scenario_name)
    if exact:
        return exact
    leaf = scenario_name.rsplit("/", 1)[-1]
    cands = [r for k, r in latest.items()
             if (k == leaf or k.endswith("/" + leaf)) and not r.external]
    return max(cands, key=lambda r: r.run_id) if cands else None


@app.get("/api/skills")
def api_skills() -> list[dict[str, Any]]:
    latest = core.latest_by_scenario()
    out = []
    for skill in core.skills.list_all():
        scenarios = []
        for sc in skill.scenarios():
            last = _latest_for(sc.name, latest)
            scenarios.append({
                **sc.to_dict(),
                "last": ({"run_id": last.run_id, "status": str(last.status), "verdict": str(last.verdict),
                          "finished_at": last.finished_at or last.started_at}
                         if last else None),
                "trend": core.knowledge.trend(sc.name, 5),
            })
        out.append({"name": skill.name, "description": skill.description,
                    "resources": len(skill.resources()), "scenarios": scenarios,
                    "projects": skill.project_groups()})
    return out


class AuthorRequest(BaseModel):
    ask: str
    skill: str | None = None
    scenario: str | None = None    # 给了就是改，不给就是新建
    url: str | None = None
    name: str | None = None        # 新建时点名场景目录名；留空由模型起
    project: str | None = None     # 新建时指定项目分组（scenarios/<项目>/<场景>/）
    config_entries: list[dict[str, str]] = []   # 确认卡上 PM 点头的新域名，先落配置再生成


# 生成任务台账（内存）：让"生成中"状态跨刷新可见、失败不再无痕。
# 服务重启即清——重启时正在生成的任务本来也不存在了。
AUTHOR_TASKS: dict[str, dict[str, Any]] = {}
AUTHOR_TASKS_LOCK = threading.Lock()


@app.get("/api/author/active")
def api_author_active() -> dict[str, Any]:
    with AUTHOR_TASKS_LOCK:
        items = sorted(AUTHOR_TASKS.values(), key=lambda t: t["started_at"], reverse=True)[:20]
    return {"running": [t for t in items if t["status"] == "running"], "recent": items}


class ConfigMatchRequest(BaseModel):
    text: str = ""
    url: str | None = None
    skill: str | None = None


@app.post("/api/skills/{skill}/config/match")
def api_config_match(skill: str, req: ConfigMatchRequest) -> dict[str, Any]:
    """生成前的域名反查（确定性正则，不经过模型）：
    文本/地址里的 origin 命中现有配置 → 告诉生成 agent 用键名引用；
    没命中 → 返回建议键名，前端弹确认卡，PM 点头才落配置。"""
    sk = _skill(skill)
    cfg = sk.config()
    matched, fresh = [], []
    for origin in extract_origins(f"{req.text}\n{req.url or ''}"):
        hit = reverse_lookup(cfg, origin)
        if hit:
            matched.append({"origin": origin, "env": hit[0], "key": hit[1]})
        else:
            fresh.append({"origin": origin, "suggested_key": suggest_key(origin)})
    return {"matched": matched, "new": fresh}


@app.post("/api/scenarios/author")
def api_author(req: AuthorRequest) -> dict[str, Any]:
    """异步提交：立刻返回 author_id，进度和结果走 SSE（/api/author/{aid}/events）。

    以前是同步等一次 LLM 调用——设计假设"几十秒的短调用"，网关一抖 PM 的弹窗就无限
    "整理中"。现在复用跑批的异步机制（_spawn + 事件流）：进度可见、关掉弹窗不影响生成。
    """
    sk = _skill(req.skill)
    aid = f"author-{time.strftime('%Y%m%d%H%M%S')}"
    cancels.create(aid)

    # 选了项目但没填场景名 → 自动补叶子名。绝不能拿项目分组名当场景名：
    # 2026-10-23 实测那样会把 intent/expect 写进分组目录根部，整个组的场景全部隐身。
    if not req.scenario and req.project and not req.name:
        req.name = f"scene-{time.strftime('%H%M%S')}"

    # 确认卡上 PM 点头的新域名：先落配置，再把「地址 → 键」映射交给生成 agent 当硬约束。
    # 配置的键和值永远来自 PM 的确认，模型只负责在场景里引用键名。
    config_hints: dict[str, str] = {}
    cfg = _load_skill_config(sk)
    envs = cfg.setdefault("environments", {})
    for e in req.config_entries or []:
        env, key, value = e.get("env", ""), e.get("key", ""), e.get("value", "")
        if (err := validate_entry(env, key, value, known_envs=sorted(envs))) is not None:
            raise HTTPException(400, err)
        bucket = envs.setdefault(env, {})
        if not isinstance(bucket, dict):
            raise HTTPException(400, f"environments.{env} 不是键值结构")
        bucket[key] = value
        config_hints[value] = key
        _write_skill_config(sk, cfg)
    for origin in extract_origins(f"{req.ask}\n{req.url or ''}"):
        hit = reverse_lookup(cfg, origin)
        if hit:
            config_hints.setdefault(origin, hit[1])

    # 提交瞬间先落占位 intent.md（有名字的新建才做）——"添加成功"是即时的：
    # 卡片立刻出现在列表里标「生成中」，生成完成后原地覆盖，失败则清掉占位不留空壳
    stub_dir = None
    if not req.scenario and (req.name or req.project):
        full = "/".join(x for x in (req.project, req.name) if x)
        if full and legal_scenario_name(full):
            stub_dir = Path(sk.scenarios_dir) / full
            try:
                stub_dir.mkdir(parents=True, exist_ok=False)
                ask_head = (req.ask or "")[:60]
                (stub_dir / "intent.md").write_text(
                    f"---\nname: {req.name or full}\n"
                    f"description: ⏳ 场景生成中（{ask_head}）——完成后本描述会被真实内容替换\n"
                    f"---\n\n# {req.name or full}\n\n（正在生成，约 1~3 分钟）\n",
                    encoding="utf-8")
            except FileExistsError:
                stub_dir = None

    entry: dict[str, Any] = {"aid": aid, "name": req.name or "", "scenario": None,
                             "project": req.project or "", "ask": (req.ask or "")[:60],
                             "status": "running", "ok": None,
                             "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with AUTHOR_TASKS_LOCK:
        AUTHOR_TASKS[aid] = entry

    def work():
        try:
            result = core.author(sk, req.ask, scenario=req.scenario, url=req.url,
                             name=req.name, project=req.project,
                             config_hints=config_hints or None)
        except Exception as e:
            if stub_dir is not None and stub_dir.exists() and not (stub_dir / "expect.yaml").exists():
                shutil.rmtree(stub_dir, ignore_errors=True)   # 生成失败：占位一并撤掉，列表不留空壳
            with AUTHOR_TASKS_LOCK:
                entry.update(status="error", ok=False, summary=f"{type(e).__name__}: {e}")
            print(f"[author] {aid} 失败：{type(e).__name__}: {e}")
            raise
        with AUTHOR_TASKS_LOCK:
            entry.update(status="done", ok=result.ok, scenario=result.scenario,
                         summary=(result.summary or "")[:100])
        print(f"[author] {aid} 完成：scenario={result.scenario} ok={result.ok} 文件 {len(result.files)} 个")
        stream.emit(aid, "done", {"status": "done", "result": {
            "ok": result.ok, "scenario": result.scenario,
            "summary": result.summary, "notes": result.notes, "files": result.files}})

    _spawn(aid, work, "整理中：读现有场景学格式、生成三件套…")
    return {"author_id": aid, "scenario_dir": str(stub_dir) if stub_dir else None}


@app.get("/api/author/{aid}/events")
def api_author_events(aid: str) -> StreamingResponse:
    """生成任务的进度流：和跑批的 events 同一条通道约定，done 即收口。"""
    def generate():
        cursor = 0
        for _ in range(SSE_MAX_POLLS):
            for event in stream.since(aid, cursor):
                cursor += 1
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event["kind"] == "done":
                    stream.close(aid)
                    return
            time.sleep(SSE_POLL_SECONDS)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/scenarios/{name:path}/runs")
def api_scenario_runs(name: str, limit: int = 20) -> list[dict[str, Any]]:
    """这个场景自己的运行记录，走索引查，不扫 storage 目录。"""
    return core.scenario_runs(name, limit)


@app.get("/api/scenarios/{name:path}/expect")
def api_get_expect(name: str, skill: str | None = None) -> dict[str, Any]:
    try:
        return load_expect(_skill(skill).scenario(name))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e


@app.get("/api/scenarios/{name:path}/context")
def api_context_vars(name: str, skill: str | None = None) -> dict[str, Any]:
    """验收路线里那些 `context.xxx` 分别是什么、上次的真实取值是多少。

    这是给人看的，不进任何提示词——裁判仍然只读 expect.yaml + 本次的 context.yaml。
    """
    scenario = _skill(skill).scenario(name)
    return describe_context(scenario, core.settings.knowledge_dir / "facts")


@app.put("/api/scenarios/{name:path}/expect")
def api_put_expect(name: str, req: ExpectRequest) -> dict[str, Any]:
    try:
        scenario = _skill(req.skill).scenario(name)
        return save_expect(scenario, req.goal, req.steps)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except EmptyRouteError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/scenarios/{name:path}/playbook")
def api_get_playbook(name: str, skill: str | None = None) -> dict[str, Any]:
    try:
        return load_playbook(_skill(skill).scenario(name))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e


@app.put("/api/scenarios/{name:path}/playbook")
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


@app.get("/api/scenarios/{name:path}/intent")
def api_get_intent(name: str, skill: str | None = None) -> dict[str, Any]:
    try:
        return load_intent(_skill(skill).scenario(name))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e


@app.put("/api/scenarios/{name:path}/intent")
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
    """技能的环境配置，**脱敏后**的版本。看板拿它把 intent / 路书里
    `environments.dev.researcher_web` 这类引用就地解释成实际值——产品经理不用去翻 json。
    redis、凭据文件指针、账号、内网网关这些不出服务端：PM 的浏览器用不到它们，
    agent 自己是用 read_resource 读原文的，不受这里影响。"""
    return sanitize_skill_config(_skill(skill).config())


class ConfigEntryRequest(BaseModel):
    """从看板新增/修改一条域名类配置（如给新场景加 study_web）。只接受 http(s) 地址，
    写进 environments.<env>.<key>；redis/凭据这类受保护键在 public_config 里硬拦。"""

    env: str
    key: str
    value: str
    skill: str | None = None


def _load_skill_config(sk: Skill) -> dict[str, Any]:
    path = sk.config_path
    if not path.is_file():
        return {}
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise HTTPException(500, f"config/env.json 解析失败，先人工修复: {e}") from e
    if not isinstance(cfg, dict):
        raise HTTPException(500, "config/env.json 顶层不是键值结构，先人工修复")
    return cfg


def _write_skill_config(sk: Skill, cfg: dict[str, Any]) -> None:
    path = sk.config_path
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


@app.post("/api/skills/{skill}/config/entries")
def api_put_config_entry(skill: str, req: ConfigEntryRequest) -> dict[str, Any]:
    sk = _skill(req.skill or skill)
    cfg = _load_skill_config(sk)
    known_envs = sorted((cfg.get("environments") or {}).keys())
    if (err := validate_entry(req.env, req.key, req.value, known_envs=known_envs)) is not None:
        raise HTTPException(400, err)
    envs = cfg.setdefault("environments", {})
    entries = envs.setdefault(req.env, {})
    if not isinstance(entries, dict):
        raise HTTPException(400, f"environments.{req.env} 不是键值结构，先人工修复")
    entries[req.key] = req.value
    _write_skill_config(sk, cfg)
    return {"ok": True, "config": sanitize_skill_config(cfg)}


@app.delete("/api/skills/{skill}/config/entries/{env}/{key}")
def api_del_config_entry(skill: str, env: str, key: str) -> dict[str, Any]:
    # 删除和写入走同一套校验——受保护键（prod 的 protected_user_ids 是清理脚本的护栏，
    # 删了护栏就静默失效）和内置键（场景可能正引用）都不能从看板动。
    sk = _skill(skill)
    cfg = _load_skill_config(sk)
    known_envs = sorted((cfg.get("environments") or {}).keys())
    if (err := validate_entry(env, key, "https://placeholder.invalid", known_envs=known_envs)) is not None:
        raise HTTPException(400, err)
    entries = (cfg.get("environments") or {}).get(env)
    if not isinstance(entries, dict) or key not in entries:
        raise HTTPException(404, f"environments.{env}.{key} 不存在")
    used_by = []
    for sc in sk.scenarios():
        for fname in ("intent.md", "expect.yaml"):
            fpath = sc.dir / fname
            if fpath.exists() and re.search(
                    rf"(environments\.[A-Za-z0-9_.-]+\.)?{re.escape(key)}\b",
                    fpath.read_text(encoding='utf-8', errors='replace')):
                used_by.append(sc.name)
                break
    if used_by:
        raise HTTPException(409, f"{key!r} 正被这些场景引用：{'、'.join(used_by)}；先改场景再删")

    del entries[key]
    _write_skill_config(sk, cfg)
    return {"ok": True, "config": sanitize_skill_config(cfg)}


@app.get("/api/meta")
def api_meta() -> dict[str, Any]:
    """看板自己的开关面：prod 围栏状态等。前端据此置灰 prod 选项。"""
    return {"allow_prod": core.settings.allow_prod}


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    """把 Doctor 的快速检查搬进看板：顶栏的状态点用它。不查被测靶子和历史产物
    （那是 `qa check` 的活，慢），只查这个进程自己能不能干活。"""
    doctor = Doctor(core.settings)
    flat = [c for group in (doctor.env(), doctor.gateway(), doctor.browser(), doctor.agents())
            for c in group]
    return {"ok": all(c.ok or c.warn for c in flat),
            "checks": [c.__dict__ | {"mark": c.mark} for c in flat]}


# ---- 跑批 ---------------------------------------------------------------------
@app.post("/api/runs")
def api_start(req: RunRequest) -> dict[str, str]:
    skill = _skill(req.skill)
    run_id = new_run_id(req.scenario)
    cancel = cancels.create(run_id)
    env = core.pick_env(skill.scenario(req.scenario), req.env)
    _prod_fence(env)
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
    if (previous := core.record(rid)) is not None:
        _prod_fence(previous.env)      # 接着跑要留在原来那个环境——原来就是 prod 的同样要过围栏
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
