"""项目聊天上下文预算与分层压缩（架构 §3.3）。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from agent.context import (
    SUMMARY_PREFIX,
    build_chat_context,
    clip_document_content,
    estimate_messages_tokens,
    estimate_tokens,
)


@dataclass(frozen=True)
class Message:
    message_id: int
    role: str
    content: str


def _history(count: int, *, size: int = 200) -> list[Message]:
    return [
        Message(
            message_id=index + 1,
            role="user" if index % 2 == 0 else "assistant",
            content=f"第{index + 1}条" + "内" * size,
        )
        for index in range(count)
    ]


async def _never_called(_: str) -> str:
    raise AssertionError("不应触发压缩")


def test_estimate_tokens_separates_cjk_from_ascii():
    assert estimate_tokens("") == 0
    assert estimate_tokens("中文四字") == 4
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("中文abcd") == 3


def test_estimate_messages_tokens_counts_per_message_overhead():
    messages = [{"role": "user", "content": "中文四字"}, {"role": "assistant", "content": ""}]
    assert estimate_messages_tokens(messages) == 4 + 4 + 4


def test_clip_document_content_keeps_head_and_tail():
    content = "".join(str(index % 10) for index in range(1000))
    clipped, truncated = clip_document_content(content, 300)

    assert truncated is True
    assert clipped.startswith(content[:200])
    assert clipped.endswith(content[-100:])
    assert "已省略" in clipped
    assert clip_document_content(content, 0) == (content, False)
    assert clip_document_content("短正文", 300) == ("短正文", False)


def test_zero_budget_disables_compaction():
    history = _history(30)

    context = asyncio.run(build_chat_context(
        history,
        system_tokens=1000,
        token_budget=0,
        keep_recent=2,
        existing_summary=None,
        existing_summary_through=None,
        summarize=_never_called,
    ))

    assert len(context.messages) == 30
    assert context.summary_changed is False


def test_history_within_budget_is_sent_verbatim():
    history = _history(3, size=5)

    context = asyncio.run(build_chat_context(
        history,
        system_tokens=10,
        token_budget=24000,
        keep_recent=2,
        existing_summary=None,
        existing_summary_through=None,
        summarize=_never_called,
    ))

    assert [item["content"] for item in context.messages] == [item.content for item in history]
    assert context.summary_changed is False


def test_over_budget_history_is_summarized_and_recent_kept_verbatim():
    history = _history(10)
    calls: list[str] = []

    async def summarize(transcript: str) -> str:
        calls.append(transcript)
        return "早期对话摘要"

    context = asyncio.run(build_chat_context(
        history,
        system_tokens=100,
        token_budget=900,
        keep_recent=3,
        existing_summary=None,
        existing_summary_through=None,
        summarize=summarize,
    ))

    assert len(calls) == 1
    assert "第1条" in calls[0] and "第7条" in calls[0] and "第8条" not in calls[0]
    assert context.messages[0]["role"] == "system"
    assert context.messages[0]["content"].startswith(SUMMARY_PREFIX)
    assert [item["content"] for item in context.messages[1:]] == [
        item.content for item in history[-3:]
    ]
    assert context.summary == "早期对话摘要"
    assert context.summary_through_message_id == 7
    assert context.compacted_message_count == 7


def test_existing_summary_is_reused_without_recompressing():
    history = _history(10, size=5)

    context = asyncio.run(build_chat_context(
        history,
        system_tokens=10,
        token_budget=24000,
        keep_recent=3,
        existing_summary="旧摘要",
        existing_summary_through=7,
        summarize=_never_called,
    ))

    assert context.messages[0]["content"].endswith("旧摘要")
    assert [item["content"] for item in context.messages[1:]] == [
        item.content for item in history[7:]
    ]
    assert context.summary_changed is False


def test_incremental_compaction_merges_previous_summary():
    history = _history(10)
    calls: list[str] = []

    async def summarize(transcript: str) -> str:
        calls.append(transcript)
        return "合并后摘要"

    context = asyncio.run(build_chat_context(
        history,
        system_tokens=100,
        token_budget=900,
        keep_recent=2,
        existing_summary="旧摘要",
        existing_summary_through=4,
        summarize=summarize,
    ))

    assert "旧摘要" in calls[0]
    assert "第5条" in calls[0]
    assert "第1条" not in calls[0]
    assert context.summary == "合并后摘要"
    assert context.summary_through_message_id == 8


def test_compaction_failure_degrades_to_dropping_oldest_messages():
    history = _history(10)

    async def failing(_: str) -> str:
        raise RuntimeError("模型不可用")

    context = asyncio.run(build_chat_context(
        history,
        system_tokens=100,
        token_budget=900,
        keep_recent=2,
        existing_summary=None,
        existing_summary_through=None,
        summarize=failing,
    ))

    assert [item["content"] for item in context.messages] == [
        item.content for item in history[-2:]
    ]
    assert context.summary_changed is False
    assert context.warnings and "模型不可用" in context.warnings[0]


def test_blank_summary_is_treated_as_failure():
    history = _history(10)

    async def blank(_: str) -> str:
        return "   "

    context = asyncio.run(build_chat_context(
        history,
        system_tokens=100,
        token_budget=900,
        keep_recent=2,
        existing_summary=None,
        existing_summary_through=None,
        summarize=blank,
    ))

    assert context.summary_changed is False
    assert context.warnings
