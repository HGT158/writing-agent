"""项目聊天上下文预算与分层压缩（架构 §3.3）。

Runtime 只负责决定"要不要落库摘要"，切分、估算与截断全部在这里，
保证压缩策略可单测、可关闭，且不依赖任何外部分词库。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Protocol

_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_MESSAGE_OVERHEAD_TOKENS = 4
_TRUNCATION_MARK = "\n\n……（正文过长，中间部分已省略；需要被省略处的完整内容时请先向用户确认）……\n\n"

SUMMARY_PREFIX = "以下是本次对话更早轮次的摘要，用于补充背景，不是用户最新指令："


class ChatMessage(Protocol):
    """兼容 MemoryStore 的 ProjectChatMessageRecord，只用到这三个字段。"""

    message_id: int
    role: str
    content: str


def estimate_tokens(text: str) -> int:
    """按字符类型估算 token：CJK 约 1 token/字，其余按 4 字符/token 折算。

    只用于是否触发压缩的判定，不追求与服务端计费口径一致。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    rest = len(text) - cjk
    return cjk + (rest + 3) // 4


def estimate_messages_tokens(messages: Iterable[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        total += _MESSAGE_OVERHEAD_TOKENS + estimate_tokens(content if isinstance(content, str) else "")
    return total


def clip_document_content(content: str, max_chars: int) -> tuple[str, bool]:
    """正文超限时保留首尾窗口，中间插入显式省略标记。"""
    if max_chars <= 0 or len(content) <= max_chars:
        return content, False
    head = max_chars * 2 // 3
    tail = max_chars - head
    return f"{content[:head]}{_TRUNCATION_MARK}{content[-tail:]}", True


@dataclass(frozen=True)
class ChatContext:
    """一次聊天要发给模型的历史部分，以及是否产生了新摘要。"""

    messages: list[dict[str, str]]
    summary: str | None = None
    summary_through_message_id: int | None = None
    compacted_message_count: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def summary_changed(self) -> bool:
        return self.compacted_message_count > 0 and self.summary is not None


def _as_prompt_messages(history: Iterable[ChatMessage]) -> list[dict[str, str]]:
    return [{"role": item.role, "content": item.content} for item in history]


def _summary_message(summary: str) -> dict[str, str]:
    return {"role": "system", "content": f"{SUMMARY_PREFIX}\n{summary}"}


def _render_for_summary(messages: Iterable[ChatMessage]) -> str:
    lines = []
    for item in messages:
        speaker = "用户" if item.role == "user" else "助手"
        lines.append(f"{speaker}：{item.content}")
    return "\n\n".join(lines)


async def build_chat_context(
    history: list[ChatMessage],
    *,
    system_tokens: int,
    token_budget: int,
    keep_recent: int,
    existing_summary: str | None,
    existing_summary_through: int | None,
    summarize: Callable[[str], Awaitable[str]],
) -> ChatContext:
    """把可见历史压成预算内的 prompt 片段。

    - `token_budget <= 0` 表示关闭压缩，直接返回全量历史（v1.15 行为）。
    - 已有摘要覆盖到 `existing_summary_through`，只对之后滑出窗口的消息增量压缩。
    - 压缩调用失败时降级为直接丢弃窗口外消息，并在 `warnings` 中说明。
    """
    covered_through = existing_summary_through if existing_summary else None
    pending = [item for item in history if covered_through is None or item.message_id > covered_through]
    carried = _summary_message(existing_summary) if existing_summary else None

    if token_budget <= 0:
        messages = _as_prompt_messages(history)
        return ChatContext(messages=messages)

    baseline = ([carried] if carried else []) + _as_prompt_messages(pending)
    if system_tokens + estimate_messages_tokens(baseline) <= token_budget:
        return ChatContext(
            messages=baseline,
            summary=existing_summary,
            summary_through_message_id=covered_through,
        )

    # 保留窗口永远全文进入 prompt；窗口之外的历史交给模型压缩。
    recent = pending[-keep_recent:] if keep_recent < len(pending) else list(pending)
    older = pending[: len(pending) - len(recent)]
    if not older:
        return ChatContext(
            messages=baseline,
            summary=existing_summary,
            summary_through_message_id=covered_through,
        )

    transcript = _render_for_summary(older)
    if existing_summary:
        transcript = f"已有摘要：\n{existing_summary}\n\n新增对话：\n{transcript}"
    warnings: list[str] = []
    try:
        summary = (await summarize(transcript)).strip()
    except Exception as exc:  # 压缩失败不能让本轮聊天失败（架构 §3.3）
        warnings.append(f"上下文压缩失败，已直接丢弃较早的 {len(older)} 条消息：{exc}")
        summary = ""
    if not summary:
        if not warnings:
            warnings.append(f"上下文压缩返回空结果，已直接丢弃较早的 {len(older)} 条消息")
        return ChatContext(
            messages=([carried] if carried else []) + _as_prompt_messages(recent),
            summary=existing_summary,
            summary_through_message_id=covered_through,
            warnings=warnings,
        )

    return ChatContext(
        messages=[_summary_message(summary), *_as_prompt_messages(recent)],
        summary=summary,
        summary_through_message_id=older[-1].message_id,
        compacted_message_count=len(older),
        warnings=warnings,
    )
