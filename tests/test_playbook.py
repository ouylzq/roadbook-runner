"""路书结构化读写的底线：**能结构化编辑的，往返必须逐字不变**。

这套用例全是真实路书上踩出来的——2026-09-20 第一版解析器把 5 份真实路书改坏了
（续行被挪走、没有「## 步骤」的被凭空加一段、另一种步骤写法被解析成空字段）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from runtime.domain import Scenario
from runtime.playbook import PlaybookError, load_playbook, render_playbook, save_playbook

STRUCTURED = """\
# demo 路书
> 最后勘路：2026-09-20 10:00，对应环境 dev

## 全局约定

- 判据一律回读。

## 步骤

1. 【参考】—
   【目的】打开首页
   【锚点】大标题「启动」
   【动作】打开 `<app>/zh`
   【到位标志】输入框可见

2. 【参考】add-task
   【目的】提交表单
   【锚点】「提交」按钮
   【动作】点「提交」
   【到位标志】出现小结卡；底部计数变 (1/2)，
     第二行续行要原样留在这个字段里
   ⚠️ 偶发红条，重载即恢复

## 已知缺陷 / 观察

- 暂无
"""

FREEFORM = """\
# login 路书
> 最后勘路：2026-09-11

整份都是散文，没有「## 步骤」这一段。

## 留给裁判的事实

`app_base` `api_base`
"""


def scenario_with(tmp_path: Path, text: str | None) -> Scenario:
    d = tmp_path / "scenarios" / "demo"
    d.mkdir(parents=True)
    if text is not None:
        (d / "playbook.md").write_text(text, encoding="utf-8")
    return Scenario("demo", "", d, text is not None, False, None)


def test_structured_playbook_round_trips_byte_for_byte(tmp_path):
    """能结构化编辑 = 解析再渲染逐字一致。这是允许结构化编辑的唯一判据。"""
    sc = scenario_with(tmp_path, STRUCTURED)
    data = load_playbook(sc)
    assert data["structured"] is True
    assert render_playbook(data["preamble"], data["steps"], data["trailing"]) == STRUCTURED


def test_continuation_line_stays_in_its_field(tmp_path):
    """续行留在所属字段里，不被收到步骤末尾——那会改变顺序和缩进。

    最后一个字段之后的自由行（⚠️ 提示之类）也归给它：缩进后的续行和独立提示行
    在文本上没法区分，硬猜就会破坏往返。留在字段里既不丢字、编辑时也看得见。
    """
    step = load_playbook(scenario_with(tmp_path, STRUCTURED))["steps"][1]
    assert "第二行续行要原样留在这个字段里" in step["done"]
    assert "⚠️ 偶发红条，重载即恢复" in step["done"]
    assert step["notes"] == ""


def test_freeform_playbook_is_not_structured_and_keeps_raw(tmp_path):
    """没有「## 步骤」标题的路书不许结构化编辑，也不许被凭空加一个步骤段。
    散文里提到「## 步骤」这四个字不算标题（早先按子串切，被这句话骗过）。"""
    data = load_playbook(scenario_with(tmp_path, FREEFORM))
    assert data["exists"] and data["structured"] is False
    assert data["raw"] == FREEFORM
    assert data["steps"] == [] and data["preamble"].startswith("# login 路书")


def test_missing_playbook(tmp_path):
    data = load_playbook(scenario_with(tmp_path, None))
    assert data["exists"] is False and data["structured"] is False


def test_save_rejects_step_missing_required_fields(tmp_path):
    sc = scenario_with(tmp_path, STRUCTURED)
    bad = [{"ref": "—", "goal": "只有目的", "anchor": "", "action": "", "done": "", "notes": ""}]
    with pytest.raises(PlaybookError) as e:
        save_playbook(sc, "# demo 路书", bad, "")
    assert "【锚点】" in str(e.value) and "【动作】" in str(e.value)


def test_save_rejects_empty(tmp_path):
    with pytest.raises(PlaybookError):
        save_playbook(scenario_with(tmp_path, STRUCTURED), "# demo", [], "")


def test_save_writes_lint_compatible_steps(tmp_path):
    """存盘产物必须带齐 lint_playbook.py 要的五要素，否则写出来就是不合规资产。"""
    sc = scenario_with(tmp_path, STRUCTURED)
    data = load_playbook(sc)
    data["steps"][0]["goal"] = "改过的目的"
    save_playbook(sc, data["preamble"], data["steps"], data["trailing"])

    text = (sc.dir / "playbook.md").read_text(encoding="utf-8")
    assert "【目的】改过的目的" in text
    for marker in ("【参考】", "【目的】", "【锚点】", "【动作】", "【到位标志】"):
        assert text.count(marker) == 2          # 两步，每步一份
    assert load_playbook(sc)["structured"] is True      # 存完还能继续结构化编辑


def test_count_steps_matches_the_linter_definition():
    """「一步」只能有一个定义：`## 步骤` 段下 `N. ` 开头的条目，和 lint_playbook.py 一致。
    比 load_playbook 宽松是故意的——那个只在字节还原时才认结构（给编辑用的安全阀），
    而数步数只是读，不该因为格式偏差就把实实在在记着的步骤算成 0。"""
    from runtime.playbook import count_steps

    pb = ("# x 路书\n> 最后勘路：2026-09-23\n\n## 步骤\n\n"
          "1. 【参考】—\n   【目的】a\n\n2. 【参考】—\n   【目的】b\n\n## 已知缺陷\n\n3. 这行不算步骤\n")
    assert count_steps(pb) == 2          # 「## 已知缺陷」之后的编号不算
    assert count_steps("") == 0
    assert count_steps("占位：探索中途写入") == 0
