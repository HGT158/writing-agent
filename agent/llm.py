"""LLM 调用辅助（审查 P1-8）：json_object 模式不可用时自动回退纯文本 + 宽容解析。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from openai import AsyncOpenAI, BadRequestError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StreamedToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class StreamedTurn:
    text: str
    tool_calls: list[StreamedToolCall]


def _is_unsupported_streaming_tools_error(exc: BadRequestError) -> bool:
    error = exc.body.get("error", {}) if isinstance(exc.body, dict) else {}
    if not isinstance(error, dict):
        error = {}
    code = str(error.get("code", "")).lower()
    details = " ".join(
        str(value).lower()
        for value in (exc, error.get("message", ""), error.get("param", ""), code)
    )
    targets_tools_or_streaming = any(
        marker in details
        for marker in ("tool", "function calling", "parallel_tool_calls", "stream")
    )
    explicitly_unsupported = code in {"unsupported_parameter", "unsupported_value"} or any(
        marker in details
        for marker in (
            "unsupported",
            "not supported",
            "does not support",
            "unknown parameter",
            "unrecognized parameter",
        )
    )
    return targets_tools_or_streaming and explicitly_unsupported


async def stream_chat_turn(
    llm: AsyncOpenAI,
    model: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    on_text: Callable[[str], None] | None = None,
    max_tool_argument_bytes: int = 1024 * 1024,
    total_timeout_seconds: float = 300.0,
    temperature: float = 0.3,
) -> StreamedTurn:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    if tools is not None:
        kwargs["tools"] = tools
        kwargs["parallel_tool_calls"] = False
    try:
        stream = await llm.chat.completions.create(**kwargs)
    except BadRequestError as exc:
        if tools is None or not _is_unsupported_streaming_tools_error(exc):
            raise
        raise RuntimeError("当前模型服务不支持项目 Agent 流式编辑工具") from exc
    text_parts: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    argument_sizes: dict[int, int] = {}
    try:
        async with asyncio.timeout(total_timeout_seconds):
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                content = delta.content
                if content:
                    text_parts.append(content)
                    if on_text is not None:
                        on_text(content)
                for item in getattr(delta, "tool_calls", None) or []:
                    current = calls.setdefault(
                        item.index,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    if item.id:
                        current["id"] = item.id
                    if item.function and item.function.name:
                        current["name"] = item.function.name
                    if item.function and item.function.arguments:
                        fragment = item.function.arguments
                        current["arguments"] += fragment
                        argument_sizes[item.index] = argument_sizes.get(item.index, 0) + len(
                            fragment.encode("utf-8")
                        )
                        if argument_sizes[item.index] > max_tool_argument_bytes:
                            raise RuntimeError("工具参数超过 1 MiB 上限")
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:
                logger.warning("关闭项目聊天模型流失败", exc_info=True)
    tool_calls = [StreamedToolCall(**calls[index]) for index in sorted(calls)]
    if any(not item.id or not item.name for item in tool_calls):
        raise RuntimeError("工具调用流不完整")
    return StreamedTurn(text="".join(text_parts), tool_calls=tool_calls)


async def chat_text(
    llm: AsyncOpenAI,
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.3,
    json_mode: bool = True,
) -> str:
    """返回助手消息文本。json_mode=True 时优先带 response_format=json_object，
    只有服务端明确拒绝该参数（400 BadRequest）才回退纯文本重试；
    网络/鉴权/限流等错误直接上抛，不做无谓重试（复审 R3）。"""
    kwargs: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
    if json_mode:
        try:
            resp = await llm.chat.completions.create(**kwargs, response_format={"type": "json_object"})
            return resp.choices[0].message.content or ""
        except BadRequestError as exc:
            logger.warning("json_object 模式被拒绝（400），回退纯文本：%s", exc)
    resp = await llm.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> str:
    """从可能含 Markdown 围栏/前后杂文的文本中提取 JSON 子串（宽容解析）。"""
    cleaned = _FENCE_RE.sub("", text).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise json.JSONDecodeError("文本中未找到 JSON 对象", cleaned, 0)
    return cleaned[start : end + 1]
