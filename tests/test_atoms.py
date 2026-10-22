"""原子记忆层：模型只能提议和投票，代码数票落盘。

锁四条：阈值（几票落、几票丢、多久过期）、独立性（不能给自己投票）、先合再建（相似的合成一票）、
全新的地方直接写。外加两道墙：裁判拿不到这两个工具；记忆层关着时整段提示词消失。
"""
from __future__ import annotations

import json
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from runtime.atoms import AtomMemory, append_bullet, remove_bullet, replace_section
from runtime.config import PROJECT_ROOT, Settings
from runtime.prompts import PromptLibrary, TaskPrompts
from runtime.subagents import SubAgentLoader
from tools import REGISTRY, ToolContext
from tools.propose_atom import NO_MEMORY

EXISTING = textwrap.dedent("""\
    ---
    name: outline-edit
    description: 研究者端「访谈大纲」面板的编辑态：编辑进入、保存退出，题目行有齿轮弹层。下拉点不中、弹层没反应时来查。
    ---

    # outline-edit

    ## 它长什么样

    面板标题栏「编辑大纲」→ 编辑态：同一按钮文案变「保存」。

    ## 变体

    - 选项文案能直接写表单值，不必逐字打。

    ## 已知会失败的方式

    - 下拉点了选项永远不提交 → 视口矮时下拉向上弹被遮住 → 把视口调高让它向下弹

    ## 出处

    - `scenarios/choice-question/playbook.md`
    """)

DESC = "研究者端右上角头像菜单里的「账号管理」弹层：唯一能看到登录邮箱的地方，菜单常要点两下才开。"
LOOKS = "右上角圆形头像 → 菜单中部「账号管理」→ 弹层「登录方式」区显示邮箱。"
LESSON = "菜单第一下只高亮不展开 → 菜单在两次操作之间会自己关掉 → 点头像后单独一次操作点「账号管理」"


@pytest.fixture
def mem(tmp_path: Path) -> AtomMemory:
    return AtomMemory(tmp_path / "knowledge", banned_words=("navigate", "click"))


@pytest.fixture
def atoms_dir(skill) -> Path:
    d = skill.root / "atoms"
    d.mkdir()
    (d / "outline-edit.md").write_text(EXISTING, encoding="utf-8")
    return d


def _propose(mem, skill, run="run-A", scenario="login", **kw):
    return mem.propose(skill, run_id=run, scenario=scenario, **kw)


# ---- 全新的地方：直接写 ---------------------------------------------------------------
def test_new_node_is_written_directly_without_votes(mem, skill, atoms_dir):
    out = _propose(mem, skill, node="account-menu", kind="new", text=LESSON, description=DESC, looks_like=LOOKS)
    assert "已新建" in out
    body = (atoms_dir / "account-menu.md").read_text(encoding="utf-8")
    assert body.startswith("---\nname: account-menu\ndescription: ")
    assert "## 它长什么样" in body and LOOKS in body
    assert "## 已知会失败的方式" in body and "- " + LESSON in body   # 带 → 的第一条进失败方式
    assert "scenarios/login/playbook.md" in body
    assert not mem.pending_path.exists()                               # 没进建议区
    assert [r["event"] for r in mem.ledger()] == ["created"]


def test_new_requires_description_of_the_right_length(mem, skill, atoms_dir):
    out = _propose(mem, skill, node="account-menu", kind="new", text="", description="太短", looks_like=LOOKS)
    assert "description" in out and not (atoms_dir / "account-menu.md").exists()


def test_new_is_refused_when_atom_already_exists(mem, skill, atoms_dir):
    out = _propose(mem, skill, node="outline-edit", kind="new", text="x", description=DESC, looks_like=LOOKS)
    assert "已经存在" in out
    assert (atoms_dir / "outline-edit.md").read_text(encoding="utf-8") == EXISTING


def test_append_kind_on_missing_atom_points_to_new(mem, skill, atoms_dir):
    out = _propose(mem, skill, node="nowhere", kind="variant", text="别的入口")
    assert "kind=new" in out and not (atoms_dir / "nowhere.md").exists()


def test_creator_run_keeps_direct_write_for_its_own_new_atom(mem, skill, atoms_dir):
    """同一次跑批给自己刚建的原子补内容：依赖从别的跑批读到它才开始，还是直写。"""
    _propose(mem, skill, node="account-menu", kind="new", text="", description=DESC, looks_like=LOOKS)
    out = _propose(mem, skill, node="account-menu", kind="variant", text="窄屏下头像收进汉堡菜单里")
    assert "直接写" in out
    assert "- 窄屏下头像收进汉堡菜单里" in (atoms_dir / "account-menu.md").read_text(encoding="utf-8")
    # 换一次跑批就不行了——它已经可能被读过
    out = _propose(mem, skill, run="run-B", node="account-menu", kind="variant", text="prod 上头像位置更靠右")
    assert "建议区" in out


