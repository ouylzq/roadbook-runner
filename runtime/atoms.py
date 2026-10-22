"""原子记忆层：模型只能提议和投票，代码数票落盘，人在 git diff 上把关。

`atoms/` 是被测产品的共享操作记忆，一个原子会被好几份路书引用。让跑批中的模型直接改它，
一次跑歪（弹层没渲染完、A/B 分流、临时故障）就会以「经验」的形式传染给所有引用者，而且
顺利回放时没人会打开原子——错了也不知道错在哪。所以写入权和可信度在这里拆开：

- 模型手里只有两个动作：propose_atom（往建议区写一条）、vote_atom（对一条候选投票）。
  **没有任何工具能写 atoms/*.md**——落盘的每一笔都是这个类干的。
- 建议区（knowledge/atoms-pending.jsonl）里的候选，从写下那一刻起就会被注入后续跑批的
  任务说明——传播是即时的，延迟的只是「信任」。
- 每次跑批收尾时数票：另一次跑批按它试过、投了确认 → 追加进原子；投了否决 → 丢；
  太久没人验证 → 过期。过期不是定时任务，是读的时候现算（跟 atoms_status.py 的「陈旧」一样）。
- 唯一的例外是**全新的地方**：这个节点还没有原子，就没有任何场景在依赖它，写错了带不歪
  任何人——直接落盘，不排队。同一次跑批给自己刚建的原子再补内容也走这条：依赖从别的
  跑批读到它那一刻才开始。
- 无论哪条路，落到 atoms/ 的都是工作区里一份未提交的 diff，人看过才 commit
  （atoms/README.md：「审批门就是 git diff」）。

阈值全部来自 Settings.atom_*。改「它长什么样」那一档这版只收不办：进建议区、能被读到，
但永不自动落盘——那是唯一会让顺利回放改道的改动，留给人点确认。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .domain import Scenario
from .playbook import load_playbook
from .skills import Skill

PENDING_FILE = "atoms-pending.jsonl"
LEDGER_FILE = "atoms-ledger.jsonl"

# 提议的三种类型。前两种是给已有原子追加，第三种是开一个新原子；canonical 只收不办。
KIND_SECTIONS = {"variant": "变体", "failure_mode": "已知会失败的方式"}
KIND_NEW = "new"
KIND_CANONICAL = "canonical"
KINDS = (*KIND_SECTIONS, KIND_NEW, KIND_CANONICAL)
SECTION_ORDER = ("它长什么样", "变体", "已知会失败的方式", "出处")

# 跟 atoms/README.md 和 tests/lint_atoms.py 同一套口径
NODE_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
RESERVED_NODES = frozenset({"readme", "status"})
DESC_MIN, DESC_MAX = 25, 90
TEXT_MAX_LINES, TEXT_MAX_CHARS = 3, 320
SIMILARITY = 0.75

CLOSED = "原子记忆层已关闭（QA_AGENT_ATOM_MEMORY=off），这次跑批不记原子。"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _cjk_len(s: str) -> int:
    return len(s.replace(" ", ""))


def _norm(s: str) -> str:
    return re.sub(r"[\s`「」『』“”\"'，。,.：:；;！!？?（）()→\-—·]", "", s).lower()


def _similar(a: str, b: str) -> bool:
    """同一件事被两次跑批各自说出来，措辞不会一样，但**顺序**一样：现象、机制、对策各换几个字。
    按顺序对齐的匹配率（difflib）抓的就是这个；字二元组 Jaccard 对「能/可以」「不必/不用」
    这种同义替换太敏感（实测 0.59），会把同一条教训判成两条。要的是「一眼看出是同一条」，
    不是语义匹配。"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= SIMILARITY


def _section(text: str, title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1) if m else ""


def _bullets(block: str) -> list[str]:
    return [ln.strip()[2:].strip() for ln in block.splitlines() if ln.strip().startswith("- ")]


