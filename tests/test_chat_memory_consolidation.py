"""聊天记忆注入与轮次终态选择性沉淀（v1.30，架构 §5.4）。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.chat_memory import (
    extract_explicit_command,
    extract_preferences,
    has_consolidation_signal,
)
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


class FakeAsyncStream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        self._iterator = iter(self.chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self):
        return None


def _stream_chunk(*, content=None):
    delta = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


class TurnLLM:
    """流式轮 + 非流式调用双支持；记录每次调用的 messages 与 stream 标记。"""

    def __init__(self, stream_chunks, *, non_stream_outputs=None, non_stream_error=None):
        self.stream_chunks = stream_chunks
        self.non_stream_outputs = list(non_stream_outputs or [])
        self.non_stream_error = non_stream_error
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return FakeAsyncStream(self.stream_chunks)
        if self.non_stream_error is not None:
            raise self.non_stream_error
        if self.non_stream_outputs:
            return _response(self.non_stream_outputs.pop(0))
        raise RuntimeError("意外的非流式 LLM 调用")


class StreamErrorLLM:
    def __init__(self, error):
        self.error = error
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        raise self.error


def _runtime(tmp_path: Path, llm):
    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    runtime = AgentRuntime(_settings(tmp_path), bus)
    runtime.llm = llm
    project = runtime.store.create_project("default", "记忆项目")
    document, _staled = runtime.store.save_document(
        "default", project.project_id, project.entry_document_id,
        "第一段原文。", expected_version=1,
    )
    return runtime, project, document, events


def _chat(runtime, project, document, message):
    session = runtime.store.create_project_chat_session("default", project.project_id)
    return asyncio.run(runtime.chat_project(
        "default", project.project_id, message,
        chat_session_id=session.chat_session_id,
        current_document_id=document.document_id,
    )), session.chat_session_id


def _work_items(events):
    starts = {e["data"]["work_id"]: e["data"] for e in events if e["type"] == "work_item_start"}
    items = []
    for e in events:
        if e["type"] != "work_item_done":
            continue
        data = dict(starts.get(e["data"]["work_id"], {}))
        data.update(e["data"])
        items.append(data)
    return items


# ---------- 纯函数：门槛与显式指令 ----------

def test_signal_gate_matches_feedback_and_commands_only():
    assert has_consolidation_signal("记住：摘要放在文末")
    assert has_consolidation_signal("以后都用短句")
    assert has_consolidation_signal("我不喜欢感叹号")
    assert has_consolidation_signal("语气再正式一点")
    assert not has_consolidation_signal("帮我改下第二段")
    assert not has_consolidation_signal("这篇文章讲了什么？")
    assert not has_consolidation_signal("")


def test_extract_explicit_command_strips_instruction_words():
    assert extract_explicit_command("记住：摘要放在文末") == "摘要放在文末"
    assert extract_explicit_command("请帮我记住这个：不用感叹号") == "不用感叹号"
    assert extract_explicit_command("记一下短句风格") == "短句风格"
    assert extract_explicit_command("我不喜欢感叹号") is None
    assert extract_explicit_command("记住") is None


def test_extract_preferences_normalizes_items():
    class _LLM:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        async def create(self, **kwargs):
            return _response(json.dumps({"items": [
                {"kind": "style", "content": "行文简洁" * 100},
                {"kind": "unknown-kind", "content": "未知类型"},
                {"kind": "topic", "content": ""},
                {"kind": "topic", "content": "常写模型蒸馏"},
            ]}, ensure_ascii=False))

    items = asyncio.run(extract_preferences(_LLM(), "fake", user_message="u", assistant_reply="r", profile_text=""))
    # 上限 3 条之内：空内容被丢弃（剩 2 条）；未知 kind 归 preference；超长内容截断
    assert ("style", "行文简洁" * 30) in items
    assert ("preference", "未知类型") in items
    assert len(items) == 2
    assert all(kind in {"preference", "style", "topic"} for kind, _ in items)
    assert all(len(content) <= 120 for _, content in items)


# ---------- 聊天注入 ----------

def test_chat_injects_memory_into_prompt_and_work_record(tmp_path):
    runtime, project, document, events = _runtime(tmp_path, TurnLLM([_stream_chunk(content="好的。")]))
    runtime.store.memorize("default", "preference", "正文先讲工程案例")

    result, _session = _chat(runtime, project, document, "继续完善这段")

    assert result.reply == "好的。"
    system_prompt = runtime.llm.calls[0]["messages"][0]["content"]
    assert "本助手长期记忆" in system_prompt
    assert "正文先讲工程案例" in system_prompt
    # 无信号 → 不发生任何额外模型调用
    assert len(runtime.llm.calls) == 1
    injection = [item for item in _work_items(events) if "已注入助手记忆" in item.get("title", "")]
    assert injection and "画像 1 条" in injection[0]["detail"]
    asyncio.run(runtime.close())


def test_chat_without_memory_keeps_prompt_clean(tmp_path):
    runtime, project, document, events = _runtime(tmp_path, TurnLLM([_stream_chunk(content="好的。")]))

    _chat(runtime, project, document, "继续完善这段")

    system_prompt = runtime.llm.calls[0]["messages"][0]["content"]
    assert "本助手长期记忆" not in system_prompt
    assert not [item for item in _work_items(events) if "已注入助手记忆" in item.get("title", "")]
    asyncio.run(runtime.close())


# ---------- 终态沉淀 ----------

def test_explicit_command_memorizes_without_extra_llm_call(tmp_path):
    runtime, project, document, events = _runtime(tmp_path, TurnLLM([_stream_chunk(content="好的。")]))

    result, _session = _chat(runtime, project, document, "记住：摘要放在文末")

    assert result.reply == "好的。"
    profile = runtime.store.get_assistant_profile("default")
    assert "- [偏好] 摘要放在文末" in profile
    assert len(runtime.llm.calls) == 1  # 直达路径不调模型
    consolidated = [item for item in _work_items(events) if "已沉淀助手记忆" in item.get("title", "")]
    assert consolidated and "摘要放在文末" in consolidated[0]["detail"]
    asyncio.run(runtime.close())


def test_heuristic_signal_triggers_extraction_and_memorize(tmp_path):
    extraction = json.dumps({"items": [{"kind": "style", "content": "行文简洁，不使用感叹号"}]}, ensure_ascii=False)
    runtime, project, document, events = _runtime(
        tmp_path,
        TurnLLM([_stream_chunk(content="好的，已注意。")], non_stream_outputs=[extraction]),
    )

    result, _session = _chat(runtime, project, document, "我更喜欢简洁的文风，别用感叹号")

    assert result.reply == "好的，已注意。"
    assert "- [风格] 行文简洁，不使用感叹号" in runtime.store.get_assistant_profile("default")
    assert len(runtime.llm.calls) == 2  # 流式轮 + 一次提取调用
    extraction_prompt = runtime.llm.calls[1]["messages"][-1]["content"]
    assert "现有长期画像" in extraction_prompt
    assert "我更喜欢简洁的文风" in extraction_prompt
    consolidated = [item for item in _work_items(events) if "已沉淀助手记忆" in item.get("title", "")]
    assert consolidated and "风格" in consolidated[0]["detail"]
    asyncio.run(runtime.close())


def test_extraction_failure_degrades_to_warning_and_reply_survives(tmp_path):
    runtime, project, document, events = _runtime(
        tmp_path,
        TurnLLM([_stream_chunk(content="回复照常。")], non_stream_error=RuntimeError("提取服务不可用")),
    )

    result, _session = _chat(runtime, project, document, "我更喜欢简洁的文风")

    assert result.reply == "回复照常。"
    assert runtime.store.get_assistant_profile("default") == ""
    warnings = [item for item in _work_items(events) if item.get("kind") == "warning"]
    assert warnings and "记忆沉淀失败" in warnings[0]["title"]
    asyncio.run(runtime.close())


def test_extraction_with_no_items_writes_nothing(tmp_path):
    runtime, project, document, events = _runtime(
        tmp_path,
        TurnLLM([_stream_chunk(content="好的。")], non_stream_outputs=['{"items": []}']),
    )

    _chat(runtime, project, document, "语气太正式了")

    assert runtime.store.get_assistant_profile("default") == ""
    assert not [item for item in _work_items(events) if "已沉淀助手记忆" in item.get("title", "")]
    assert not [item for item in _work_items(events) if item.get("kind") == "warning"]
    asyncio.run(runtime.close())


def test_failed_turn_skips_consolidation(tmp_path):
    runtime, project, document, _events = _runtime(
        tmp_path, StreamErrorLLM(RuntimeError("stream down"))
    )

    with pytest.raises(RuntimeError, match="stream down"):
        _chat(runtime, project, document, "记住：摘要放在文末")

    assert runtime.store.get_assistant_profile("default") == ""
    asyncio.run(runtime.close())


def test_consolidation_disabled_via_settings(tmp_path):
    runtime, project, document, events = _runtime(tmp_path, TurnLLM([_stream_chunk(content="好的。")]))
    runtime.settings.chat_memory_consolidation = False

    _chat(runtime, project, document, "记住：摘要放在文末")

    assert runtime.store.get_assistant_profile("default") == ""
    assert not [item for item in _work_items(events) if "已沉淀助手记忆" in item.get("title", "")]
    asyncio.run(runtime.close())


# ---------- 普通任务 recall 摘要 ----------

def test_run_emits_recall_summary_info_event(tmp_path: Path):
    class _Completions:
        async def create(self, *, messages, stream=False, **_kwargs):
            if stream:
                return FakeAsyncStream([_stream_chunk(content="正文。")])
            user = messages[-1]["content"]
            if "核查清单" in user:
                return _response(json.dumps({"passed": True, "missing": [], "new_preferences": []}, ensure_ascii=False))
            return _response(json.dumps({
                "thought": "直接成文",
                "next_action": "write",
                "skill": None,
                "skill_reason": "已有足够上下文",
                "tool_calls": [],
                "tool_reason": None,
                "done_criteria_met": False,
            }, ensure_ascii=False))

    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    runtime = AgentRuntime(_settings(tmp_path), bus)
    runtime.llm = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    runtime.store.memorize("default", "preference", "正文先讲工程案例")

    async def scenario():
        try:
            return await runtime.run("default", "写一篇模型蒸馏实践")
        finally:
            await runtime.close()

    state = asyncio.run(scenario())
    assert state["status"] == "done"
    infos = [e["data"].get("text", "") for e in events if e["type"] == "info"]
    assert any("已注入助手记忆" in text and "画像 1 条" in text for text in infos)
