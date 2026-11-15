"""应用知识图谱：把「页面 / 实体 / 动作」连成一张可查询的有向图。

边界（改这里之前先读完）：
- 图是**资产**，不是判据。它只做两件读取：给执行 agent 一条候选导航路径（plan），
  给清理台账一条「创建 ↔ 删除」的反向边（cleanup_for）。**独立复核方永远看不到它**——
  判分只凭真实现场与 expect.yaml，图谱提示不进裁判的任何输入。
- 图只在三个时刻被代码碰到：跑批前 plan、verdict 后登记清理、收尾时 apply_evidence。
  其余时间它是一份躺在技能目录里的 YAML，不参与任何推理。
- **本模块不写任何资产文件。** 置信度更新是纯函数，返回一张新图；落不落盘由人决定。
  跟 atoms/ 一个纪律：经验要变成工作区里一份能被 review 的 diff，不该被模型或后台偷改。

匹配刻意用「标题/别名的字面包含」，不上 embedding：这张图是给人写、给人审的，
出问题要能在 YAML 里一眼看出为什么命中。语义检索是以后的事，不是这一版。
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

GRAPH_FILE = "graph/app.yaml"       # 相对技能根目录
NODE_KINDS = frozenset({"page", "entity", "action"})
ID_RE = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
NORM_RE = re.compile(r"[\s`「」『』“”\"'，。,.：:；;！!？?（）()→\-—·/]")

NODE_FIELDS = frozenset({
    "id", "kind", "title", "anchors", "aliases", "arrival", "atoms", "entry",
    "fact_key", "create_via", "cleanup_via", "cleanup_recipe", "cleanup_args",
})
EDGE_FIELDS = frozenset({"from", "to", "action", "anchor", "confidence", "evidence"})
EVIDENCE_STEP = 0.05               # 一次验证对一条边的置信度调整幅度


def _norm(text: str) -> str:
    """归一化后再做字面包含匹配：去标点、空白，转小写。"""
    return NORM_RE.sub("", text).lower()


def _str_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return ()


@dataclass(frozen=True)
class GraphNode:
    id: str
    kind: str
    title: str = ""
    anchors: tuple[str, ...] = ()      # 页面上认得到的文字，给 resolve 兜底
    aliases: tuple[str, ...] = ()      # 目标里可能出现的说法，给 plan 匹配
    arrival: str = ""                  # 到位标志（人读的，不参与判分）
    atoms: tuple[str, ...] = ()        # 关联 atoms/*.md
    entry: bool = False                # 入口页面，plan 的默认起点
    fact_key: str = ""                 # context.yaml 里的键
    create_via: str = ""               # 创建这个实体的动作节点 id
    cleanup_via: str = ""              # 删除这个实体的动作节点 id（反向边的语义）
    cleanup_recipe: str = ""           # recipes/*.yaml 里的配方名（反向边的执行）
    cleanup_args: tuple[str, ...] = () # 形如 ("context.project_id",)

    def to_dict(self) -> dict[str, Any]:
        """按 YAML 字段名序列化，给看板画图用：人对着 app.yaml 能逐字核上。

        只吐非空字段——空值写出来只会让看板多出一堆没意义的「—」。
        """
        data: dict[str, Any] = {"id": self.id, "kind": self.kind}
        for name in ("title", "arrival", "fact_key", "create_via", "cleanup_via", "cleanup_recipe"):
            value = getattr(self, name)
            if value:
                data[name] = value
        for name in ("anchors", "aliases", "atoms", "cleanup_args"):
            value = getattr(self, name)
            if value:
                data[name] = list(value)
        if self.entry:
            data["entry"] = True
        return data


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    action: str = ""
    anchor: str = ""
    confidence: float = 1.0
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"from": self.source, "to": self.target,
                                "confidence": self.confidence}
        if self.action:
            data["action"] = self.action
        if self.anchor:
            data["anchor"] = self.anchor
        if self.evidence:
            data["evidence"] = self.evidence
        return data


@dataclass(frozen=True)
class GraphStep:
    """路径上的一步：从哪来、做什么、到哪去。只用于渲染提示。"""
    source: GraphNode
    target: GraphNode
    action: str
    anchor: str
    confidence: float


@dataclass(frozen=True)
class GraphPlan:
    goal_node: GraphNode
    steps: tuple[GraphStep, ...]

    @property
    def found(self) -> bool:
        return bool(self.steps)

    def render(self) -> str:
        """给 runner 的任务说明。措辞要钉死「仅供参考」——别让模型把它当判据。"""
        head = [f"目标节点：{self.goal_node.id}（{self.goal_node.title or '未命名'}）"]
        for i, s in enumerate(self.steps, 1):
            anchor = f"，锚点「{s.anchor}」" if s.anchor else ""
            head.append(f"{i}. {s.source.title or s.source.id} → "
                        f"{s.target.title or s.target.id}：{s.action or '（未写动作）'}{anchor}")
        return "\n".join(head)


@dataclass(frozen=True)
class CleanupHint:
    entity: GraphNode
    via: str                          # cleanup_via 指向的动作节点
    recipe: str                       # 配方名，交给 CleanupManager 的目录解析
    args: tuple[str, ...]             # 参数引用，交给 resolve_args 从 facts 取实值


class AppGraph:
    """只读的有向图。构造时建好索引；所有查询都是纯读，更新走 apply_evidence 返回新图。"""

    def __init__(self, app: str, nodes: tuple[GraphNode, ...],
                 edges: tuple[GraphEdge, ...], warnings: tuple[str, ...] = ()):
        self.app = app
        self.nodes = nodes
        self.edges = edges
        self.warnings = warnings
        self._by_id: dict[str, GraphNode] = {n.id: n for n in nodes}
        self._out: dict[str, list[GraphEdge]] = {}
        for e in edges:
            self._out.setdefault(e.source, []).append(e)

    def __bool__(self) -> bool:
        return bool(self.nodes)

    def __len__(self) -> int:
        return len(self.nodes)

    @property
    def is_valid(self) -> bool:
        return not self.warnings

    def node(self, node_id: str) -> GraphNode | None:
        return self._by_id.get(node_id)

    # ---- ① 跑批前：把目标连成一条候选路径 ---------------------------------------
    def match_goal(self, goal: str) -> tuple[GraphNode, ...]:
        """标题/别名出现在目标描述里就算命中，按**命中的最长标签**排序返回。

        排序是必要的：「任务」这种两字标题几乎会出现在任何描述里，不该压过
        「看统计」这种更具体的说法。命不中返回空——不瞎猜一个节点。
        """
        text = _norm(goal)
        if not text:
            return ()
        scored: list[tuple[int, int, GraphNode]] = []
        for i, n in enumerate(self.nodes):
            hits: list[int] = []
            for label in (n.title, *n.aliases, *n.anchors):
                norm = _norm(label)
                if norm and norm in text:
                    hits.append(len(norm))
            if hits:
                scored.append((max(hits), -i, n))   # 同长度时靠前的节点优先
        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return tuple(n for _, _, n in scored)

    def plan(self, goal: str, *, start: str | None = None) -> GraphPlan | None:
        """从入口到「最具体那个目标」的最短路径（BFS，边数最少）。

        按匹配质量逐个试目标：第一个可达的赢，且它的路径一定是最短的。
        提示而已：每一跳都要靠本次页面上的锚点重新确认，图说了不算。
        """
        targets = self.match_goal(goal)
        if not targets:
            return None
        start_id = start or next((n.id for n in self.nodes if n.entry),
                                 next((n.id for n in self.nodes if n.kind == "page"), ""))
        if not start_id or self.node(start_id) is None:
            return None
        for target in targets:
            if target.id == start_id:
                return GraphPlan(goal_node=target, steps=())
            path = self._shortest_path(start_id, target.id)
            if path is not None:
                return path
        return None

    def _shortest_path(self, start_id: str, target_id: str) -> GraphPlan | None:
        # BFS：记「到这个节点走的是哪条边」，第一个撞上目标就是最短路径
        queue: deque[str] = deque([start_id])
        prev: dict[str, GraphEdge] = {}
        seen = {start_id}
        while queue:
            cur = queue.popleft()
            for edge in self._out.get(cur, ()):
                if edge.target in seen or self.node(edge.target) is None:
                    continue
                seen.add(edge.target)
                prev[edge.target] = edge
                if edge.target == target_id:
                    return self._plan_from_prev(edge.target, start_id, prev)
                queue.append(edge.target)
        return None

    def _plan_from_prev(self, end: str, start: str,
                        prev: dict[str, GraphEdge]) -> GraphPlan:
        chain: list[GraphEdge] = []
        cur = end
        while cur != start:
            edge = prev[cur]
            chain.append(edge)
            cur = edge.source
        chain.reverse()
        steps = tuple(
            GraphStep(source=self._by_id[e.source], target=self._by_id[e.target],
                      action=e.action, anchor=e.anchor, confidence=e.confidence)
            for e in chain
        )
        return GraphPlan(goal_node=self._by_id[end], steps=steps)

    def briefing(self, goal: str, *, start: str | None = None) -> str:
        """接进 runner 的任务说明。图是空的、目标没命中，都返回空串（整段不出现）。

        目标恰好落在入口节点时也要给东西：真实场景的 intent 往往就写「任务列表」，
        这时没有跨页路径可给，但「这个页面认得到哪些锚点、从它出发做过什么」同样有用。
        早期版本在这条分支返回空串，等于对最常见的场景完全没接上。
        """
        plan = self.plan(goal, start=start)
        if plan is None:
            return ""
        if plan.found:
            body = plan.render()
        else:
            node = plan.goal_node
            anchors = "、".join(node.anchors) or "（未登记）"
            acts = [e for e in self._out.get(node.id, ())]
            actions = "；".join(f"{e.action or '未写动作'} → {e.target}" for e in acts) or "（无）"
            body = (f"目标就在入口节点 {node.id}（{node.title or '未命名'}），不需要跨页导航。\n"
                    f"这个节点上已知锚点：{anchors}\n"
                    f"从它出发记过的动作：{actions}")
        return ("## 已知导航路径（来自应用知识图谱，仅供导航参考，不是判据）\n"
                + body
                + "\n\n这里只说明「历史上怎么走到过」，本次仍以页面上实际看到的锚点为准；"
                  "走到某一步对不上就按现场重新勘路，不要照着提示硬走。")

    # ---- ② 登记清理：反向边 -------------------------------------------------------
    def cleanup_for(self, entity_id: str) -> CleanupHint | None:
        entity = self.node(entity_id)
        if entity is None or entity.kind != "entity" or not entity.cleanup_recipe:
            return None
        return CleanupHint(entity=entity, via=entity.cleanup_via,
                           recipe=entity.cleanup_recipe, args=entity.cleanup_args)

    def cleanup_entries_for_facts(self, facts: dict[str, Any]) -> list[dict[str, Any]]:
        """本次跑批创建了哪些实体 → 该登记哪些清理任务。

        只认 facts 里真实存在、且非空的值：没有 project_id 就不该编一个清理任务出来。
        返回的是给 ``CleanupManager._resolve_recipes`` 的条目（recipe + args），
        配方到脚本的解析仍由目录负责——图不碰执行细节。
        """
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, tuple[str, ...]]] = set()
        for n in self.nodes:
            if n.kind != "entity" or not n.fact_key or not n.cleanup_recipe:
                continue
            value = facts.get(n.fact_key)
            if value is None or not str(value).strip():
                continue
            hint = self.cleanup_for(n.id)
            if hint is None:
                continue
            key = (hint.recipe, hint.args)
            if key in seen:
                continue
            seen.add(key)
            out.append({"recipe": hint.recipe, "args": list(hint.args)})
        return out

    # ---- UI 漂移：锚点找不到了，给个等价候选 ---------------------------------------
    def resolve(self, anchor: str) -> tuple[GraphNode, ...]:
        """按锚点/别名找节点。返回的是「可能在这儿」，不是「一定在这儿」，由人批。"""
        needle = _norm(anchor)
        if not needle:
            return ()
        return tuple(
            n for n in self.nodes
            if any(needle in _norm(label) or _norm(label) in needle
                   for label in (*n.anchors, *n.aliases, n.title) if _norm(label))
        )

    # ---- ③ 收尾：置信度（纯函数，不落盘） -----------------------------------------
    def apply_evidence(self, edge_index: int, ok: bool,
                       *, step: float = EVIDENCE_STEP) -> AppGraph:
        """一次跑批验证过某条边 → 返回一张调整了置信度的新图，原图不动。

        刻意不写 YAML：回写会冲掉注释和排版，而这份文件是要给人审的。
        真要落地，由人在 diff 里确认后手动改，跟 atoms/ 一个规矩。
        """
        if not 0 <= edge_index < len(self.edges):
            return self
        old = self.edges[edge_index]
        delta = step if ok else -step
        new_conf = max(0.0, min(1.0, round(old.confidence + delta, 2)))
        if new_conf == old.confidence:
            return self
        edges = list(self.edges)
        edges[edge_index] = replace(old, confidence=new_conf)
        return AppGraph(self.app, self.nodes, tuple(edges), self.warnings)


def _parse_node(raw: Any, warnings: list[str]) -> GraphNode | None:
    if not isinstance(raw, dict):
        warnings.append(f"nodes 里有不是字典的条目：{raw!r}")
        return None
    unknown = set(raw) - NODE_FIELDS
    if unknown:
        warnings.append(f"节点 {raw.get('id', '?')} 有未知字段：{'、'.join(sorted(unknown))}")
    node_id = str(raw.get("id") or "").strip()
    if not ID_RE.match(node_id):
        warnings.append(f"节点 id 不合法：{node_id!r}（小写字母开头，只许 . _ -）")
        return None
    kind = str(raw.get("kind") or "").strip()
    if kind not in NODE_KINDS:
        warnings.append(f"节点 {node_id} 的 kind 只能是 {sorted(NODE_KINDS)}，不是 {kind!r}")
        return None
    try:
        entry = bool(raw.get("entry") or False)
    except (TypeError, ValueError):
        entry = False
    return GraphNode(
        id=node_id, kind=kind, title=str(raw.get("title") or "").strip(),
        anchors=_str_list(raw.get("anchors")), aliases=_str_list(raw.get("aliases")),
        arrival=str(raw.get("arrival") or "").strip(), atoms=_str_list(raw.get("atoms")),
        entry=entry, fact_key=str(raw.get("fact_key") or "").strip(),
        create_via=str(raw.get("create_via") or "").strip(),
        cleanup_via=str(raw.get("cleanup_via") or "").strip(),
        cleanup_recipe=str(raw.get("cleanup_recipe") or "").strip(),
        cleanup_args=_str_list(raw.get("cleanup_args")),
    )


def _parse_edge(raw: Any, warnings: list[str]) -> GraphEdge | None:
    if not isinstance(raw, dict):
        warnings.append(f"edges 里有不是字典的条目：{raw!r}")
        return None
    unknown = set(raw) - EDGE_FIELDS
    if unknown:
        warnings.append(f"边 {raw.get('from', '?')}→{raw.get('to', '?')} 有未知字段："
                        f"{'、'.join(sorted(unknown))}")
    source, target = str(raw.get("from") or "").strip(), str(raw.get("to") or "").strip()
    if not source or not target:
        warnings.append("边缺 from 或 to")
        return None
    try:
        confidence = float(raw.get("confidence", 1.0))
    except (TypeError, ValueError):
        warnings.append(f"边 {source}→{target} 的 confidence 不是数字，按 1.0 处理")
        confidence = 1.0
    if not 0.0 <= confidence <= 1.0:
        warnings.append(f"边 {source}→{target} 的 confidence 越界，已夹到 0~1")
        confidence = max(0.0, min(1.0, confidence))
    return GraphEdge(source=source, target=target,
                     action=str(raw.get("action") or "").strip(),
                     anchor=str(raw.get("anchor") or "").strip(),
                     confidence=confidence, evidence=str(raw.get("evidence") or "").strip())


def parse_graph(data: Any) -> AppGraph:
    """解析已经 load 过的 YAML 对象，顺手做全部校验。**不抛异常**：坏的地方记进 warnings，
    能用的部分照常留着——渐进式建图时，一条写错的边不该让整张图作废。"""
    warnings: list[str] = []
    if not isinstance(data, dict):
        return AppGraph("", (), (), ("图谱根节点必须是字典",))
    # 刻意不用 `or []`：nodes: {} 是写错的类型，不能被静默当成「没写节点」。
    raw_nodes = data.get("nodes")
    raw_edges = data.get("edges")
    raw_nodes = [] if raw_nodes is None else raw_nodes
    raw_edges = [] if raw_edges is None else raw_edges
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        return AppGraph(str(data.get("app") or ""), (), (),
                        ("nodes / edges 必须是列表",))

    nodes: list[GraphNode] = []
    seen: set[str] = set()
    for raw in raw_nodes:
        node = _parse_node(raw, warnings)
        if node is None:
            continue
        if node.id in seen:
            warnings.append(f"节点 id 重复：{node.id}（后者已忽略）")
            continue
        seen.add(node.id)
        nodes.append(node)

    edges: list[GraphEdge] = []
    for raw in raw_edges:
        edge = _parse_edge(raw, warnings)
        if edge is None:
            continue
        for endpoint in (edge.source, edge.target):
            if endpoint not in seen:
                warnings.append(f"边 {edge.source}→{edge.target} 引用了不存在的节点 {endpoint}")
        if edge.source not in seen or edge.target not in seen:
            continue
        edges.append(edge)

    for n in nodes:
        for field, value in (("create_via", n.create_via), ("cleanup_via", n.cleanup_via)):
            if value and value not in seen:
                warnings.append(f"节点 {n.id} 的 {field} 指向不存在的动作节点 {value}")
        if n.cleanup_recipe and not n.cleanup_via:
            warnings.append(f"节点 {n.id} 写了 cleanup_recipe 但没写 cleanup_via（反向边不完整）")

    return AppGraph(str(data.get("app") or ""), tuple(nodes), tuple(edges), tuple(warnings))


def load_graph(path: Path) -> AppGraph:
    """读一份图谱文件。文件不存在 / YAML 坏 / 编码错 → 空图，warnings 里说明原因。

    没有图谱是正常状态（先跑核心流程，图以后再补），所以这里刻意不抛异常：
    跑批不该因为一份可选资产起不来。
    """
    path = Path(path)
    if not path.is_file():
        return AppGraph("", (), (), (f"图谱文件不存在：{path}",))
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as e:
        return AppGraph("", (), (), (f"图谱读取失败 {path.name}：{type(e).__name__}: {e}",))
    return parse_graph(data)


def load_skill_graph(skill_root: Path) -> AppGraph:
    """按约定路径读技能里的图谱：``<skill>/graph/app.yaml``。"""
    return load_graph(Path(skill_root) / GRAPH_FILE)