def append_bullet(text: str, section: str, bullet: str) -> str:
    """把一条追加到某一节末尾。**只插一行**，别的地方一个字节不动——落盘的是给人审的 diff，
    重排别的段落会让「加了一条」看起来像「改了一篇」。节不存在就按固定顺序补一节。"""
    lines = text.rstrip("\n").split("\n")
    heads = [i for i, ln in enumerate(lines) if ln.startswith("## ")]
    idx = next((i for i in heads if lines[i].strip() == f"## {section}"), None)
    if idx is None:
        # 插在顺序上排它后面的第一节之前；没有就放最后
        later = [t for t in SECTION_ORDER[SECTION_ORDER.index(section) + 1:]]
        at = next((i for i in heads if lines[i].strip()[3:] in later), len(lines))
        block = ["", f"## {section}", "", f"- {bullet}"]
        lines[at:at] = block if at == len(lines) else [*block, ""]
        return "\n".join(lines).lstrip("\n") + "\n"
    end = next((i for i in heads if i > idx), len(lines))
    j = end
    while j > idx + 1 and not lines[j - 1].strip():
        j -= 1
    lines[j:j] = [f"- {bullet}"]
    return "\n".join(lines) + "\n"


def render_new_atom(node: str, description: str, looks_like: str, scenario: str,
                    first_entry: tuple[str, str] | None = None) -> str:
    """一个全新原子的正文，格式照 atoms/README.md「文件格式」那一节，能过 lint_atoms.py。"""
    out = ["---", f"name: {node}", f"description: {description.strip()}", "---", "",
           f"# {node}", "", "## 它长什么样", "", looks_like.strip()]
    if first_entry:
        section, bullet = first_entry
        out += ["", f"## {section}", "", f"- {bullet}"]
    out += ["", "## 出处", "", f"- `scenarios/{scenario}/playbook.md`", ""]
    return "\n".join(out)


def replace_section(text: str, section: str, body: str) -> str:
    """整段换掉某一节的正文，别的字节不动。只给「它长什么样」的人工确认用——那是唯一会让
    顺利回放改道的改动，永远不自动做。"""
    lines = text.rstrip("\n").split("\n")
    heads = [i for i, ln in enumerate(lines) if ln.startswith("## ")]
    idx = next((i for i in heads if lines[i].strip() == f"## {section}"), None)
    block = [f"## {section}", "", *body.strip().split("\n")]
    if idx is None:
        at = heads[0] if heads else len(lines)
        lines[at:at] = [*block, ""]
        return "\n".join(lines) + "\n"
    end = next((i for i in heads if i > idx), len(lines))
    lines[idx:end] = [*block, ""] if end < len(lines) else block
    return "\n".join(lines) + "\n"


def remove_bullet(text: str, section: str, bullet: str) -> str | None:
    """从某一节里删掉一条（逐字匹配）。找不到返回 None——落盘之后人可能改过措辞，那就不动。"""
    lines = text.rstrip("\n").split("\n")
    heads = [i for i, ln in enumerate(lines) if ln.startswith("## ")]
    idx = next((i for i in heads if lines[i].strip() == f"## {section}"), None)
    if idx is None:
        return None
    end = next((i for i in heads if i > idx), len(lines))
    for i in range(idx + 1, end):
        if lines[i].strip() == f"- {bullet}":
            del lines[i]
            return "\n".join(lines) + "\n"
    return None


@dataclass
class Candidate:
    id: str
    node: str
    kind: str
    text: str
    proposed_by: str                 # run_id
    scenario: str
    proposed_at: str
    last_activity_at: str
    votes: list[dict[str, Any]] = field(default_factory=list)

    def confirms(self) -> set[str]:
        return {v["run_id"] for v in self.votes if v["verdict"] == "confirm"}

    def refutes(self) -> set[str]:
        return {v["run_id"] for v in self.votes if v["verdict"] == "refute"}

    def confirm_scenarios(self) -> set[str]:
        return {v["scenario"] for v in self.votes if v["verdict"] == "confirm"}


