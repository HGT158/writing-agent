"""AgentRuntime 的选区改写与项目聊天入口。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import Request, Response
from openai import BadRequestError

from agent.events import EventBus
from agent.llm import stream_chat_turn
from agent.runtime import AgentRuntime
from config.settings import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path,
        data_dir=tmp_path,
        skills_dir=Path(__file__).resolve().parents[1] / "skills",
        mcp_config=tmp_path / "empty.json",
        openai_api_key="fake",
        openai_base_url="",
        model_name="fake",
    )


def _response(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class FakeLLM:
    def __init__(self, outputs: list[str | Exception]):
        self.outputs = iter(outputs)
        self.messages = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.messages.append(kwargs["messages"])
        output = next(self.outputs)
        if isinstance(output, Exception):
            raise output
        return _response(output)


class FakeAsyncStream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __aiter__(self):
        self._iterator = iter(self.chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self):
        self.closed = True


class StreamOnlyLLM:
    def __init__(self, chunks):
        self.chunks = chunks
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        self.stream = FakeAsyncStream(self.chunks)
        return self.stream


class MultiTurnStreamLLM:
    def __init__(self, turns):
        self.turns = iter(turns)
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        turn = next(self.turns)
        if isinstance(turn, Exception):
            raise turn
        return FakeAsyncStream(turn)


def _stream_chunk(*, content=None, tool_calls=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _tool_delta(index, *, call_id=None, name=None, arguments=None):
    function = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=call_id, function=function)


def test_stream_chat_turn_forwards_text_deltas():
    llm = StreamOnlyLLM([
        _stream_chunk(content="你"),
        _stream_chunk(content="好"),
    ])
    chunks = []

    turn = asyncio.run(stream_chat_turn(
        llm,
        "fake",
        [{"role": "user", "content": "问候"}],
        on_text=chunks.append,
    ))

    assert chunks == ["你", "好"]
    assert turn.text == "你好"
    assert turn.tool_calls == []
    assert llm.calls[0]["stream"] is True


def test_stream_chat_turn_accumulates_tool_argument_deltas():
    llm = StreamOnlyLLM([
        _stream_chunk(tool_calls=[_tool_delta(
            0,
            call_id="call-1",
            name="propose_project_edits",
            arguments='{"changes":[',
        )]),
        _stream_chunk(tool_calls=[_tool_delta(0, arguments=
            '{"document_id":"doc-1","old_text":"旧","new_text":"新",'
            '"document_version":1}]}'
        )]),
    ])
    tools = [{
        "type": "function",
        "function": {
            "name": "propose_project_edits",
            "description": "提出编辑建议",
            "parameters": {"type": "object"},
        },
    }]

    turn = asyncio.run(stream_chat_turn(
        llm,
        "fake",
        [{"role": "user", "content": "修改"}],
        tools=tools,
    ))

    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].id == "call-1"
    assert turn.tool_calls[0].name == "propose_project_edits"
    assert json.loads(turn.tool_calls[0].arguments)["changes"][0]["new_text"] == "新"
    assert llm.calls[0]["tools"] == tools
    assert llm.calls[0]["parallel_tool_calls"] is False


def test_stream_chat_turn_rejects_tool_arguments_over_limit():
    llm = StreamOnlyLLM([
        _stream_chunk(tool_calls=[_tool_delta(
            0,
            call_id="call-large",
            name="propose_project_edits",
            arguments="123456789",
        )]),
    ])

    with pytest.raises(RuntimeError, match="工具参数超过"):
        asyncio.run(stream_chat_turn(
            llm,
            "fake",
            [{"role": "user", "content": "修改"}],
            tools=[],
            max_tool_argument_bytes=8,
        ))
    assert llm.stream.closed is True


def test_stream_chat_turn_rejects_incomplete_tool_call():
    llm = StreamOnlyLLM([
        _stream_chunk(tool_calls=[_tool_delta(
            0,
            name="propose_project_edits",
            arguments='{"changes":[]}',
        )]),
    ])

    with pytest.raises(RuntimeError, match="工具调用流不完整"):
        asyncio.run(stream_chat_turn(
            llm,
            "fake",
            [{"role": "user", "content": "修改"}],
            tools=[],
        ))


def test_stream_chat_turn_reports_unsupported_streaming_tools():
    response = Response(400, request=Request("POST", "https://example.test/chat"))
    error = BadRequestError(
        "tools unsupported",
        response=response,
        body={"error": {"message": "tools unsupported"}},
    )
    llm = MultiTurnStreamLLM([error])

    with pytest.raises(RuntimeError, match="不支持项目 Agent 流式编辑工具"):
        asyncio.run(stream_chat_turn(
            llm,
            "fake",
            [{"role": "user", "content": "修改"}],
            tools=[],
        ))


def test_stream_chat_turn_limits_tool_arguments_by_utf8_bytes():
    llm = StreamOnlyLLM([
        _stream_chunk(tool_calls=[_tool_delta(
            0,
            call_id="call-multibyte",
            name="propose_project_edits",
            arguments="你你你",
        )]),
    ])

    with pytest.raises(RuntimeError, match="工具参数超过"):
        asyncio.run(stream_chat_turn(
            llm,
            "fake",
            [{"role": "user", "content": "修改"}],
            tools=[],
            max_tool_argument_bytes=8,
        ))


def test_stream_chat_turn_preserves_unrelated_bad_request():
    response = Response(400, request=Request("POST", "https://example.test/chat"))
    error = BadRequestError(
        "maximum context length exceeded",
        response=response,
        body={"error": {"message": "maximum context length exceeded"}},
    )
    llm = MultiTurnStreamLLM([error])

    with pytest.raises(BadRequestError, match="maximum context length"):
        asyncio.run(stream_chat_turn(
            llm,
            "fake",
            [{"role": "user", "content": "分析"}],
            tools=[],
        ))


def _prepared_runtime(tmp_path: Path, outputs: list[str | Exception]):
    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    runtime = AgentRuntime(_settings(tmp_path), bus)
    runtime.llm = FakeLLM(outputs)
    project = runtime.store.create_project("default", "改写项目")
    document, _staled = runtime.store.save_document(
        "default", project.project_id, project.entry_document_id,
        "第一段原文。第二段原文。", expected_version=1,
    )
    return runtime, project, document, events


def _chat_session(runtime: AgentRuntime, project) -> str:
    return runtime.store.create_project_chat_session(
        "default", project.project_id
    ).chat_session_id


def test_rewrite_selection_creates_preview_without_mutating_document(tmp_path):
    runtime, project, document, events = _prepared_runtime(tmp_path, ["第一段改写。"])

    change = asyncio.run(runtime.rewrite_selection(
        "default", project.project_id, document.document_id,
        start=0, end=6, selected_text="第一段原文。",
        instruction="更简洁", document_version=document.version,
    ))

    current = runtime.store.get_document("default", project.project_id, document.document_id)
    assert change.hunks[0].new_text == "第一段改写。"
    assert change.status == "pending"
    assert current.content == "第一段原文。第二段原文。"
    # SSE hunk 载荷必须对齐 v1.20 契约（含状态字段），否则前端 isChangePreview 拒绝。
    preview = next(event for event in events if event["type"] == "change_preview")
    assert preview["data"]["hunks"] == [{
        "hunk_id": change.hunks[0].hunk_id,
        "range": {"from": 0, "to": 6},
        "original": "第一段原文。",
        "replacement": "第一段改写。",
        "status": "pending",
    }]
    assert not runtime.store.is_locked("default")
    asyncio.run(runtime.close())


def test_rewrite_selection_validates_selected_text_before_llm(tmp_path):
    runtime, project, document, _ = _prepared_runtime(tmp_path, ["不应调用"])

    with pytest.raises(RuntimeError, match="选区文本"):
        asyncio.run(runtime.rewrite_selection(
            "default", project.project_id, document.document_id,
            start=0, end=6, selected_text="错误选区",
            instruction="改写", document_version=document.version,
        ))
    assert runtime.llm.messages == []
    assert not runtime.store.is_locked("default")
    asyncio.run(runtime.close())


def test_rewrite_selection_releases_lock_when_llm_fails(tmp_path):
    runtime, project, document, events = _prepared_runtime(tmp_path, [RuntimeError("llm down")])

    with pytest.raises(RuntimeError, match="llm down"):
        asyncio.run(runtime.rewrite_selection(
            "default", project.project_id, document.document_id,
            start=0, end=6, selected_text="第一段原文。",
            instruction="改写", document_version=document.version,
        ))
    assert not runtime.store.is_locked("default")
    assert any(event["type"] == "failed" for event in events)
    asyncio.run(runtime.close())


def test_project_chat_streams_plain_reply_deltas(tmp_path):
    runtime, project, document, events = _prepared_runtime(tmp_path, [])
    runtime.llm = StreamOnlyLLM([
        _stream_chunk(content="我建议"),
        _stream_chunk(content="先精简。"),
    ])

    result = asyncio.run(runtime.chat_project(
        "default",
        project.project_id,
        "有什么问题？",
        chat_session_id=_chat_session(runtime, project),
        current_document_id=document.document_id,
    ))

    assert result.reply == "我建议先精简。"
    assert result.changes == []
    assert [event["data"]["text"] for event in events if event["type"] == "token"] == [
        "我建议",
        "先精简。",
    ]
    assert runtime.llm.calls[0]["stream"] is True
    assert not runtime.store.is_locked("default")
    asyncio.run(runtime.close())


def test_project_chat_persists_messages_and_sends_complete_history(tmp_path):
    runtime, project, document, _ = _prepared_runtime(tmp_path, [])
    session = runtime.store.create_project_chat_session("default", project.project_id)
    runtime.store.add_project_chat_message(
        "default", project.project_id, session.chat_session_id,
        "user", "上一条问题",
    )
    runtime.store.add_project_chat_message(
        "default", project.project_id, session.chat_session_id,
        "assistant", "上一条回答",
    )
    runtime.llm = StreamOnlyLLM([
        _stream_chunk(content="当前"),
        _stream_chunk(content="回答"),
    ])

    result = asyncio.run(runtime.chat_project(
        "default",
        project.project_id,
        "当前问题",
        chat_session_id=session.chat_session_id,
        current_document_id=document.document_id,
    ))

    assert result.reply == "当前回答"
    assert [
        (item["role"], item["content"])
        for item in runtime.llm.calls[0]["messages"][1:]
    ] == [
        ("user", "上一条问题"),
        ("assistant", "上一条回答"),
        ("user", "当前问题"),
    ]
    assert "第一段原文。第二段原文。" in runtime.llm.calls[0]["messages"][0]["content"]
    persisted = runtime.store.list_project_chat_messages(
        "default", project.project_id, session.chat_session_id
    )
    assert [(item.role, item.content) for item in persisted] == [
        ("user", "上一条问题"),
        ("assistant", "上一条回答"),
        ("user", "当前问题"),
        ("assistant", "当前回答"),
    ]
    asyncio.run(runtime.close())


def test_project_chat_failure_persists_user_without_partial_assistant(tmp_path):
    runtime, project, document, _ = _prepared_runtime(tmp_path, [])
    session = runtime.store.create_project_chat_session("default", project.project_id)
    runtime.llm = MultiTurnStreamLLM([RuntimeError("stream down")])

    with pytest.raises(RuntimeError, match="stream down"):
        asyncio.run(runtime.chat_project(
            "default",
            project.project_id,
            "失败问题",
            chat_session_id=session.chat_session_id,
            current_document_id=document.document_id,
        ))

    persisted = runtime.store.list_project_chat_messages(
        "default", project.project_id, session.chat_session_id
    )
    assert [(item.role, item.content) for item in persisted] == [
        ("user", "失败问题"),
    ]
    assert not runtime.store.is_locked("default")
    asyncio.run(runtime.close())


def test_project_chat_returns_reply_and_pending_change_sets(tmp_path):
    runtime, project, document, events = _prepared_runtime(tmp_path, [])
    arguments = json.dumps({
        "documents": [{
            "document_id": document.document_id,
            "document_version": document.version,
            "hunks": [{"old_text": "第一段原文。", "new_text": "首段精简。"}],
            }],
    }, ensure_ascii=False)
    runtime.llm = MultiTurnStreamLLM([
        [_stream_chunk(tool_calls=[_tool_delta(
            0,
            call_id="call-edit",
            name="propose_project_edits",
            arguments=arguments,
        )])],
        [
            _stream_chunk(content="我建议"),
            _stream_chunk(content="压缩第一段。"),
        ],
    ])
    chat_session_id = _chat_session(runtime, project)

    result = asyncio.run(runtime.chat_project(
        "default", project.project_id, "请精简开头",
        chat_session_id=chat_session_id,
        current_document_id=document.document_id,
    ))

    assert result.reply == "我建议压缩第一段。"
    assert len(result.changes) == 1
    assert result.changes[0].source == "chat"
    assert result.changes[0].session_id == chat_session_id
    assert runtime.store.get_document("default", project.project_id, document.document_id).content == "第一段原文。第二段原文。"
    event_types = [event["type"] for event in events]
    assert event_types.count("tool_call") == 1
    assert event_types.count("tool_result") == 1
    assert event_types.count("change_preview") == 1
    # SSE hunk 载荷必须对齐 v1.20 契约（含状态字段），否则前端 isChangePreview 拒绝。
    preview = next(event for event in events if event["type"] == "change_preview")
    assert preview["data"]["hunks"] == [{
        "hunk_id": result.changes[0].hunks[0].hunk_id,
        "range": {"from": 0, "to": 6},
        "original": "第一段原文。",
        "replacement": "首段精简。",
        "status": "pending",
    }]
    assert [event["data"]["text"] for event in events if event["type"] == "token"] == [
        "我建议", "压缩第一段。",
    ]
    assert runtime.llm.calls[0]["tools"][0]["function"]["name"] == "propose_project_edits"
    assert "tools" not in runtime.llm.calls[1]
    asyncio.run(runtime.close())


def test_project_chat_creates_preview_for_empty_document(tmp_path):
    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    runtime = AgentRuntime(_settings(tmp_path), bus)
    project = runtime.store.create_project("default", "空白故事")
    document = runtime.store.get_document(
        "default", project.project_id, project.entry_document_id
    )
    arguments = json.dumps({
        "documents": [{
            "document_id": document.document_id,
            "document_version": document.version,
            "hunks": [{"old_text": "", "new_text": "# 小锅鱼的深圳奇遇\n\n故事正文。"}],
            }],
    }, ensure_ascii=False)
    runtime.llm = MultiTurnStreamLLM([
        [_stream_chunk(tool_calls=[_tool_delta(
            0,
            call_id="call-empty-draft",
            name="propose_project_edits",
            arguments=arguments,
        )])],
        [_stream_chunk(content="首稿已生成，请审核。")],
    ])

    result = asyncio.run(runtime.chat_project(
        "default", project.project_id, "写一个小锅鱼的深圳奇遇故事",
        chat_session_id=_chat_session(runtime, project),
        current_document_id=document.document_id,
    ))

    assert result.reply == "首稿已生成，请审核。"
    assert len(result.changes) == 1
    hunk = result.changes[0].hunks[0]
    assert hunk.start == hunk.end == 0
    assert any(event["type"] == "change_preview" for event in events)
    assert runtime.store.get_document(
        "default", project.project_id, document.document_id
    ).content == ""
    asyncio.run(runtime.close())


def test_project_chat_rolls_back_all_changes_when_one_change_is_invalid(tmp_path):
    runtime, project, document, _ = _prepared_runtime(tmp_path, [])
    arguments = json.dumps({
        "documents": [
            {
                "document_id": document.document_id,
                "document_version": document.version,
                "hunks": [{"old_text": "第一段原文。", "new_text": "首段精简。"}],
            },
            {
                "document_id": "missing-document",
                "document_version": 1,
                "hunks": [{"old_text": "x", "new_text": "y"}],
            },
        ],
    }, ensure_ascii=False)
    runtime.llm = MultiTurnStreamLLM([[
        _stream_chunk(tool_calls=[_tool_delta(
            0,
            call_id="call-invalid",
            name="propose_project_edits",
            arguments=arguments,
        )]),
    ]])

    with pytest.raises(KeyError):
        asyncio.run(runtime.chat_project(
            "default", project.project_id, "修改两处",
            chat_session_id=_chat_session(runtime, project),
            current_document_id=document.document_id,
        ))

    conn = sqlite3.connect(tmp_path / "app.db")
    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM change_sets WHERE assistant_id = ? AND project_id = ? AND status = 'pending'",
            ("default", project.project_id),
        ).fetchone()[0]
    finally:
        conn.close()
    assert pending == 0
    asyncio.run(runtime.close())


@pytest.mark.parametrize("project_state", ["missing", "other_assistant", "archived"])
def test_project_chat_validates_project_scope_without_current_document(
    tmp_path, project_state
):
    runtime, project, _, _ = _prepared_runtime(tmp_path, [])
    project_id = "missing-project"
    if project_state == "other_assistant":
        project_id = runtime.store.create_project("other", "其他助手项目").project_id
    elif project_state == "archived":
        project_id = project.project_id
        runtime.store.archive_project("default", project_id)
    runtime.llm = StreamOnlyLLM([_stream_chunk(content="不应调用模型")])

    with pytest.raises(KeyError, match="项目不存在"):
        asyncio.run(runtime.chat_project(
            "default", project_id, "分析项目",
            chat_session_id="missing-session", current_document_id=None,
        ))

    assert runtime.llm.calls == []
    assert not runtime.store.is_locked("default")
    asyncio.run(runtime.close())


def test_project_chat_rejects_multiple_tool_calls_without_changes(tmp_path):
    runtime, project, document, events = _prepared_runtime(tmp_path, [])
    arguments = json.dumps({
        "documents": [{
            "document_id": document.document_id,
            "document_version": document.version,
            "hunks": [{"old_text": document.content, "new_text": "replacement"}],
            }],
    }, ensure_ascii=False)
    runtime.llm = MultiTurnStreamLLM([[
        _stream_chunk(tool_calls=[
            _tool_delta(
                0,
                call_id="call-1",
                name="propose_project_edits",
                arguments=arguments,
            ),
            _tool_delta(
                1,
                call_id="call-2",
                name="propose_project_edits",
                arguments=arguments,
            ),
        ]),
    ]])

    with pytest.raises(RuntimeError, match="只允许一个"):
        asyncio.run(runtime.chat_project(
            "default", project.project_id, "修改两次",
            chat_session_id=_chat_session(runtime, project),
            current_document_id=document.document_id,
        ))

    assert not any(event["type"] == "tool_call" for event in events)
    assert not any(event["type"] == "change_preview" for event in events)
    assert not runtime.store.is_locked("default")
    asyncio.run(runtime.close())


def test_project_chat_emits_failed_tool_result_for_invalid_edit(tmp_path):
    runtime, project, document, events = _prepared_runtime(tmp_path, [])
    arguments = json.dumps({
        "documents": [{
            "document_id": document.document_id,
            "document_version": document.version,
            "hunks": [{"old_text": "missing text", "new_text": "replacement"}],
            }],
    })
    runtime.llm = MultiTurnStreamLLM([[
        _stream_chunk(tool_calls=[_tool_delta(
            0,
            call_id="call-invalid-edit",
            name="propose_project_edits",
            arguments=arguments,
        )]),
    ]])

    with pytest.raises(Exception, match="旧文本不存在"):
        asyncio.run(runtime.chat_project(
            "default", project.project_id, "修改不存在的文本",
            chat_session_id=_chat_session(runtime, project),
            current_document_id=document.document_id,
        ))

    tool_results = [event for event in events if event["type"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["data"]["ok"] is False
    assert not any(event["type"] == "change_preview" for event in events)
    assert not runtime.store.is_locked("default")
    asyncio.run(runtime.close())


def test_project_chat_does_not_emit_tool_call_before_schema_validation(tmp_path):
    runtime, project, document, events = _prepared_runtime(tmp_path, [])
    runtime.llm = MultiTurnStreamLLM([[
        _stream_chunk(tool_calls=[_tool_delta(
            0,
            call_id="call-invalid-schema",
            name="propose_project_edits",
            arguments=json.dumps({"documents": []}),
        )]),
    ]])

    with pytest.raises(ValueError, match="修改建议参数无效"):
        asyncio.run(runtime.chat_project(
            "default", project.project_id, "修改正文",
            chat_session_id=_chat_session(runtime, project),
            current_document_id=document.document_id,
        ))

    assert not any(event["type"] == "tool_call" for event in events)
    assert any(event["type"] == "tool_result" for event in events)
    asyncio.run(runtime.close())


def test_project_chat_persists_visible_message_when_model_reply_is_blank(tmp_path):
    runtime, project, document, _ = _prepared_runtime(tmp_path, [])
    session_id = _chat_session(runtime, project)
    runtime.llm = MultiTurnStreamLLM([[]])

    result = asyncio.run(runtime.chat_project(
        "default", project.project_id, "请回答",
        chat_session_id=session_id,
        current_document_id=document.document_id,
    ))

    assert result.reply == "模型未返回可见内容，请重试。"
    persisted = runtime.store.list_project_chat_messages(
        "default", project.project_id, session_id
    )
    assert [(item.role, item.content) for item in persisted] == [
        ("user", "请回答"),
        ("assistant", "模型未返回可见内容，请重试。"),
    ]
    asyncio.run(runtime.close())


def test_project_chat_keeps_pending_change_when_followup_stream_fails(tmp_path):
    runtime, project, document, events = _prepared_runtime(tmp_path, [])
    arguments = json.dumps({
        "documents": [{
            "document_id": document.document_id,
            "document_version": document.version,
            "hunks": [{"old_text": document.content, "new_text": "replacement"}],
            }],
    }, ensure_ascii=False)
    runtime.llm = MultiTurnStreamLLM([
        [_stream_chunk(tool_calls=[_tool_delta(
            0,
            call_id="call-followup-fails",
            name="propose_project_edits",
            arguments=arguments,
        )])],
        RuntimeError("followup down"),
    ])

    with pytest.raises(RuntimeError, match="followup down"):
        asyncio.run(runtime.chat_project(
            "default", project.project_id, "替换正文",
            chat_session_id=_chat_session(runtime, project),
            current_document_id=document.document_id,
        ))

    previews = [event for event in events if event["type"] == "change_preview"]
    assert len(previews) == 1
    change = runtime.store.get_change_set(
        "default", project.project_id, previews[0]["data"]["change_set_id"]
    )
    assert change.status == "pending"
    assert any(event["type"] == "failed" for event in events)
    assert not runtime.store.is_locked("default")
    asyncio.run(runtime.close())


def test_project_chat_releases_lock_when_stream_fails(tmp_path):
    runtime, project, document, events = _prepared_runtime(tmp_path, [])
    runtime.llm = MultiTurnStreamLLM([RuntimeError("stream down")])

    with pytest.raises(RuntimeError, match="stream down"):
        asyncio.run(runtime.chat_project(
            "default", project.project_id, "分析正文",
            chat_session_id=_chat_session(runtime, project),
            current_document_id=document.document_id,
        ))

    assert any(event["type"] == "failed" for event in events)
    assert not runtime.store.is_locked("default")
    asyncio.run(runtime.close())


class HybridLLM:
    """流式轮次走 stream_chat_turn，压缩调用走 chat_text（架构 §3.3）。"""

    def __init__(self, turns, summaries):
        self.turns = iter(turns)
        self.summaries = iter(summaries)
        self.stream_calls = []
        self.summary_calls = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        if kwargs.get("stream"):
            self.stream_calls.append(kwargs)
            turn = next(self.turns)
            if isinstance(turn, Exception):
                raise turn
            return FakeAsyncStream(turn)
        self.summary_calls.append(kwargs)
        summary = next(self.summaries)
        if isinstance(summary, Exception):
            raise summary
        return _response(summary)


def _seed_long_history(runtime, project, count: int) -> str:
    session_id = _chat_session(runtime, project)
    for index in range(count):
        runtime.store.add_project_chat_message(
            "default", project.project_id, session_id,
            "user" if index % 2 == 0 else "assistant",
            f"历史第{index + 1}条" + "内" * 200,
        )
    return session_id


def test_project_chat_compacts_long_history_and_persists_summary(tmp_path):
    runtime, project, document, events = _prepared_runtime(tmp_path, [])
    runtime.settings.chat_context_token_budget = 900
    runtime.settings.chat_context_keep_recent = 2
    session_id = _seed_long_history(runtime, project, 8)
    runtime.llm = HybridLLM([[_stream_chunk(content="回答")]], ["早期讨论摘要"])

    result = asyncio.run(runtime.chat_project(
        "default", project.project_id, "继续",
        chat_session_id=session_id,
        current_document_id=document.document_id,
    ))

    assert result.reply == "回答"
    assert len(runtime.llm.summary_calls) == 1
    sent = runtime.llm.stream_calls[0]["messages"]
    assert sent[1]["role"] == "system"
    assert "早期讨论摘要" in sent[1]["content"]
    assert [item["content"] for item in sent[2:]] == [
        "历史第8条" + "内" * 200,
        "继续",
    ]
    stored = runtime.store.get_project_chat_summary(
        "default", project.project_id, session_id
    )
    assert stored is not None
    assert stored.summary == "早期讨论摘要"
    assert any(
        event["type"] == "info" and "压缩" in event["data"]["text"] for event in events
    )
    # 压缩是派生数据：可见历史必须保持完整
    assert len(runtime.store.list_project_chat_messages(
        "default", project.project_id, session_id
    )) == 10
    asyncio.run(runtime.close())


def test_project_chat_reuses_persisted_summary_without_recompressing(tmp_path):
    runtime, project, document, _ = _prepared_runtime(tmp_path, [])
    runtime.settings.chat_context_keep_recent = 2
    session_id = _seed_long_history(runtime, project, 4)
    runtime.store.save_project_chat_summary(
        "default", project.project_id, session_id, "已有摘要", 3
    )
    runtime.llm = HybridLLM([[_stream_chunk(content="回答")]], [])

    asyncio.run(runtime.chat_project(
        "default", project.project_id, "继续",
        chat_session_id=session_id,
        current_document_id=document.document_id,
    ))

    assert runtime.llm.summary_calls == []
    sent = runtime.llm.stream_calls[0]["messages"]
    assert "已有摘要" in sent[1]["content"]
    assert [item["content"] for item in sent[2:]] == [
        "历史第4条" + "内" * 200,
        "继续",
    ]
    asyncio.run(runtime.close())


def test_project_chat_survives_context_compaction_failure(tmp_path):
    runtime, project, document, events = _prepared_runtime(tmp_path, [])
    runtime.settings.chat_context_token_budget = 900
    runtime.settings.chat_context_keep_recent = 2
    session_id = _seed_long_history(runtime, project, 8)
    runtime.llm = HybridLLM(
        [[_stream_chunk(content="仍然回答")]],
        [RuntimeError("压缩服务不可用")],
    )

    result = asyncio.run(runtime.chat_project(
        "default", project.project_id, "继续",
        chat_session_id=session_id,
        current_document_id=document.document_id,
    ))

    assert result.reply == "仍然回答"
    assert runtime.store.get_project_chat_summary(
        "default", project.project_id, session_id
    ) is None
    assert any(
        event["type"] == "warning" and "压缩" in event["data"]["text"] for event in events
    )
    sent = runtime.llm.stream_calls[0]["messages"]
    assert [item["content"] for item in sent[1:]] == [
        "历史第8条" + "内" * 200,
        "继续",
    ]
    asyncio.run(runtime.close())


def test_project_chat_clips_oversized_document_into_prompt_window(tmp_path):
    runtime, project, _, _ = _prepared_runtime(tmp_path, [])
    runtime.settings.chat_context_doc_max_chars = 120
    document, _staled = runtime.store.save_document(
        "default", project.project_id, project.entry_document_id,
        "开头标记" + "正" * 2000 + "结尾标记", expected_version=2,
    )
    runtime.llm = HybridLLM([[_stream_chunk(content="回答")]], [])

    asyncio.run(runtime.chat_project(
        "default", project.project_id, "总结正文",
        chat_session_id=_chat_session(runtime, project),
        current_document_id=document.document_id,
    ))

    system_prompt = runtime.llm.stream_calls[0]["messages"][0]["content"]
    assert "已按上下文预算截断" in system_prompt
    assert "开头标记" in system_prompt
    assert "结尾标记" in system_prompt
    assert "正" * 500 not in system_prompt
    asyncio.run(runtime.close())