# ---- 已有原子：只能进建议区 -------------------------------------------------------------
def test_append_on_existing_atom_goes_to_pending_not_file(mem, skill, atoms_dir):
    out = _propose(mem, skill, node="outline-edit", kind="failure_mode", text=LESSON)
    assert "建议区" in out
    assert (atoms_dir / "outline-edit.md").read_text(encoding="utf-8") == EXISTING
    pending = mem.live()
    assert len(pending) == 1 and pending[0].node == "outline-edit" and pending[0].votes == []


def test_failure_mode_must_be_phenomenon_mechanism_remedy(mem, skill, atoms_dir):
    out = _propose(mem, skill, node="outline-edit", kind="failure_mode", text="点了没反应，多等一会就好")
    assert "→" in out and not mem.live()


def test_runtime_tool_names_are_refused(mem, skill, atoms_dir):
    out = _propose(mem, skill, node="outline-edit", kind="variant", text="用 navigate 直接跳到编辑页也行")
    assert "工具名" in out and not mem.live()


def test_lesson_already_in_atom_is_not_proposed_again(mem, skill, atoms_dir):
    out = _propose(mem, skill, node="outline-edit", kind="variant", text="选项文案可以直接写表单值，不用逐字打")
    assert "已经有这一条" in out and not mem.live()


# ---- 投票与数票 -------------------------------------------------------------------------
def test_cannot_vote_on_own_proposal(mem, skill, atoms_dir):
    _propose(mem, skill, node="outline-edit", kind="failure_mode", text=LESSON)
    cid = mem.live()[0].id
    out = mem.vote(run_id="run-A", scenario="login", candidate_id=cid, verdict="confirm", why="试了")
    assert "自己" in out and mem.live()[0].votes == []


def test_one_confirm_from_another_run_promotes_on_settle(mem, skill, atoms_dir):
    _propose(mem, skill, node="outline-edit", kind="failure_mode", text=LESSON)
    cid = mem.live()[0].id
    assert "已记一票" in mem.vote(run_id="run-B", scenario="signup", candidate_id=cid,
                                verdict="confirm", why="按它点了两下，弹层出来了")
    # 投票本身不落盘——落盘是收尾时代码干的
    assert LESSON not in (atoms_dir / "outline-edit.md").read_text(encoding="utf-8")
    result = mem.settle(skill)
    assert result["promoted"] == [cid] and not mem.live()
    body = (atoms_dir / "outline-edit.md").read_text(encoding="utf-8")
    fails = body.split("## 已知会失败的方式")[1].split("## 出处")[0]
    assert "- " + LESSON in fails
    assert body.startswith(EXISTING.split("## 已知会失败的方式")[0])   # 前面一个字节没动
    assert [r["event"] for r in mem.ledger()] == ["proposed", "voted", "promoted"]


def test_one_refute_drops(mem, skill, atoms_dir):
    _propose(mem, skill, node="outline-edit", kind="variant", text="从项目列表页右键也能进编辑态")
    cid = mem.live()[0].id
    mem.vote(run_id="run-B", scenario="login", candidate_id=cid, verdict="refute", why="右键没有菜单")
    result = mem.settle(skill)
    assert result["dropped"] == [cid] and not mem.live()
    assert (atoms_dir / "outline-edit.md").read_text(encoding="utf-8") == EXISTING


def test_duplicate_vote_from_same_run_is_refused(mem, skill, atoms_dir):
    _propose(mem, skill, node="outline-edit", kind="variant", text="从项目列表页右键也能进编辑态")
    cid = mem.live()[0].id
    mem.vote(run_id="run-B", scenario="login", candidate_id=cid, verdict="confirm", why="试了")
    out = mem.vote(run_id="run-B", scenario="login", candidate_id=cid, verdict="refute", why="又试了")
    assert "已经给" in out and len(mem.live()[0].votes) == 1


