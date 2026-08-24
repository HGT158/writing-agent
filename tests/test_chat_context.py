"""项目聊天上下文预算与分层压缩（架构 §3.3）。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from agent.context import (
    SUMMARY_PREFIX,
    build_chat_context,
    clip_document_content,
    clip_content_to_token_budget,
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


def test_recent_window_overflow_truncates_and_shrinks_to_budget():
    """P2 兜底：8 条满额超长消息（无可压缩前史）也必须产出预算内的 prompt。"""
    history = _history(8, size=50_000)
    history[-1] = Message(message_id=8, role="user", content="最后一条正常指令")

    context = asyncio.run(build_chat_context(
        history,
        system_tokens=100,
        token_budget=24_000,
        keep_recent=8,
        existing_summary=None,
        existing_summary_through=None,
        summarize=_never_called,
    ))

    assert estimate_messages_tokens(context.messages) <= 24_000 - 100
    assert context.messages[-1]["content"] == "最后一条正常指令"
    assert any("……" in message["content"] for message in context.messages[:-1])
    assert any("截断" in warning or "丢弃" in warning for warning in context.warnings)


def test_single_message_exceeding_budget_alone_is_clipped():
    """单条消息自身超预算（100k 字符上限场景）也截断到预算内，最新指令不例外。"""
    history = [Message(message_id=1, role="user", content="超" * 100_000)]

    context = asyncio.run(build_chat_context(
        history,
        system_tokens=50,
        token_budget=8_000,
        keep_recent=8,
        existing_summary=None,
        existing_summary_through=None,
        summarize=_never_called,
    ))

    assert len(context.messages) == 1
    assert estimate_messages_tokens(context.messages) <= 8_000 - 50
    assert "……" in context.messages[0]["content"]
    assert any("截断" in warning for warning in context.warnings)


def test_compacted_path_also_enforces_budget():
    """压缩成功的路径同样受兜底约束：recent 自身过大时截断后进入 prompt。"""
    history = _history(20, size=20_000)
    history[-1] = Message(message_id=20, role="user", content="正常收尾指令")

    async def fake_summary(_: str) -> str:
        return "早期对话摘要"

    context = asyncio.run(build_chat_context(
        history,
        system_tokens=100,
        token_budget=16_000,
        keep_recent=8,
        existing_summary=None,
        existing_summary_through=None,
        summarize=fake_summary,
    ))

    assert context.summary == "早期对话摘要"
    assert estimate_messages_tokens(context.messages) <= 16_000 - 100
    assert context.messages[-1]["content"] == "正常收尾指令"


def test_token_clipping_handles_mixed_width_text_without_exceeding_budget():
    clipped, changed = clip_content_to_token_budget("中文" * 500 + "abcd" * 500, 120)

    assert changed is True
    assert estimate_tokens(clipped) <= 120
    assert "已省略" in clipped


def test_system_prompt_over_budget_warns_and_drops_history():
    context = asyncio.run(build_chat_context(
        _history(2, size=100),
        system_tokens=501,
        token_budget=500,
        keep_recent=2,
        existing_summary=None,
        existing_summary_through=None,
        summarize=_never_called,
    ))

    assert context.messages == []
    assert any("system prompt" in warning for warning in context.warnings)