class AtomMemory:
    def __init__(self, knowledge_dir: Path, *, enabled: bool = True,
                 confirms_append: int = 1, confirms_canonical: int = 2,
                 refutes_drop: int = 1, refutes_demote: int = 2, ttl_days: int = 30,
                 banned_words: Iterable[str] = ()):
        self.directory = knowledge_dir
        self.enabled = enabled
        self.confirms_append = confirms_append
        self.confirms_canonical = confirms_canonical
        self.refutes_drop = refutes_drop
        self.refutes_demote = refutes_demote
        self.ttl = timedelta(days=ttl_days)
        # 资产层不许出现运行时工具名（lint_atoms.py 会拦）。运行时最清楚自己叫什么，直接把
        # 自己的工具名当禁用词，不用再去解析技能里的 runtimes/*.md。
        self.banned = tuple(w for w in banned_words if w)

    # ---- 存储 -------------------------------------------------------------------
    @property
    def pending_path(self) -> Path:
        return self.directory / PENDING_FILE

    @property
    def ledger_path(self) -> Path:
        return self.directory / LEDGER_FILE

    def _load(self) -> list[Candidate]:
        if not self.pending_path.exists():
            return []
        out = []
        for line in self.pending_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(Candidate(**json.loads(line)))
        return out

    def _save(self, items: list[Candidate]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.pending_path.write_text(
            "".join(json.dumps(asdict(c), ensure_ascii=False) + "\n" for c in items), encoding="utf-8")

    def _log(self, event: str, **fields: Any) -> None:
        """账本：每条候选/落盘条目的来源和每一票。原子正文里不写这些（「不写日期不写故事」），
        正文是给模型的，账本是给人的。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": _now(), "event": event, **fields}, ensure_ascii=False) + "\n")

    def ledger(self) -> list[dict[str, Any]]:
        if not self.ledger_path.exists():
            return []
        return [json.loads(ln) for ln in self.ledger_path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def _created_by(self, node: str) -> str | None:
        """这个原子是哪次跑批建的。给「同一次跑批补自己刚建的原子」那条例外用。"""
        for row in reversed(self.ledger()):
            if row.get("event") == "created" and row.get("node") == node:
                return str(row.get("run_id"))
        return None

    def promoted_entries(self, node: str | None = None) -> list[dict[str, Any]]:
        """已落盘、还没被划掉的条目，带上它们收到的否决。给「已落盘的也能被推翻」用。"""
        rows = self.ledger()
        demoted = {r["id"] for r in rows if r.get("event") == "demoted"}
        refutes: dict[str, set[str]] = {}
        for r in rows:
            if r.get("event") == "refute_promoted":
                refutes.setdefault(r["id"], set()).add(str(r.get("run_id")))
        out = []
        for r in rows:
            if r.get("event") != "promoted" or r["id"] in demoted or (node and r.get("node") != node):
                continue
            out.append({**r, "refutes": sorted(refutes.get(r["id"], ()))})
        return out

    def recent(self, limit: int = 40) -> list[dict[str, Any]]:
        return self.ledger()[-limit:][::-1]

    def candidates(self) -> list[dict[str, Any]]:
        """给看板的：每条候选 + 票数 + 到哪一档。"""
        out = []
        for c in self.live():
            need = self.confirms_canonical if c.kind == KIND_CANONICAL else self.confirms_append
            ready = (len(c.confirms()) >= need
                     and (c.kind != KIND_CANONICAL or len(c.confirm_scenarios()) >= 2))
            out.append({**asdict(c), "confirms": len(c.confirms()), "refutes": len(c.refutes()),
                        "need": need, "ready": ready,
                        "section": KIND_SECTIONS.get(c.kind, "它长什么样")})
        return out

    # ---- 过期：读的时候现算，不是定时任务 -----------------------------------------
    def is_expired(self, c: Candidate, now: datetime | None = None) -> bool:
        if c.votes:                       # 有人验证过就不算「没人管」，票够不够是另一回事
            return False
        last = datetime.fromisoformat(c.last_activity_at)
        return (now or datetime.now()) - last > self.ttl

    def live(self) -> list[Candidate]:
        now = datetime.now()
        return [c for c in self._load() if not self.is_expired(c, now)]

    def sweep(self) -> int:
        """物理清掉过期候选。功能上它们早就「删了」（读的时候会被过滤），这一步只是让文件
        不无限长。挂在服务启动和每次收尾，不新开调度点。"""
        items = self._load()
        now = datetime.now()
        keep = []
        for c in items:
            if self.is_expired(c, now):
                self._log("expired", id=c.id, node=c.node, kind=c.kind)
            else:
                keep.append(c)
        if len(keep) != len(items):
            self._save(keep)
        return len(items) - len(keep)

    # ---- 校验 -------------------------------------------------------------------
    def _banned_hit(self, text: str) -> str | None:
        for tok in self.banned:
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(tok)}(?![A-Za-z0-9_])", text):
                return tok
        return None

    def _check_text(self, text: str, kind: str) -> str | None:
        t = text.strip()
        if not t:
            return "内容是空的"
        if len(t.splitlines()) > TEXT_MAX_LINES or len(t) > TEXT_MAX_CHARS:
            return f"一条只写一条教训：不超过 {TEXT_MAX_LINES} 行、{TEXT_MAX_CHARS} 字。写成故事了就拆开"
        if kind == "failure_mode" and "→" not in t:
            return "「已知会失败的方式」要写成「现象 → 机制 → 对策」，用 → 分开三段"
        if re.search(r"\b20\d{2}-\d{2}-\d{2}\b", t):
            return "不写日期——规则要脱离故事也成立，什么时候发生的交给 git log"
        if hit := self._banned_hit(t):
            return f"出现了运行时工具名 {hit!r}。原子是资产，换一套工具要照样能用；改成能力词（点、读页面文本、写表单值…）"
        return None

    @staticmethod
    def _atom_path(skill: Skill, node: str) -> Path:
        return skill.root / "atoms" / f"{node}.md"

    # ---- 模型能做的两件事 -----------------------------------------------------------
    def propose(self, skill: Skill, *, run_id: str, scenario: str, node: str, kind: str, text: str,
                description: str | None = None, looks_like: str | None = None) -> str:
        if not self.enabled:
            return CLOSED
        node = (node or "").strip()
        if not NODE_RE.match(node) or node in RESERVED_NODES:
            return f"节点名不合法: {node!r}。用小写字母、数字、连字符，像 outline-edit、account-menu"
        if kind not in KINDS:
            return f"kind 只能是 {list(KINDS)}，不是 {kind!r}"
        path = self._atom_path(skill, node)
        exists = path.is_file()
        creator = self._created_by(node) if exists else None

        # 全新的地方（或者这次跑批自己刚建的）：没人依赖它，直接写
        if not exists or creator == run_id:
            return self._write_direct(skill, path, exists, run_id=run_id, scenario=scenario,
                                      node=node, kind=kind, text=text,
                                      description=description, looks_like=looks_like)

        if kind == KIND_NEW:
            return (f"atoms/{node}.md 已经存在，不能再建。想补内容用 kind=variant 或 failure_mode——"
                    f"那会进建议区，等另一次跑批验证过才落盘")
        if kind == KIND_CANONICAL:
            problem = self._check_text(text, kind)
            if problem:
                return f"没提交：{problem}"
            return self._enqueue(run_id=run_id, scenario=scenario, node=node, kind=kind, text=text.strip(),
                                 note="改「它长什么样」这版不会自动落盘，只进建议区给人看")

        problem = self._check_text(text, kind)
        if problem:
            return f"没提交：{problem}"
        # 原子里已经有这条了？那这次踩坑没学到新东西
        section = KIND_SECTIONS[kind]
        for existing in _bullets(_section(path.read_text(encoding="utf-8"), section)):
            if _similar(existing, text):
                return f"atoms/{node}.md 的「{section}」里已经有这一条了：{existing[:60]}…。不用再提"
        return self._enqueue(run_id=run_id, scenario=scenario, node=node, kind=kind, text=text.strip())

    def _write_direct(self, skill: Skill, path: Path, exists: bool, *, run_id: str, scenario: str,
                      node: str, kind: str, text: str, description: str | None, looks_like: str | None) -> str:
        if not exists:
            if kind != KIND_NEW:
                return (f"这个地方还没有原子。先想清楚值不值得单独记一条；值得就用 kind=new，"
                        f"给 description（一句话，{DESC_MIN}～{DESC_MAX} 字：哪个应用的哪个节点、长什么样、什么时候来查）"
                        f"和 looks_like（它长什么样：锚点在哪、正常路径是什么）")
            d = (description or "").strip()
            n = _cjk_len(d)
            if n < DESC_MIN or n > DESC_MAX:
                return f"description 要 {DESC_MIN}～{DESC_MAX} 字，现在 {n} 字。它是索引行，只读这一句就该能决定要不要打开"
            ll = (looks_like or "").strip()
            if not ll:
                return "looks_like 不能空：失败者要靠「它长什么样」确认自己站在对的地方"
            for label, value in (("description", d), ("looks_like", ll), ("text", text or "")):
                if hit := self._banned_hit(value):
                    return f"{label} 里出现了运行时工具名 {hit!r}，改成能力词"
            first = None
            if (text or "").strip():
                problem = self._check_text(text, "failure_mode" if "→" in text else "variant")
                if problem:
                    return f"没提交：{problem}"
                first = ("已知会失败的方式" if "→" in text else "变体", " ".join(text.strip().split("\n")))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render_new_atom(node, d, ll, scenario, first), encoding="utf-8")
            self._log("created", node=node, run_id=run_id, scenario=scenario, path=str(path))
            return (f"已新建 atoms/{node}.md（这个地方之前没有原子，没人依赖它，直接落盘，不排队）。"
                    f"落的是工作区里一份未提交的改动，人审过才会进仓库")

        # 同一次跑批给自己刚建的原子补内容：依赖还没开始，照样直写
        if kind in (KIND_NEW, KIND_CANONICAL):
            return f"atoms/{node}.md 是这次跑批刚建的；再补内容用 kind=variant 或 failure_mode"
        problem = self._check_text(text, kind)
        if problem:
            return f"没提交：{problem}"
        section = KIND_SECTIONS[kind]
        bullet = " ".join(text.strip().split("\n"))
        path.write_text(append_bullet(path.read_text(encoding="utf-8"), section, bullet), encoding="utf-8")
        self._log("appended_direct", node=node, kind=kind, run_id=run_id, scenario=scenario, text=bullet)
        return f"已追加进 atoms/{node}.md 的「{section}」（这个原子是这次跑批刚建的，还没人依赖，直接写）"

    def _enqueue(self, *, run_id: str, scenario: str, node: str, kind: str, text: str, note: str = "") -> str:
        items = self._load()
        now = datetime.now()
        # 先合再建：同一节点同一类型已经有人提过差不多的 → 不新建，算你投了一票确认
        for c in items:
            if c.node == node and c.kind == kind and not self.is_expired(c, now) and _similar(c.text, text):
                if c.proposed_by == run_id:
                    return f"这次跑批已经提过这一条了（候选 {c.id}），不重复"
                if run_id in {v["run_id"] for v in c.votes}:
                    return f"候选 {c.id} 说的就是这件事，而且这次跑批已经投过票了"
                c.votes.append({"run_id": run_id, "scenario": scenario, "verdict": "confirm",
                                "why": "另一次跑批独立提出了同一条", "at": _now()})
                c.last_activity_at = _now()
                self._save(items)
                self._log("merged_as_vote", id=c.id, node=node, kind=kind, run_id=run_id, scenario=scenario)
                return (f"候选 {c.id} 已经说了同一件事（{c.scenario} 那次跑批提的）。两次独立跑批各自发现，"
                        f"这就是最强的证据——算成你投的一票确认，不另建")
        cid = hashlib.sha1(f"{node}|{kind}|{_norm(text)}".encode()).hexdigest()[:8]
        c = Candidate(id=cid, node=node, kind=kind, text=text, proposed_by=run_id, scenario=scenario,
                      proposed_at=_now(), last_activity_at=_now())
        items.append(c)
        self._save(items)
        self._log("proposed", id=cid, node=node, kind=kind, run_id=run_id, scenario=scenario, text=text)
        label = "改「它长什么样」" if kind == KIND_CANONICAL else f"追加到「{KIND_SECTIONS[kind]}」"
        tail = f"。{note}" if note else ""
        return (f"已进建议区（候选 {cid}，{label}）。atoms/{node}.md 有别的场景在依赖，不能直接改——"
                f"下一次跑批走到这里会读到它，按它试过、投票确认之后才落盘{tail}")

    def vote(self, *, run_id: str, scenario: str, candidate_id: str, verdict: str, why: str) -> str:
        if not self.enabled:
            return CLOSED
        if verdict not in ("confirm", "refute"):
            return f"verdict 只能是 confirm（按它试了，是真的）或 refute（按它试了，不对），不是 {verdict!r}"
        if not (why or "").strip():
            return "why 不能空：一句话说清你按它做了什么、看到了什么。这是账本上给人看的"
        items = self._load()
        c = next((x for x in items if x.id == candidate_id), None)
        if c is None or self.is_expired(c):
            # 不在建议区——可能是已经落盘的条目。落盘的也能被推翻，但门槛更高，而且是收尾时代码划
            entry = next((e for e in self.promoted_entries() if e["id"] == candidate_id), None)
            if entry is None:
                return f"没有候选 {candidate_id}（不存在、已经落盘并划掉，或者过期了）"
            if verdict == "confirm":
                return f"{candidate_id} 已经落盘在 atoms/{entry['node']}.md 里了，不用再确认"
            if run_id in entry["refutes"] or run_id == entry.get("proposed_by"):
                return f"这次跑批已经对 {candidate_id} 表过态了"
            self._log("refute_promoted", id=candidate_id, node=entry["node"], kind=entry["kind"],
                      run_id=run_id, scenario=scenario, why=why.strip())
            n = len(entry["refutes"]) + 1
            return (f"已记一票否决（已落盘条目 {candidate_id}：{n}/{self.refutes_demote}）。"
                    f"够 {self.refutes_demote} 个不同跑批说不对，收尾时代码会把它从原子里划掉")
        if c.proposed_by == run_id:
            return "不能给自己提的候选投票——「独立验证」的意思就是得换一次跑批"
        if run_id in {v["run_id"] for v in c.votes}:
            return f"这次跑批已经给候选 {candidate_id} 投过票了"
        c.votes.append({"run_id": run_id, "scenario": scenario, "verdict": verdict, "why": why.strip(), "at": _now()})
        c.last_activity_at = _now()
        self._save(items)
        self._log("voted", id=c.id, node=c.node, kind=c.kind, run_id=run_id, scenario=scenario,
                  verdict=verdict, why=why.strip())
        need = self.confirms_canonical if c.kind == KIND_CANONICAL else self.confirms_append
        return (f"已记一票 {verdict}（候选 {c.id}：确认 {len(c.confirms())}/{need}，否决 {len(c.refutes())}/{self.refutes_drop}）。"
                f"票够了这次跑批收尾时代码会落盘，不用你写")

    # ---- 代码干的：收尾数票 -----------------------------------------------------------
    def _promote(self, skill: Skill, c: Candidate, *, by: str = "votes") -> bool:
        """把一条候选追加进原子。原子在等票期间被删了/改名了就作废。"""
        path = self._atom_path(skill, c.node)
        if not path.is_file():
            self._log("dropped", id=c.id, node=c.node, kind=c.kind, reason="atom_missing")
            return False
        section = KIND_SECTIONS[c.kind]
        bullet = " ".join(c.text.strip().split("\n"))
        path.write_text(append_bullet(path.read_text(encoding="utf-8"), section, bullet), encoding="utf-8")
        self._log("promoted", id=c.id, node=c.node, kind=c.kind, text=bullet,
                  proposed_by=c.proposed_by, confirms=sorted(c.confirms()), by=by)
        return True

    def _demote(self, skill: Skill, entry: dict[str, Any]) -> bool:
        path = self._atom_path(skill, entry["node"])
        if not path.is_file():
            return False
        updated = remove_bullet(path.read_text(encoding="utf-8"), KIND_SECTIONS[entry["kind"]], entry["text"])
        if updated is None:            # 人改过措辞，逐字对不上——不动，留给人
            self._log("demote_skipped", id=entry["id"], node=entry["node"], reason="text_changed")
            return False
        path.write_text(updated, encoding="utf-8")
        self._log("demoted", id=entry["id"], node=entry["node"], kind=entry["kind"],
                  text=entry["text"], refutes=entry["refutes"])
        return True

    # ---- 人在看板上做的：一票顶阈值 -------------------------------------------------------
    def human_vote(self, skill: Skill, candidate_id: str, verdict: str, who: str = "human") -> str:
        """人看过就是终审：确认直接落盘（canonical 也在这儿落——这是它唯一的落盘路径），否决直接丢。"""
        if not self.enabled:
            return CLOSED
        items = self._load()
        c = next((x for x in items if x.id == candidate_id), None)
        if c is None:
            return f"没有候选 {candidate_id}"
        self._log("voted", id=c.id, node=c.node, kind=c.kind, run_id=who, scenario="", verdict=verdict, why="看板人工")
        rest = [x for x in items if x.id != candidate_id]
        if verdict == "refute":
            self._log("dropped", id=c.id, node=c.node, kind=c.kind, by=who)
            self._save(rest)
            return f"已丢弃候选 {c.id}"
        if c.kind == KIND_CANONICAL:
            path = self._atom_path(skill, c.node)
            if not path.is_file():
                return f"atoms/{c.node}.md 不存在了"
            path.write_text(replace_section(path.read_text(encoding="utf-8"), "它长什么样", c.text), encoding="utf-8")
            self._log("canonical_promoted", id=c.id, node=c.node, text=c.text, by=who)
            self._save(rest)
            return f"已把候选 {c.id} 写成 atoms/{c.node}.md 的「它长什么样」——是未提交的改动，记得看 diff"
        ok = self._promote(skill, c, by=who)
        self._save(rest)
        return f"已追加进 atoms/{c.node}.md" if ok else f"atoms/{c.node}.md 不存在了，候选已作废"

    def settle(self, skill: Skill) -> dict[str, list[str]]:
        """每次跑批收尾调一次。谁该落盘、谁该丢、谁过期、谁被推翻，都在这儿定——模型不参与。"""
        result: dict[str, list[str]] = {"promoted": [], "dropped": [], "expired": [], "kept": [], "demoted": []}
        if not self.enabled:
            return result
        for entry in self.promoted_entries():
            if len(entry["refutes"]) >= self.refutes_demote and self._demote(skill, entry):
                result["demoted"].append(entry["id"])
        items = self._load()
        now = datetime.now()
        keep: list[Candidate] = []
        for c in items:
            if self.is_expired(c, now):
                self._log("expired", id=c.id, node=c.node, kind=c.kind)
                result["expired"].append(c.id)
                continue
            if len(c.refutes()) >= self.refutes_drop:
                self._log("dropped", id=c.id, node=c.node, kind=c.kind, refutes=sorted(c.refutes()))
                result["dropped"].append(c.id)
                continue
            if c.kind == KIND_CANONICAL:
                # 票够了也只留在建议区，账本里标一下，等人来点——这版不自动改老做法
                if len(c.confirms()) >= self.confirms_canonical and len(c.confirm_scenarios()) >= 2:
                    self._log("canonical_ready", id=c.id, node=c.node, confirms=sorted(c.confirms()))
                keep.append(c)
                result["kept"].append(c.id)
                continue
            if len(c.confirms()) >= self.confirms_append:
                result["promoted" if self._promote(skill, c) else "dropped"].append(c.id)
                continue
            keep.append(c)
            result["kept"].append(c.id)
        if len(keep) != len(items):
            self._save(keep)
        return result

    # ---- 注入任务说明 -------------------------------------------------------------------
    def brief(self, refs: Iterable[str]) -> str:
        """给下一次跑批看的候选。本场景路书【参考】过的节点给全文（走到那儿就要用），
        其它节点只给一行索引——建议区会衰减，这段不会长。"""
        if not self.enabled:
            return ""
        wanted = {r for r in refs if r and r != "—"}
        live = self.live()
        now = datetime.now()
        # 最近落盘的也要列：落盘不等于永远对，按它做了发现不对就 refute，够数代码会划掉
        fresh = [e for e in self.promoted_entries() if e["node"] in wanted
                 and now - datetime.fromisoformat(e["at"]) <= self.ttl]
        if not live and not fresh:
            return "建议区现在是空的。"
        full = [c for c in live if c.node in wanted]
        rest = [c for c in live if c.node not in wanted]
        out: list[str] = []
        if full:
            out.append("这条路书引用的节点有这些候选（别人踩坑后提的，**还没被验证**）：")
            for c in full:
                age = (now - datetime.fromisoformat(c.proposed_at)).days
                label = {"variant": "变体", "failure_mode": "坑", KIND_CANONICAL: "改老做法"}[c.kind]
                votes = f"，已有 {len(c.confirms())} 票确认" if c.confirms() else ""
                out.append(f"- [{c.id}] {c.node} · {label}：{c.text.strip()}（{c.scenario} 提的，{age} 天前{votes}）")
        if rest:
            by_node: dict[str, int] = {}
            for c in rest:
                by_node[c.node] = by_node.get(c.node, 0) + 1
            index = "、".join(f"{n}({k})" for n, k in sorted(by_node.items()))
            out.append(f"其它节点还有候选：{index}。走到那些地方再问它们的全文。")
        if fresh:
            out.append("这些是最近才落盘的（已在原子里，按它做了发现不对就投 refute）：")
            out += [f"- [{e['id']}] {e['node']}：{e['text']}" for e in fresh]
        return "\n".join(out)

    def brief_for(self, scenario: Scenario) -> str:
        refs = [str(s.get("ref") or "").strip() for s in load_playbook(scenario).get("steps", [])]
        return self.brief(refs)
