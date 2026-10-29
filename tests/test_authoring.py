"""authoring 的纯函数：写入收集、健康校验、状态判定。"""
from runtime.authoring import _check_health, _collect_written, _status_of
from runtime.traces import Step


def _wstep(scenario: str, filename: str, ok: bool) -> Step:
    preview = f"已创建 {scenario}/{filename}" if ok else "拒绝：路径越界"
    return Step(n=1, tool="write_scenario_file",
                args={"scenario": scenario, "filename": filename},
                result_preview=preview)


def test_collect_written_only_counts_successful_writes():
    steps = [_wstep("t", "intent.md", True), _wstep("t", "expect.yaml", False)]
    assert _collect_written(steps) == ["t/intent.md"]


def test_collect_written_dedupes():
    steps = [_wstep("t", "intent.md", True), _wstep("t", "intent.md", True)]
    assert _collect_written(steps) == ["t/intent.md"]


def test_status_of_health():
    assert _status_of({"intent": True, "expect": True}) == "ok"
    assert _status_of({"intent": False, "expect": False}) == "failed"
    assert _status_of({"intent": True, "expect": False}) == "partial"


def test_check_health_reads_written_files(tmp_path):
    (tmp_path / "s1").mkdir()
    (tmp_path / "s1" / "intent.md").write_text(
        "---\nname: s1\ndescription: d\n---\n\n# 场景\n\n## 什么时候跑\n\n写。\n", encoding="utf-8")
    (tmp_path / "s1" / "expect.yaml").write_text(
        "goal: g\nsteps:\n  - go: 打开页面\n    check:\n      - 看到标题\n", encoding="utf-8")
    h = _check_health(tmp_path, "s1")
    assert h["intent"] and h["expect"]


def test_check_health_flags_placeholder_intent(tmp_path):
    (tmp_path / "s2").mkdir()
    (tmp_path / "s2" / "intent.md").write_text(
        "description: ⏳ 场景生成中（示例）——完成后替换\n", encoding="utf-8")
    h = _check_health(tmp_path, "s2")
    assert not h["intent"] and not h["expect"]


def test_check_health_on_missing_dir(tmp_path):
    h = _check_health(tmp_path, "nope")
    assert h == {"intent": False, "expect": False, "placeholder": False}