def test_similar_proposal_from_another_run_counts_as_a_confirm(mem, skill, atoms_dir):
    """先合再建：两次独立跑批各自发现同一件事，就是最强的证据——合成一票，不另建。"""
    _propose(mem, skill, node="outline-edit", kind="failure_mode", text=LESSON)
    out = _propose(mem, skill, run="run-B", scenario="signup", node="outline-edit", kind="failure_mode",
                   text="菜单第一下只高亮、不展开 → 菜单会在两次操作之间自己关掉 → 点完头像单独一次操作再点「账号管理」")
    assert "算成你投的一票确认" in out
    live = mem.live()
    assert len(live) == 1 and live[0].confirms() == {"run-B"}
    assert mem.settle(skill)["promoted"] == [live[0].id]


def test_similar_proposal_from_same_run_is_a_noop(mem, skill, atoms_dir):
    _propose(mem, skill, node="outline-edit", kind="failure_mode", text=LESSON)
    out = _propose(mem, skill, node="outline-edit", kind="failure_mode", text=LESSON + "。")
    assert "不重复" in out and len(mem.live()) == 1 and mem.live()[0].votes == []


def test_canonical_change_is_accepted_but_never_auto_written(mem, skill, atoms_dir):
    _propose(mem, skill, node="outline-edit", kind="canonical", text="编辑态入口改到了右上角的铅笔图标")
    cid = mem.live()[0].id
    mem.vote(run_id="run-B", scenario="signup", candidate_id=cid, verdict="confirm", why="看到了")
    mem.vote(run_id="run-C", scenario="choice-question", candidate_id=cid, verdict="confirm", why="看到了")
    result = mem.settle(skill)
    assert result["kept"] == [cid] and result["promoted"] == []
    assert (atoms_dir / "outline-edit.md").read_text(encoding="utf-8") == EXISTING
    assert "canonical_ready" in [r["event"] for r in mem.ledger()]


# ---- 过期：读的时候现算 -------------------------------------------------------------------
def _age(mem: AtomMemory, days: int) -> None:
    rows = [json.loads(ln) for ln in mem.pending_path.read_text(encoding="utf-8").splitlines()]
    old = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    for r in rows:
        r["proposed_at"] = r["last_activity_at"] = old
    mem.pending_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def test_unverified_candidate_expires_after_ttl(mem, skill, atoms_dir):
    _propose(mem, skill, node="outline-edit", kind="variant", text="从项目列表页右键也能进编辑态")
    _age(mem, 31)
    assert mem.live() == []                                   # 读的时候就当它不存在
    assert mem.pending_path.read_text(encoding="utf-8").strip()   # 文件里还躺着
    assert mem.sweep() == 1                                   # 顺手清掉
    assert mem.pending_path.read_text(encoding="utf-8") == ""
    assert [r["event"] for r in mem.ledger()][-1] == "expired"


def test_candidate_with_a_vote_does_not_expire(mem, skill, atoms_dir):
    _propose(mem, skill, node="outline-edit", kind="variant", text="从项目列表页右键也能进编辑态")
    cid = mem.live()[0].id
    mem.vote(run_id="run-B", scenario="login", candidate_id=cid, verdict="confirm", why="试了")
    _age(mem, 90)
    assert len(mem.live()) == 1                               # 有人验证过就不算「没人管」


# ---- 注入任务说明 ---------------------------------------------------------------------------
def test_brief_gives_full_text_for_referenced_nodes_and_an_index_for_the_rest(mem, skill, atoms_dir):
    (atoms_dir / "composer-input.md").write_text(EXISTING.replace("outline-edit", "composer-input"), encoding="utf-8")
    _propose(mem, skill, node="outline-edit", kind="failure_mode", text=LESSON)
    _propose(mem, skill, run="run-B", node="composer-input", kind="variant", text="回车也能发送")
    text = mem.brief(["outline-edit", "—"])
    assert LESSON in text and "[" in text                      # 引用到的节点：全文 + 可投票的 id
    assert "回车也能发送" not in text and "composer-input(1)" in text   # 没引用的：只给索引
    assert mem.brief([]) .startswith("其它节点还有候选")
    assert AtomMemory(mem.directory).brief([]) != ""            # 没候选也有一句话，工具说明照样注入


def test_brief_hides_expired(mem, skill, atoms_dir):
    _propose(mem, skill, node="outline-edit", kind="variant", text="从项目列表页右键也能进编辑态")
    _age(mem, 40)
    assert mem.brief(["outline-edit"]) == "建议区现在是空的。"


def test_prompt_carries_the_atom_block_only_when_memory_is_on(project_settings, skill):
    lib = PromptLibrary(project_settings.prompts_dir)
    on = TaskPrompts(lib, memory_brief=lambda _s: "建议区现在是空的。").for_scenario(skill.scenario("with-playbook"))
    assert "propose_atom" in on and "vote_atom" in on and "建议区现在是空的" in on
    off = TaskPrompts(lib).for_scenario(skill.scenario("with-playbook"))
    assert "propose_atom" not in off                             # 关着就整段不出现，模型不会去找不存在的工具


