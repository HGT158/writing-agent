"""项目聊天持久化工作记录（v1.19）：store、记录器、Runtime 与 API 契约。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.events import EventBus
from agent.runtime import AgentRuntime
from agent.work_log import WorkLogRecorder
from config.settings import Settings
from memory.store import MemoryStore


def _add_user_message(store: MemoryStore, assistant: str, project, session: str) -> int:
    record = store.add_project_chat_message(assistant, project.project_id, session, "user", "改一下开头")
    return record.message_id


def _add_event(store: MemoryStore, project, session: str, *, task_id: str, seq: int,
               kind: str = "progress", status: str = "succeeded", user_message_id: int = 1,
               assistant: str = "writer-a", **kwargs) -> None:
    store.add_project_chat_work_event(
        assistant, project.project_id, session,
        task_id=task_id, user_message_id=user_message_id, event_seq=seq,
        kind=kind, status=status, title=kwargs.get("title", "条目"),
        detail=kwargs.get("detail", ""), created_at=kwargs.get("created_at", "2026-08-16T00:00:00"),
        completed_at=kwargs.get("completed_at"), tool_name=kwargs.get("tool_name"),
        args_summary=kwargs.get("args_summary"), result_summary=kwargs.get("result_summary"),
        change_set_id=kwargs.get("change_set_id"), document_id=kwargs.get("document_id"),
    )


# ---------- MemoryStore ----------

def test_work_events_persist_list_order_and_isolation(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "工作记录项目")
    session = store.create_project_chat_session("writer-a", project.project_id)
    user_message_id = _add_user_message(store, "writer-a", project, session.chat_session_id)

    _add_event(store, project, session.chat_session_id, task_id="t1", seq=2,
               kind="tool", user_message_id=user_message_id, tool_name="propose_project_edits")
    _add_event(store, project, session.chat_session_id, task_id="t1", seq=1,
               user_message_id=user_message_id)
    _add_event(store, project, session.chat_session_id, task_id="t1", seq=3, kind="task")

    events = store.list_project_chat_work_events(
        "writer-a", project.project_id, session.chat_session_id
    )
    assert [(item.task_id, item.event_seq) for item in events] == [
        ("t1", 1), ("t1", 2), ("t1", 3),
    ]
    assert events[1].tool_name == "propose_project_edits"

    with pytest.raises(KeyError):
        store.list_project_chat_work_events(
            "writer-b", project.project_id, session.chat_session_id
        )
    with pytest.raises(KeyError):
        store.add_project_chat_work_event(
            "writer-a", project.project_id, "missing-session",
            task_id="t1", user_message_id=1, event_seq=1,
            kind="progress", status="succeeded", title="x", created_at="now",
        )
    with pytest.raises(ValueError):
        _add_event(store, project, session.chat_session_id, task_id="t2", seq=1, kind="bogus")
    store.close()


def test_work_event_seq_unique_and_task_terminal_idempotent(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "唯一索引项目")
    session = store.create_project_chat_session("writer-a", project.project_id)

    _add_event(store, project, session.chat_session_id, task_id="t1", seq=1)
    with pytest.raises(sqlite3.IntegrityError):
        _add_event(store, project, session.chat_session_id, task_id="t1", seq=1)

    first = store.add_project_chat_work_event(
        "writer-a", project.project_id, session.chat_session_id,
        task_id="t1", user_message_id=1, event_seq=5, kind="task",
        status="succeeded", title="任务完成", created_at="a", completed_at="b",
    )
    duplicated = store.add_project_chat_work_event(
        "writer-a", project.project_id, session.chat_session_id,
        task_id="t1", user_message_id=1, event_seq=9, kind="task",
        status="succeeded", title="重复终态", created_at="c", completed_at="d",
    )
    assert duplicated.event_id == first.event_id
    terminals = [
        item for item in store.list_project_chat_work_events(
            "writer-a", project.project_id, session.chat_session_id
        ) if item.kind == "task"
    ]
    assert len(terminals) == 1
    _add_event(store, project, session.chat_session_id, task_id="t2", seq=1, kind="task")
    terminals = [
        item for item in store.list_project_chat_work_events(
            "writer-a", project.project_id, session.chat_session_id
        ) if item.kind == "task"
    ]
    assert len(terminals) == 2
    store.close()


def test_unfinished_work_task_ids_and_interrupt(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "对账项目")
    session = store.create_project_chat_session("writer-a", project.project_id)

    assert store.list_unfinished_project_chat_work_task_ids(
        "writer-a", project.project_id, session.chat_session_id
    ) == []
    _add_event(store, project, session.chat_session_id, task_id="t1", seq=1)
    _add_event(store, project, session.chat_session_id, task_id="t1", seq=2)
    _add_event(store, project, session.chat_session_id, task_id="t2", seq=1, kind="task")

    assert store.list_unfinished_project_chat_work_task_ids(
        "writer-a", project.project_id, session.chat_session_id
    ) == ["t1"]

    store.interrupt_project_chat_work_task(
        "writer-a", project.project_id, session.chat_session_id, "t1"
    )
    store.interrupt_project_chat_work_task(
        "writer-a", project.project_id, session.chat_session_id, "t1"
    )
    events = store.list_project_chat_work_events(
        "writer-a", project.project_id, session.chat_session_id
    )
    terminal = [item for item in events if item.task_id == "t1" and item.kind == "task"]
    assert len(terminal) == 1
    assert terminal[0].status == "interrupted"
    assert terminal[0].event_seq == 3
    assert store.list_unfinished_project_chat_work_task_ids(
        "writer-a", project.project_id, session.chat_session_id
    ) == []
    store.close()


def test_work_events_cascade_delete(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "级联项目")
    session = store.create_project_chat_session("writer-a", project.project_id)
    other = store.create_project("writer-b", "另一助手项目")
    other_session = store.create_project_chat_session("writer-b", other.project_id)
    _add_user_message(store, "writer-b", other, other_session.chat_session_id)
    _add_event(store, other, other_session.chat_session_id, task_id="t-other", seq=1,
               assistant="writer-b")

    def count() -> int:
        return store._conn.execute(
            "SELECT COUNT(*) FROM project_chat_work_events"
        ).fetchone()[0]

    _add_user_message(store, "writer-a", project, session.chat_session_id)
    _add_event(store, project, session.chat_session_id, task_id="t1", seq=1)
    _add_event(store, project, session.chat_session_id, task_id="t1", seq=2, kind="task")
    assert count() == 3

    store.delete_project_chat_session("writer-a", project.project_id, session.chat_session_id)
    assert count() == 1

    project2 = store.create_project("writer-a", "再建项目")
    session2 = store.create_project_chat_session("writer-a", project2.project_id)
    _add_user_message(store, "writer-a", project2, session2.chat_session_id)
    _add_event(store, project2, session2.chat_session_id, task_id="t2", seq=1)
    store.purge_project("writer-a", project2.project_id)
    assert count() == 1

    store.purge_assistant("writer-b")
    assert count() == 0
    store.close()


# ---------- WorkLogRecorder ----------

def _recorder(tmp_path, project, session: str, user_message_id: int = 1):
    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    store = MemoryStore(tmp_path)
    recorder = WorkLogRecorder(
        store, bus,
        assistant_id="writer-a", project_id=project.project_id,
        chat_session_id=session, task_id="task-1", user_message_id=user_message_id,
    )
    return recorder, store, events


def test_recorder_streams_and_persists_on_done(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "记录器项目")
    session = store.create_project_chat_session("writer-a", project.project_id)
    user_message_id = _add_user_message(store, "writer-a", project, session.chat_session_id)
    recorder, store, events = _recorder(tmp_path, project, session.chat_session_id, user_message_id)

    work_id = recorder.start("tool", "正在准备修改", tool_name="propose_project_edits",
                             args={"changes": 2})
    recorder.delta(work_id, "正在校验修改范围")
    assert store.list_project_chat_work_events(
        "writer-a", project.project_id, session.chat_session_id
    ) == []  # delta 与 start 都不落库
    recorder.done(work_id, result='{"change_set_ids": ["c1"]}')
    recorder.finish_task("succeeded", title="改一下开头")

    kinds = [(event["type"], event["data"].get("work_id")) for event in events]
    assert kinds == [
        ("work_item_start", work_id),
        ("work_item_delta", work_id),
        ("work_item_done", work_id),
    ]
    persisted = store.list_project_chat_work_events(
        "writer-a", project.project_id, session.chat_session_id
    )
    assert [(item.kind, item.status, item.event_seq) for item in persisted] == [
        ("tool", "succeeded", 1),
        ("task", "succeeded", 2),
    ]
    assert persisted[0].result_summary == '{"change_set_ids": ["c1"]}'
    assert persisted[1].user_message_id == user_message_id
    store.close()


def test_recorder_redacts_and_truncates(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "截断项目")
    session = store.create_project_chat_session("writer-a", project.project_id)
    recorder, store, _ = _recorder(tmp_path, project, session.chat_session_id)

    work_id = recorder.start("tool", "调用工具", args={
        "api_key": "sk-secret", "nested": {"token": "abc", "ok": 1},
    })
    recorder.done(work_id, result="x" * 10_000)
    recorder.finish_task("succeeded")

    persisted = store.list_project_chat_work_events(
        "writer-a", project.project_id, session.chat_session_id
    )
    args_summary = persisted[0].args_summary
    assert "sk-secret" not in args_summary and "abc" not in args_summary
    assert '"***"' in args_summary and '"ok": 1' in args_summary
    result = persisted[0].result_summary
    assert result.startswith("x" * 50)
    assert result.endswith("x" * 50)
    assert "已截断" in result and "10000" in result
    assert len(result) <= 8_200
    store.close()


def test_recorder_detail_limit_and_overflow_summary(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "上限项目")
    session = store.create_project_chat_session("writer-a", project.project_id)
    recorder, store, _ = _recorder(tmp_path, project, session.chat_session_id)

    for index in range(199):
        work_id = recorder.start("progress", f"步骤 {index}")
        recorder.done(work_id)
    for index in range(6):
        work_id = recorder.start("tool", f"追加工具 {index}", tool_name="propose_project_edits")
        recorder.done(work_id)
    recorder.finish_task("succeeded")

    persisted = store.list_project_chat_work_events(
        "writer-a", project.project_id, session.chat_session_id
    )
    details = [item for item in persisted if item.kind != "task"]
    assert len(details) == 200
    assert details[-1].event_seq == 200
    assert "省略 6 条" in details[-1].title
    # 溢出摘要按被省略事件的类型合并计数，不是已持久化明细的分布（架构 §5.7）。
    assert "工具 6 条" in details[-1].title
    assert not any(item.event_seq > 200 and item.kind != "task" for item in persisted)
    terminal = [item for item in persisted if item.kind == "task"]
    assert len(terminal) == 1 and terminal[0].event_seq == 206
    store.close()


def test_recorder_overflow_absent_when_under_limit(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "无溢出项目")
    session = store.create_project_chat_session("writer-a", project.project_id)
    recorder, store, _ = _recorder(tmp_path, project, session.chat_session_id)

    for index in range(3):
        work_id = recorder.start("progress", f"步骤 {index}")
        recorder.done(work_id)
    recorder.finish_task("succeeded")

    persisted = store.list_project_chat_work_events(
        "writer-a", project.project_id, session.chat_session_id
    )
    assert [item.event_seq for item in persisted] == [1, 2, 3, 4]
    assert not any("省略" in item.title for item in persisted)
    store.close()


def test_recorder_interrupts_running_items_on_failure(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "中断项目")
    session = store.create_project_chat_session("writer-a", project.project_id)
    recorder, store, _ = _recorder(tmp_path, project, session.chat_session_id)

    kept = recorder.start("progress", "已完成步骤")
    recorder.done(kept)
    running = recorder.start("tool", "运行中工具", tool_name="propose_project_edits")
    recorder.interrupt_running()
    recorder.finish_task("failed")

    persisted = store.list_project_chat_work_events(
        "writer-a", project.project_id, session.chat_session_id
    )
    assert [(item.kind, item.status) for item in persisted] == [
        ("progress", "succeeded"),
        ("tool", "interrupted"),
        ("task", "failed"),
    ]
    store.close()


# ---------- Runtime ----------

def _runtime(tmp_path: Path) -> AgentRuntime:
    return AgentRuntime(Settings(
        project_root=tmp_path, data_dir=tmp_path,
        skills_dir=Path(__file__).resolve().parents[1] / "skills",
        mcp_config=tmp_path / "empty.json",
        openai_api_key="fake", openai_base_url="", model_name="fake",
    ))


def test_chat_project_persists_work_log_and_keeps_it_out_of_prompt(tmp_path):
    from tests.test_runtime_project_editing import (
        MultiTurnStreamLLM, _stream_chunk, _tool_delta,
    )

    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    runtime = AgentRuntime(_settings(tmp_path), bus)
    project = runtime.store.create_project("default", "工作记录运行时")
    document, _staled = runtime.store.save_document(
        "default", project.project_id, project.entry_document_id,
        "第一段原文。第二段原文。", expected_version=1,
    )
    session = runtime.store.create_project_chat_session("default", project.project_id)
    arguments = json.dumps({
        "documents": [{
            "document_id": document.document_id,
            "document_version": document.version,
            "hunks": [{"old_text": "第一段原文。", "new_text": "第一段改写。"}],
        }],
    }, ensure_ascii=False)
    runtime.llm = MultiTurnStreamLLM([
        [_stream_chunk(tool_calls=[_tool_delta(
            0, call_id="call-1", name="propose_project_edits", arguments=arguments,
        )])],
        [_stream_chunk(content="修改建议已生成。")],
    ])

    with bus.task_scope("broker-task-9"):
        result = asyncio.run(runtime.chat_project(
            "default", project.project_id, "请精简开头",
            chat_session_id=session.chat_session_id,
            current_document_id=document.document_id,
        ))

    work = runtime.store.list_project_chat_work_events(
        "default", project.project_id, session.chat_session_id
    )
    kinds = [(item.kind, item.status) for item in work]
    assert ("tool", "succeeded") in kinds
    assert ("changes", "succeeded") in kinds
    assert kinds[-1] == ("task", "succeeded")
    assert all(item.task_id == "broker-task-9" for item in work)
    user_message = [
        item for item in runtime.store.list_project_chat_messages(
            "default", project.project_id, session.chat_session_id
        ) if item.role == "user"
    ][-1]
    assert {item.user_message_id for item in work} == {user_message.message_id}
    changes_item = next(item for item in work if item.kind == "changes")
    assert changes_item.change_set_id == result.changes[0].change_set_id
    assert changes_item.document_id == document.document_id
    tool_item = next(item for item in work if item.kind == "tool")
    assert tool_item.tool_name == "propose_project_edits"
    assert tool_item.args_summary and "hunks" in tool_item.args_summary

    sse_types = [event["type"] for event in events]
    assert "work_item_start" in sse_types and "work_item_done" in sse_types
    prompt = json.dumps(runtime.llm.calls[0]["messages"], ensure_ascii=False)
    assert "正在读取" not in prompt and "正在准备修改" not in prompt
    asyncio.run(runtime.close())


def test_chat_project_marks_work_log_failed_and_interrupts_running(tmp_path):
    from tests.test_runtime_project_editing import MultiTurnStreamLLM

    runtime = AgentRuntime(_settings(tmp_path))
    project = runtime.store.create_project("default", "失败工作记录")
    document, _staled = runtime.store.save_document(
        "default", project.project_id, project.entry_document_id,
        "正文", expected_version=1,
    )
    session = runtime.store.create_project_chat_session("default", project.project_id)
    runtime.llm = MultiTurnStreamLLM([RuntimeError("stream down")])

    with pytest.raises(RuntimeError, match="stream down"):
        asyncio.run(runtime.chat_project(
            "default", project.project_id, "失败指令",
            chat_session_id=session.chat_session_id,
            current_document_id=document.document_id,
        ))

    work = runtime.store.list_project_chat_work_events(
        "default", project.project_id, session.chat_session_id
    )
    # 上下文进度项在失败前已完成；无仍在运行的工作项，任务终态为 failed。
    assert [(item.kind, item.status) for item in work][-1] == ("task", "failed")
    progress = [item for item in work if item.kind == "progress"]
    assert progress and all(item.status == "succeeded" for item in progress)
    asyncio.run(runtime.close())


def test_chat_project_work_log_failure_does_not_mask_task_error(tmp_path, monkeypatch):
    """终态落库自身失败只记 warning，不得掩盖原始任务错误（对齐 v1.16 补偿原则）。"""
    from tests.test_runtime_project_editing import MultiTurnStreamLLM

    runtime = AgentRuntime(_settings(tmp_path))
    project = runtime.store.create_project("default", "掩盖错误项目")
    document, _staled = runtime.store.save_document(
        "default", project.project_id, project.entry_document_id,
        "正文", expected_version=1,
    )
    session = runtime.store.create_project_chat_session("default", project.project_id)
    runtime.llm = MultiTurnStreamLLM([RuntimeError("stream down")])
    original_add = runtime.store.add_project_chat_work_event

    def failing_add(*args, **kwargs):
        if kwargs.get("kind") == "task":
            raise sqlite3.OperationalError("disk I/O error")
        return original_add(*args, **kwargs)

    monkeypatch.setattr(runtime.store, "add_project_chat_work_event", failing_add)

    with pytest.raises(RuntimeError, match="stream down"):
        asyncio.run(runtime.chat_project(
            "default", project.project_id, "失败指令",
            chat_session_id=session.chat_session_id,
            current_document_id=document.document_id,
        ))

    work = runtime.store.list_project_chat_work_events(
        "default", project.project_id, session.chat_session_id
    )
    assert all(item.kind != "task" for item in work)
    asyncio.run(runtime.close())


# ---------- API ----------

def test_session_detail_returns_work_events_and_reconciles(tmp_path):
    runtime = AgentRuntime(_settings(tmp_path))
    project = runtime.store.create_project("default", "对账 API 项目")
    session = runtime.store.create_project_chat_session("default", project.project_id)
    user = runtime.store.add_project_chat_message(
        "default", project.project_id, session.chat_session_id, "user", "历史指令"
    )
    _add_event(runtime.store, project, session.chat_session_id,
               task_id="orphan-task", seq=1, user_message_id=user.message_id,
               assistant="default")
    _add_event(runtime.store, project, session.chat_session_id,
               task_id="done-task", seq=1, user_message_id=user.message_id,
               assistant="default")
    _add_event(runtime.store, project, session.chat_session_id,
               task_id="done-task", seq=2, kind="task", user_message_id=user.message_id,
               assistant="default")

    from api.main import create_app
    from api.tasks import TaskRecord

    app = create_app(settings=_settings(tmp_path), runtime=runtime, start_runtime=False)
    with TestClient(app) as client:
        broker = app.state.tasks
        # 直接注入运行中任务记录，避免在测试线程里创建 asyncio 任务。
        active_record = TaskRecord(task_id="active-task", assistant_id="default")
        broker.records["active-task"] = active_record
        _add_event(runtime.store, project, session.chat_session_id,
                   task_id="active-task", seq=1, user_message_id=user.message_id,
                   assistant="default")

        detail = client.get(
            f"/api/projects/{project.project_id}/agent/sessions/{session.chat_session_id}",
            params={"assistant_id": "default"},
        ).json()

        orphan_terminal = [
            item for item in detail["work_events"]
            if item["task_id"] == "orphan-task" and item["kind"] == "task"
        ]
        assert len(orphan_terminal) == 1
        assert orphan_terminal[0]["status"] == "interrupted"
        assert orphan_terminal[0]["event_seq"] == 2
        active_rows = [
            item for item in detail["work_events"] if item["task_id"] == "active-task"
        ]
        assert len(active_rows) == 1  # 运行中的任务不得提前终结

        active_record.status = "done"
        detail = client.get(
            f"/api/projects/{project.project_id}/agent/sessions/{session.chat_session_id}",
            params={"assistant_id": "default"},
        ).json()
        active_terminal = [
            item for item in detail["work_events"]
            if item["task_id"] == "active-task" and item["kind"] == "task"
        ]
        assert len(active_terminal) == 1
        assert active_terminal[0]["status"] == "interrupted"
        assert [item["role"] for item in detail["messages"]] == ["user"]


def test_session_detail_respects_run_lock_for_direct_tasks(tmp_path):
    """无 broker 作用域的直连任务以运行锁 task_id 标识：锁未释放视为仍在运行。"""
    runtime = AgentRuntime(_settings(tmp_path))
    project = runtime.store.create_project("default", "直连锁项目")
    session = runtime.store.create_project_chat_session("default", project.project_id)
    user = runtime.store.add_project_chat_message(
        "default", project.project_id, session.chat_session_id, "user", "直连指令"
    )
    runtime.store.acquire_lock("default", "direct-task-1", 2)
    _add_event(runtime.store, project, session.chat_session_id,
               task_id="direct-task-1", seq=1, user_message_id=user.message_id,
               assistant="default")

    from api.main import create_app

    app = create_app(settings=_settings(tmp_path), runtime=runtime, start_runtime=False)
    with TestClient(app) as client:
        detail = client.get(
            f"/api/projects/{project.project_id}/agent/sessions/{session.chat_session_id}",
            params={"assistant_id": "default"},
        ).json()
        assert not [
            item for item in detail["work_events"]
            if item["task_id"] == "direct-task-1" and item["kind"] == "task"
        ]

        runtime.store.release_lock("default", "direct-task-1")
        detail = client.get(
            f"/api/projects/{project.project_id}/agent/sessions/{session.chat_session_id}",
            params={"assistant_id": "default"},
        ).json()
        terminal = [
            item for item in detail["work_events"]
            if item["task_id"] == "direct-task-1" and item["kind"] == "task"
        ]
        assert len(terminal) == 1
        assert terminal[0]["status"] == "interrupted"


def _settings(tmp_path: Path) -> Settings:
    empty = tmp_path / "empty.json"
    empty.write_text('{"mcpServers": {}}', encoding="utf-8")
    return Settings(
        project_root=tmp_path, data_dir=tmp_path,
        skills_dir=Path(__file__).resolve().parents[1] / "skills",
        mcp_config=empty, openai_api_key="fake", openai_base_url="", model_name="fake",
    )
