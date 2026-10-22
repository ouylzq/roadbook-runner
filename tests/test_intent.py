"""intent.md 结构化读写。规范骨架见 runtime/intent.py 的 SECTIONS。

底线跟路书一样：**合规的文件往返必须逐字不变**，不合规的老文件一律退回纯文本，
绝不拿结构化编辑器去动它。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from runtime.domain import Scenario
from runtime.intent import IntentError, load_intent, render_intent, save_intent, spec

CONFORMING = """\
---
name: demo
description: 演示场景：在待办应用里加一条任务，确认刷新之后它还在——验的是真存下来了而不只是渲染出来。
envs: [dev, prod]
---

# 场景：加一条任务

## 什么时候跑

用户说"待办还能用吗""加任务会不会丢"。

## 目标

任务真的被持久化了，刷新之后还在。

## 环境

本地 demo 应用，入口 `http://localhost:4173/`。

## 固定参数

- 任务标题固定用 `买咖啡`。

## 清理

不需要，demo 应用的数据本来就是一次性的。

## 红线

- 不要点「重置演示数据」，那会清空别的用例造的数据。
"""

LEGACY = """\
---
name: legacy
description: 老场景：规范落地之前手写的，小节名跟规范对不上，只能按纯文本改。
---

# 场景：老的

## 前置

先跑脚本。（规范里叫「前置造数」，这份写的是「前置」，对不上）

## 目标状态（UI 操作要达成的）

走完流程。
"""


def scenario_with(tmp_path: Path, text: str | None) -> Scenario:
    d = tmp_path / "scenarios" / "demo"
    d.mkdir(parents=True)
    if text is not None:
        (d / "intent.md").write_text(text, encoding="utf-8")
    return Scenario("demo", "", d, False, False, None)


def test_conforming_intent_round_trips_byte_for_byte(tmp_path):
    data = load_intent(scenario_with(tmp_path, CONFORMING))
    assert data["conforms"] is True and data["missing"] == []
    assert render_intent(data) == CONFORMING


def test_frontmatter_list_stays_inline(tmp_path):
    """`envs: [dev, prod]` 不能被展开成多行——现有文件都是行内写法，展开了往返就不一致。"""
    data = load_intent(scenario_with(tmp_path, CONFORMING))
    assert "envs: [dev, prod]" in render_intent(data)


def test_legacy_intent_is_not_conforming_and_says_what_is_missing(tmp_path):
    """老文件不许结构化编辑，而且要明确列出缺哪些必填小节、多了哪些非规范小节。"""
    data = load_intent(scenario_with(tmp_path, LEGACY))
    assert data["conforms"] is False
    assert set(data["missing"]) >= {"when", "goal", "env", "params", "cleanup", "redlines"}
    assert [b["heading"] for b in data["extra"]] == ["前置", "目标状态（UI 操作要达成的）"]
    assert data["raw"] == LEGACY


def test_missing_file(tmp_path):
    data = load_intent(scenario_with(tmp_path, None))
    assert data["exists"] is False and data["conforms"] is False


def test_save_requires_description_of_reasonable_length(tmp_path):
    sc = scenario_with(tmp_path, CONFORMING)
    data = load_intent(sc)
    data["meta"]["description"] = "太短"
    with pytest.raises(IntentError) as e:
        save_intent(sc, data)
    assert "description" in str(e.value)


def test_save_rejects_missing_required_section(tmp_path):
    sc = scenario_with(tmp_path, CONFORMING)
    data = load_intent(sc)
    data["sections"]["redlines"] = ""
    with pytest.raises(IntentError) as e:
        save_intent(sc, data)
    assert "红线" in str(e.value)


def test_save_round_trips(tmp_path):
    sc = scenario_with(tmp_path, CONFORMING)
    data = load_intent(sc)
    data["sections"]["goal"] = "改过的目标"
    save_intent(sc, data)
    again = load_intent(sc)
    assert again["conforms"] is True and again["sections"]["goal"] == "改过的目标"


def test_spec_is_the_single_source_for_the_form(tmp_path):
    """前端表单照 spec() 画，所以每一项都得带上「写什么」和示例，不能有空的。"""
    items = spec()
    assert [s["key"] for s in items][:4] == ["when", "goal", "env", "params"]
    assert all(s["hint"].strip() and s["example"].strip() for s in items)
    assert [s["key"] for s in items if s["required"]] == [
        "when", "goal", "env", "params", "cleanup", "redlines"]


def test_parse_intent_matches_load_intent_for_archived_text(tmp_path):
    """跑批目录里存档的 intent 是纯文本，看板要把它画成卡片——
    所以解析必须能脱离文件系统，且和 load_intent 走同一套逻辑（一个解析器，不许两份）。"""
    from runtime.intent import parse_intent

    sc = scenario_with(tmp_path, CONFORMING)
    assert parse_intent(CONFORMING) == load_intent(sc)
    assert parse_intent(CONFORMING)["conforms"] is True
    assert parse_intent(LEGACY)["conforms"] is False


def test_task_of_carries_structured_intent(tmp_path, monkeypatch):
    """详情页的「任务要求」页签靠 intent_doc 画卡片；存档版和当前版都得带上它。"""
    from runtime.app import QaAgent
    from runtime.config import PROJECT_ROOT, Settings

    core = QaAgent(Settings.from_env(PROJECT_ROOT))
    run_dir = tmp_path / "run-x" / "task"
    run_dir.mkdir(parents=True)
    (run_dir / "intent.md").write_text(CONFORMING, encoding="utf-8")
    monkeypatch.setattr(core, "run_dir", lambda rid: tmp_path / rid)

    task = core.task_of("run-x", "demo", None)
    assert task["archived"] is True
    assert task["intent_doc"]["conforms"] is True
    assert task["intent_doc"]["sections"]["goal"].startswith("任务真的被持久化了")
