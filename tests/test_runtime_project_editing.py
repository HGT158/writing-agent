"""AgentRuntime 的选区改写与项目聊天入口。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.events import EventBus
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


def _prepared_runtime(tmp_path: Path, outputs: list[str | Exception]):
    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    runtime = AgentRuntime(_settings(tmp_path), bus)
    runtime.llm = FakeLLM(outputs)
    project = runtime.store.create_project("default", "改写项目")
    document = runtime.store.save_document(
        "default", project.project_id, project.entry_document_id,
        "第一段原文。第二段原文。", expected_version=1,
    )
    return runtime, project, document, events


def test_rewrite_selection_creates_preview_without_mutating_document(tmp_path):
    runtime, project, document, events = _prepared_runtime(tmp_path, ["第一段改写。"])

    change = asyncio.run(runtime.rewrite_selection(
        "default", project.project_id, document.document_id,
        start=0, end=6, selected_text="第一段原文。",
        instruction="更简洁", document_version=document.version,
    ))

    current = runtime.store.get_document("default", project.project_id, document.document_id)
    assert change.replacement_text == "第一段改写。"
    assert change.status == "pending"
    assert current.content == "第一段原文。第二段原文。"
    assert any(event["type"] == "change_preview" for event in events)
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


def test_project_chat_returns_reply_and_pending_change_sets(tmp_path):
    runtime, project, document, events = _prepared_runtime(tmp_path, [])
    runtime.llm = FakeLLM([json.dumps({
        "reply": "我建议压缩第一段。",
        "changes": [{
            "document_id": document.document_id,
            "start": 0,
            "end": 6,
            "original_text": "第一段原文。",
            "replacement_text": "首段精简。",
            "document_version": document.version,
        }],
    }, ensure_ascii=False)])

    result = asyncio.run(runtime.chat_project(
        "default", project.project_id, "请精简开头",
        current_document_id=document.document_id,
    ))

    assert result.reply == "我建议压缩第一段。"
    assert len(result.changes) == 1
    assert result.changes[0].source == "chat"
    assert runtime.store.get_document("default", project.project_id, document.document_id).content == "第一段原文。第二段原文。"
    assert any(event["type"] == "token" for event in events)
    assert any(event["type"] == "change_preview" for event in events)
    asyncio.run(runtime.close())


def test_project_chat_rolls_back_all_changes_when_one_change_is_invalid(tmp_path):
    runtime, project, document, _ = _prepared_runtime(tmp_path, [])
    runtime.llm = FakeLLM([json.dumps({
        "reply": "准备修改两处。",
        "changes": [
            {
                "document_id": document.document_id,
                "start": 0,
                "end": 6,
                "original_text": "第一段原文。",
                "replacement_text": "首段精简。",
                "document_version": document.version,
            },
            {
                "document_id": "missing-document",
                "start": 0,
                "end": 1,
                "original_text": "x",
                "replacement_text": "y",
                "document_version": 1,
            },
        ],
    }, ensure_ascii=False)])

    with pytest.raises(KeyError):
        asyncio.run(runtime.chat_project(
            "default", project.project_id, "修改两处",
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
