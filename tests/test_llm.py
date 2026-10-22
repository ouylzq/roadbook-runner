"""LLM.chat 的记录职责：usage/耗时以前直接被丢弃，这里锁住"补上了、算对了"。
超时/重试：实测撞过一次 3.6 万 prompt token 的调用花 125 秒，跟 token 量对不上——
是网关偶发延迟尖峰，SDK 默认单次请求能等 600 秒，一卡就是干等十分钟。这里锁住
"卡住的请求最终会被当失败处理、换一次连接重试"，以及"参数错这类重试没用的错误不重试"。
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx2  # openai>=3 换了底层 HTTP 库（openai._httpx2 只是兼容层），构造异常要用这个
import openai
import pytest

from runtime.llm import LLM, LlmCallInfo


class FakeCompletions:
    def __init__(self, response):
        self._response = response
        self.last_params: dict | None = None

    def create(self, **params):
        self.last_params = params
        return self._response


class FlakyCompletions:
    """前几次调用按 `raises` 里排的顺序抛异常，抛完了才真的返回 response。"""

    def __init__(self, raises, response):
        self._raises = list(raises)
        self._response = response
        self.calls = 0

    def create(self, **params):
        self.calls += 1
        if self._raises:
            raise self._raises.pop(0)
        return self._response


def _timeout_error() -> openai.APITimeoutError:
    return openai.APITimeoutError(request=httpx2.Request("POST", "http://x/chat"))


def _bad_request_error() -> openai.BadRequestError:
    resp = httpx2.Response(400, request=httpx2.Request("POST", "http://x/chat"))
    return openai.BadRequestError("参数不对", response=resp, body=None)


def make_llm(monkeypatch, project_settings, response):
    llm = LLM(project_settings)
    fake = FakeCompletions(response)
    monkeypatch.setattr(llm._client.chat, "completions", fake)
    return llm, fake


def fake_response(*, content=None, tool_calls=None, prompt_tokens=10, completion_tokens=5,
                  model="bedrock/claude-sonnet-5", finish_reason="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
    )


def test_chat_returns_message_unchanged(monkeypatch, project_settings):
    llm, _ = make_llm(monkeypatch, project_settings, fake_response(content="hi"))
    assert llm.chat([], []).content == "hi"


def test_on_call_receives_usage_and_model(monkeypatch, project_settings):
    llm, _ = make_llm(monkeypatch, project_settings,
                      fake_response(prompt_tokens=123, completion_tokens=45, model="m-x", finish_reason="tool_calls"))
    captured: list[LlmCallInfo] = []
    llm.chat([], [], on_call=captured.append)
    info = captured[0]
    assert info.model == "m-x"
    assert info.prompt_tokens == 123 and info.completion_tokens == 45
    assert info.stop_reason == "tool_calls"
    assert info.latency_ms >= 0


def test_on_call_not_invoked_when_absent(monkeypatch, project_settings):
    llm, _ = make_llm(monkeypatch, project_settings, fake_response())
    llm.chat([], [])   # 不传 on_call 不应该报错


def test_missing_usage_defaults_to_zero_not_crash(monkeypatch, project_settings):
    response = fake_response()
    response.usage = None   # 有些网关可能不回 usage 字段
    llm, _ = make_llm(monkeypatch, project_settings, response)
    captured: list[LlmCallInfo] = []
    llm.chat([], [], on_call=captured.append)
    assert captured[0].prompt_tokens == 0 and captured[0].completion_tokens == 0


def test_temperature_none_is_not_sent(monkeypatch, project_settings):
    llm, fake = make_llm(monkeypatch, project_settings, fake_response())
    llm.temperature = None
    llm.chat([], [])
    assert "temperature" not in fake.last_params


def test_temperature_set_is_sent(monkeypatch, project_settings):
    llm, fake = make_llm(monkeypatch, project_settings, fake_response())
    llm.temperature = 0.0
    llm.chat([], [])
    assert fake.last_params["temperature"] == 0.0


def test_cache_markers_pin_head_and_slide_with_the_tail():
    """Anthropic 的缓存是显式的：不打 cache_control 一点都不缓存。
    断点分配照 OpenCode：头部钉住整批不变的 system/任务说明，尾部那个每轮往后挪——
    这一轮标在末尾，下一轮它就成了前缀，缓存跟着对话一起长。"""
    from runtime.llm import with_cache_markers

    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "T"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "结果"},
    ]
    out = with_cache_markers(msgs)
    marked = [i for i, m in enumerate(out) if isinstance(m["content"], list)]
    assert marked == [0, 1, 3]                       # 头两条 + 当前末尾
    assert out[2]["content"] is None                 # content 不是字符串的一律不碰
    assert out[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert out[0]["content"][0]["text"] == "S"
    assert [m["content"] for m in msgs] == ["S", "T", None, "结果"]   # 原列表没被就地改


def _make(monkeypatch, project_settings, fake, *, max_retries=2):
    llm = LLM(project_settings, sleep=lambda _seconds: None)   # 测试不真的睡
    llm.max_retries = max_retries
    monkeypatch.setattr(llm._client.chat, "completions", fake)
    return llm


def test_retries_once_after_a_timeout_and_succeeds(monkeypatch, project_settings):
    fake = FlakyCompletions([_timeout_error()], fake_response(content="ok"))
    llm = _make(monkeypatch, project_settings, fake)
    captured: list[LlmCallInfo] = []
    msg = llm.chat([], [], on_call=captured.append)
    assert msg.content == "ok"
    assert fake.calls == 2                      # 第一次超时，第二次才真的打通
    assert captured[0].retries == 1              # 这次调用记着"重试了一次才成"


def test_retries_are_exhausted_then_raises_the_last_error(monkeypatch, project_settings):
    fake = FlakyCompletions([_timeout_error(), _timeout_error(), _timeout_error()], fake_response())
    llm = _make(monkeypatch, project_settings, fake, max_retries=2)
    with pytest.raises(openai.APITimeoutError):
        llm.chat([], [])
    assert fake.calls == 3                       # 首次 + 2 次重试，试完就不再等了


def test_non_retryable_error_is_not_retried(monkeypatch, project_settings):
    """参数错这类重试没用——原样穿出去，别浪费时间重试一个必错的请求。"""
    fake = FlakyCompletions([_bad_request_error()], fake_response())
    llm = _make(monkeypatch, project_settings, fake)
    with pytest.raises(openai.BadRequestError):
        llm.chat([], [])
    assert fake.calls == 1


def test_zero_retries_means_first_failure_raises_immediately(monkeypatch, project_settings):
    fake = FlakyCompletions([_timeout_error()], fake_response())
    llm = _make(monkeypatch, project_settings, fake, max_retries=0)
    with pytest.raises(openai.APITimeoutError):
        llm.chat([], [])
    assert fake.calls == 1


def test_successful_first_try_reports_zero_retries(monkeypatch, project_settings):
    llm, _ = make_llm(monkeypatch, project_settings, fake_response())
    captured: list[LlmCallInfo] = []
    llm.chat([], [], on_call=captured.append)
    assert captured[0].retries == 0


def test_client_is_built_with_timeout_and_sdk_retries_disabled(project_settings):
    """SDK 自己的重试要关掉——两层重试叠在一起，"重试了几次"就没法准确记录，
    单次请求实际等多久也会失控（SDK 重试期间我们这边的 timeout 还没算完）。"""
    llm = LLM(project_settings)
    assert llm._client.max_retries == 0
    assert llm._client.timeout == project_settings.llm_timeout_seconds


def test_cache_markers_do_not_touch_tool_call_pairing():
    """只改 content，不增删消息——每个 tool_use 后面的 tool_result 必须原位还在，
    少一个续跑就是 400（2026-09-16 踩过）。"""
    from runtime.llm import with_cache_markers

    msgs = [{"role": "system", "content": "S"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "r1"}]
    out = with_cache_markers(msgs)
    assert len(out) == len(msgs)
    assert [m.get("tool_call_id") for m in out] == [None, None, "c1"]
    assert out[1]["tool_calls"] == [{"id": "c1"}]