# ---- 开关与墙 ------------------------------------------------------------------------------
def test_disabled_memory_refuses_everything_and_touches_nothing(tmp_path, skill, atoms_dir):
    mem = AtomMemory(tmp_path / "knowledge", enabled=False)
    assert "已关闭" in _propose(mem, skill, node="account-menu", kind="new", text="", description=DESC, looks_like=LOOKS)
    assert "已关闭" in mem.vote(run_id="r", scenario="s", candidate_id="x", verdict="confirm", why="y")
    assert mem.brief(["outline-edit"]) == ""
    assert not (atoms_dir / "account-menu.md").exists() and not (tmp_path / "knowledge").exists()


def test_tools_dispatch_through_registry_and_refuse_without_memory(mem, skill, atoms_dir):
    ctx = ToolContext(skill=skill, scenario="login", run_id="run-A", memory=mem)
    out = REGISTRY.dispatch(ctx, "propose_atom", {"node": "outline-edit", "kind": "failure_mode", "text": LESSON})
    assert "建议区" in out
    cid = mem.live()[0].id
    ctx_b = ToolContext(skill=skill, scenario="signup", run_id="run-B", memory=mem)
    assert "已记一票" in REGISTRY.dispatch(ctx_b, "vote_atom", {"candidate": cid, "verdict": "confirm", "why": "试了"})
    bare = ToolContext(skill=skill, scenario="login")
    assert REGISTRY.dispatch(bare, "propose_atom", {"node": "x", "kind": "new", "text": ""}) == NO_MEMORY
    assert REGISTRY.dispatch(bare, "vote_atom", {"candidate": cid, "verdict": "confirm", "why": "y"}) == NO_MEMORY


def test_judge_and_only_the_runner_have_the_memory_tools(project_settings):
    loader = SubAgentLoader(project_settings)
    assert {"propose_atom", "vote_atom"} <= set(loader.load("agent").tools)
    assert not {"propose_atom", "vote_atom"} & set(loader.load("judge").tools)   # 数据流墙：裁判不知道别人提了什么


def test_settings_defaults():
    s = Settings.from_env(PROJECT_ROOT)
    assert s.atom_memory is True
    assert (s.atom_confirms_append, s.atom_confirms_canonical) == (1, 2)
    assert (s.atom_refutes_drop, s.atom_refutes_demote, s.atom_pending_ttl_days) == (1, 2, 30)


# ---- 落盘只插一行 -----------------------------------------------------------------------------
def test_append_bullet_adds_one_line_and_leaves_the_rest_byte_identical():
    out = append_bullet(EXISTING, "变体", "新的一条")
    assert out.count("\n") == EXISTING.count("\n") + 1
    assert "- 选项文案能直接写表单值，不必逐字打。\n- 新的一条\n\n## 已知会失败的方式" in out
    assert out.split("## 变体")[0] == EXISTING.split("## 变体")[0]


def test_append_bullet_creates_a_missing_section_in_canonical_order():
    without = EXISTING.replace("## 变体\n\n- 选项文案能直接写表单值，不必逐字打。\n\n", "")
    out = append_bullet(without, "变体", "新的一条")
    assert out.index("## 它长什么样") < out.index("## 变体") < out.index("## 已知会失败的方式")
    assert "## 变体\n\n- 新的一条\n" in out


def test_replace_section_swaps_only_that_section():
    out = replace_section(EXISTING, "它长什么样", "新入口：右上角铅笔图标进编辑态。")
    assert "## 它长什么样\n\n新入口：右上角铅笔图标进编辑态。\n\n## 变体" in out
    assert "面板标题栏「编辑大纲」" not in out
    assert out.split("## 变体")[1] == EXISTING.split("## 变体")[1]      # 后面一个字节没动


def test_remove_bullet_is_exact_and_returns_none_when_reworded():
    out = remove_bullet(EXISTING, "变体", "选项文案能直接写表单值，不必逐字打。")
    assert out is not None and "选项文案" not in out and "## 变体\n\n\n## 已知" in out.replace("\n\n\n", "\n\n\n")
    assert remove_bullet(EXISTING, "变体", "选项文案能直接写表单值") is None     # 人改过措辞就不动


