"""LLM 调用辅助（审查 P1-8）：json_object 模式不可用时自动回退纯文本 + 宽容解析。"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import AsyncOpenAI, BadRequestError

logger = logging.getLogger(__name__)


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
