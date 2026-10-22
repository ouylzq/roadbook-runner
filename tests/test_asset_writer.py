from runtime.asset_writer import AssetWriter


def _step(scenario: str, number: int, goal: str) -> dict:
    return {
        "scenario": scenario,
        "step_number": number,
        "reference": "—",
        "goal": goal,
        "anchor": "页面标题",
        "action": "打开页面",
        "done": "内容出现",
    }


def test_asset_writer_queues_then_flushes_in_order(skill):
    writer = AssetWriter(skill, "no-playbook")
    first = writer.enqueue_playbook_step(_step("no-playbook", 1, "第一步"))
    second = writer.enqueue_playbook_step(_step("no-playbook", 2, "第二步"))
    ok, errors = writer.flush()

    text = (skill.root / "scenarios" / "no-playbook" / "playbook.md").read_text()
    assert "已排队" in first and "已排队" in second
    assert len(ok) == 2 and errors == []
    assert text.index("【目的】第一步") < text.index("【目的】第二步")


def test_asset_writer_failure_is_reported_without_corrupting_file(skill):
    target = skill.root / "scenarios" / "with-playbook" / "playbook.md"
    before = target.read_text()
    writer = AssetWriter(skill, "with-playbook")
    writer.enqueue_playbook_step(_step("with-playbook", 9, "不能跳号"))
    ok, errors = writer.flush()

    assert ok == [] and errors and "不能直接写第 9 步" in errors[0]
    assert target.read_text() == before