# ---- 已落盘的也能被推翻；人的一票是终审 ----------------------------------------------------
def _promote_one(mem, skill) -> str:
    _propose(mem, skill, node="outline-edit", kind="failure_mode", text=LESSON)
    cid = mem.live()[0].id
    mem.vote(run_id="run-B", scenario="signup", candidate_id=cid, verdict="confirm", why="试通了")
    assert mem.settle(skill)["promoted"] == [cid]
    return cid


def test_two_runs_refuting_a_promoted_entry_demote_it(mem, skill, atoms_dir):
    cid = _promote_one(mem, skill)
    assert LESSON in (atoms_dir / "outline-edit.md").read_text(encoding="utf-8")
    assert "已经落盘" in mem.vote(run_id="run-C", scenario="login", candidate_id=cid, verdict="confirm", why="x")
    assert "1/2" in mem.vote(run_id="run-C", scenario="login", candidate_id=cid, verdict="refute", why="按它点了没反应")
    assert "表过态" in mem.vote(run_id="run-C", scenario="login", candidate_id=cid, verdict="refute", why="再来")
    assert mem.settle(skill)["demoted"] == []                                   # 一票不够
    assert "2/2" in mem.vote(run_id="run-D", scenario="signup", candidate_id=cid, verdict="refute", why="同样没反应")
    result = mem.settle(skill)
    assert result["demoted"] == [cid]
    body = (atoms_dir / "outline-edit.md").read_text(encoding="utf-8")
    assert LESSON not in body and body == EXISTING                              # 划掉之后跟原来一字不差
    assert mem.promoted_entries() == []                                         # 不再算已落盘
    assert "demoted" in [r["event"] for r in mem.ledger()]


def test_brief_lists_fresh_promoted_entries_for_referenced_nodes(mem, skill, atoms_dir):
    cid = _promote_one(mem, skill)
    text = mem.brief(["outline-edit"])
    assert "最近才落盘" in text and f"[{cid}]" in text and LESSON in text
    assert f"[{cid}]" not in mem.brief(["composer-input"])                      # 没引用的节点不列


def test_human_confirm_promotes_immediately_even_with_zero_votes(mem, skill, atoms_dir):
    _propose(mem, skill, node="outline-edit", kind="variant", text="从项目列表页右键也能进编辑态")
    cid = mem.live()[0].id
    out = mem.human_vote(skill, cid, "confirm")
    assert "已追加" in out and not mem.live()
    assert "- 从项目列表页右键也能进编辑态" in (atoms_dir / "outline-edit.md").read_text(encoding="utf-8")
    promoted = [r for r in mem.ledger() if r["event"] == "promoted"]
    assert promoted and promoted[0]["by"] == "human"


def test_human_refute_drops(mem, skill, atoms_dir):
    _propose(mem, skill, node="outline-edit", kind="variant", text="从项目列表页右键也能进编辑态")
    cid = mem.live()[0].id
    assert "已丢弃" in mem.human_vote(skill, cid, "refute")
    assert not mem.live() and (atoms_dir / "outline-edit.md").read_text(encoding="utf-8") == EXISTING


def test_only_a_human_can_land_a_canonical_change(mem, skill, atoms_dir):
    _propose(mem, skill, node="outline-edit", kind="canonical", text="编辑态入口改到了右上角的铅笔图标。")
    cid = mem.live()[0].id
    for run, sc in (("run-B", "signup"), ("run-C", "choice-question")):
        mem.vote(run_id=run, scenario=sc, candidate_id=cid, verdict="confirm", why="看到了")
    assert mem.settle(skill)["kept"] == [cid]                                   # 票够也不自动写
    assert mem.candidates()[0]["ready"] is True                                 # 看板上亮「等人点」
    out = mem.human_vote(skill, cid, "confirm")
    assert "它长什么样" in out and not mem.live()
    body = (atoms_dir / "outline-edit.md").read_text(encoding="utf-8")
    assert "## 它长什么样\n\n编辑态入口改到了右上角的铅笔图标。\n\n## 变体" in body
    assert "canonical_promoted" in [r["event"] for r in mem.ledger()]


def test_candidates_view_reports_votes_and_readiness(mem, skill, atoms_dir):
    _propose(mem, skill, node="outline-edit", kind="failure_mode", text=LESSON)
    cid = mem.live()[0].id
    c = mem.candidates()[0]
    assert (c["confirms"], c["refutes"], c["need"], c["ready"], c["section"]) == (0, 0, 1, False, "已知会失败的方式")
    mem.vote(run_id="run-B", scenario="signup", candidate_id=cid, verdict="confirm", why="试通了")
    assert mem.candidates()[0]["ready"] is True
