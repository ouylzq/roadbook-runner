from tools.browser.base import Observation


def test_render_truncates_with_markers():
    obs = Observation(url="u", title="t", tree="x" * 50, text="y" * 50, ref_count=1,
                      console_errors=[f"e{i}" for i in range(10)])
    out = obs.render(max_tree=10, max_text=10)
    assert "结构已截断" in out and "文本已截断" in out
    assert "e9" in out and "e0" not in out        # 只保留最后 5 条控制台错误


def test_render_does_not_leak_screenshot_path():
    out = Observation("u", "t", "tree", "text", 0, screenshot_path="/secret/001.png").render()
    assert "/secret/001.png" not in out            # 截图只留档，不进模型上下文
