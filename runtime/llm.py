"""LLM 客户端：OpenAI 兼容协议，换供应商只改 .env。

`usage`（token 用量）和耗时以前直接被 `.choices[0].message` 这一步截掉、从没留下过——
这里补上 `on_call` 钩子，把每次调用的元数据交出去，不改变返回值本身。
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from openai.types.chat import ChatCompletionMessage

from .config import Settings, get_settings

# 只重试"再来一次可能就好了"的错误：网络没通、请求超时、限流、网关自己 5xx 了。
# 请求本身有问题（参数错 400、鉴权错 401……）重试是在原地重复同一个错误，纯粹多等而已。
RETRYABLE = (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError)


@dataclass(frozen=True)
class LlmCallInfo:
    """一次 LLM 调用的元数据。不带 prompt/response 原文——那类大内容留在文件里，
    这里只留"能查、能算总量"的那部分。"""

    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    stop_reason: str | None
    cache_read_tokens: int = 0      # 命中缓存的部分，按 0.1× 计
    cache_write_tokens: int = 0     # 这次写进缓存的部分，按 1.25× 计
    retries: int = 0                # 这次调用重试了几次才成功；0 = 一次就成，latency_ms 已含重试耗时


LlmCallHook = Callable[[LlmCallInfo], None]

# 提示词缓存：Anthropic 的缓存是**显式**的，不打 cache_control 标记就一点都不缓存。
# 2026-09-23 实测本网关（bedrock/claude-sonnet-5）支持它：第一次写入 4504 tokens，
# 第二次原样读回 4504。写入 1.25×、读取 0.1×——一次 52 轮的跑批历史几乎整段都是稳定前缀，
# 不打标记就等于每轮按原价重发一遍（实测一次 PASS 跑批 3.2M prompt tokens，一分没省）。
#
# 断点怎么分配照 OpenCode 的 applyCaching：**头部钉住不变的部分，尾部跟着对话滑**。
# 头部（system + 首条任务说明）整个跑批不变；尾部那个每轮往后挪一格——
# 这一轮标在末尾，下一轮它就成了前缀，于是缓存跟着对话一起长，而不是冻在开头。
# Anthropic 最多 4 个断点，这里用 3 个，留一个余量。
#
# ⚠️ 别在跑批中途改写前面的消息。那会让前缀失效、缓存全部落空——Codex 源码里
# 「Trim from the beginning to preserve cache (prefix-based)」那句注释就是冲这个来的。
CACHE_MARK = {"type": "ephemeral"}


def _marked(content: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": content, "cache_control": CACHE_MARK}]


def with_cache_markers(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """在稳定前缀和当前末尾打缓存断点。只碰 content 是非空字符串的消息，其余原样。"""
    out = [dict(m) for m in messages]
    markable = [i for i, m in enumerate(out) if isinstance(m.get("content"), str) and m["content"]]
    if not markable:
        return out
    head = [i for i in markable if out[i]["role"] in ("system", "user")][:2]   # 不变的那截
    tail = markable[-1:]                                                       # 随对话滑动
    for i in dict.fromkeys([*head, *tail]):
        out[i]["content"] = _marked(out[i]["content"])
    return out


class LLM:
    def __init__(self, settings: Settings | None = None, *, model: str | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        settings = settings or get_settings()
        self.model = model or settings.llm_model
        self.max_tokens = settings.llm_max_tokens
        self.temperature = settings.llm_temperature
        self.prompt_cache = settings.prompt_cache
        self.max_retries = settings.llm_max_retries
        self._sleep = sleep    # 测试注入一个不真的睡的假函数，重试逻辑不用为此跑几十秒
        self._last_retries = 0
        # max_retries=0：SDK 自己也会重试，两层叠在一起会把"重试了几次"变得没法追踪，
        # 也会让单次请求的实际等待时间失控（SDK 重试期间我们这边的 timeout 还没算完）。
        # 重试只在这一层做，SDK 只管单次请求的超时。
        self._client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url,
                              timeout=settings.llm_timeout_seconds, max_retries=0)

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
             *, on_call: LlmCallHook | None = None) -> ChatCompletionMessage:
        params: dict[str, Any] = {
            "model": self.model,
            "messages": with_cache_markers(messages) if self.prompt_cache else messages,
            "tools": tools,
            "max_tokens": self.max_tokens,
        }
        # 新版 Claude 在 Bedrock 上废弃了 temperature：不配就不传，传了会 400
        if self.temperature is not None:
            params["temperature"] = self.temperature

        started = time.perf_counter()
        completion = self._request_with_retry(params)
        if on_call:
            usage = completion.usage
            on_call(LlmCallInfo(
                model=completion.model,
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                latency_ms=int((time.perf_counter() - started) * 1000),
                stop_reason=completion.choices[0].finish_reason,
                # 缓存用量：不记下来就完全看不见缓存有没有生效、省了多少
                cache_read_tokens=_usage_int(usage, "cache_read_input_tokens"),
                cache_write_tokens=_usage_int(usage, "cache_creation_input_tokens"),
                retries=self._last_retries,
            ))
        return completion.choices[0].message

    def _request_with_retry(self, params: dict[str, Any]) -> Any:
        """卡住的单次请求最多等 `settings.llm_timeout_seconds`，超时/连接错/限流/网关 5xx
        换一次连接重试；参数错这类重试没用的错误直接穿出去，不浪费时间重试一个必错的请求。
        重试间隔短退避（2s、4s……），不是为了防限流硬顶，是给网关那次偶发抖动一点喘息时间。
        """
        self._last_retries = 0
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            try:
                return self._client.chat.completions.create(**params)
            except RETRYABLE:
                self._last_retries = attempt + 1
                if attempt + 1 >= attempts:
                    raise
                self._sleep(min(2 ** (attempt + 1), 10))
        raise AssertionError("unreachable")   # attempts >= 1，循环要么 return 要么 raise


def _usage_int(usage: Any, field: str) -> int:
    """网关对缓存字段的命名各家不一，取不到就当 0——记录用量绝不该拖垮跑批。"""
    if usage is None:
        return 0
    value = getattr(usage, field, None)
    if value is None:
        details = getattr(usage, "prompt_tokens_details", None)
        value = getattr(details, field.replace("input_", ""), None) if details else None
    return int(value or 0)
