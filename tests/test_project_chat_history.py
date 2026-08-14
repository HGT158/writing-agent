"""项目 Agent 多会话历史的 MemoryStore 契约。"""
from __future__ import annotations

import pytest

from memory.errors import ResourceConflictError
from memory.store import AssistantBusyError, MemoryStore


def test_project_chat_sessions_persist_messages_title_order_and_scope(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "会话项目")
    other_project = store.create_project("writer-a", "其他项目")
    first = store.create_project_chat_session("writer-a", project.project_id)
    second = store.create_project_chat_session("writer-a", project.project_id)

    store.add_project_chat_message(
        "writer-a", project.project_id, first.chat_session_id,
        "user", "  第一条需求\n补充说明  ",
    )
    store.add_project_chat_message(
        "writer-a", project.project_id, first.chat_session_id,
        "assistant", "第一条回答",
    )
    store.add_project_chat_message(
        "writer-a", project.project_id, second.chat_session_id,
        "user", "最近会话",
    )

    messages = store.list_project_chat_messages(
        "writer-a", project.project_id, first.chat_session_id
    )
    assert [(item.role, item.content) for item in messages] == [
        ("user", "  第一条需求\n补充说明  "),
        ("assistant", "第一条回答"),
    ]
    sessions = store.list_project_chat_sessions("writer-a", project.project_id)
    assert [item.chat_session_id for item in sessions] == [
        second.chat_session_id, first.chat_session_id,
    ]
    assert sessions[0].title == "最近会话"
    assert sessions[0].message_count == 1
    assert sessions[1].title == "第一条需求"
    assert sessions[1].message_count == 2

    with pytest.raises(KeyError):
        store.get_project_chat_session(
            "writer-b", project.project_id, first.chat_session_id
        )
    with pytest.raises(KeyError):
        store.get_project_chat_session(
            "writer-a", other_project.project_id, first.chat_session_id
        )
    store.close()


def test_project_chat_session_delete_rejects_running_assistant(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "运行中会话")
    session = store.create_project_chat_session("writer-a", project.project_id)
    store.add_project_chat_message(
        "writer-a", project.project_id, session.chat_session_id,
        "user", "正在回答的问题",
    )
    store.acquire_lock("writer-a", "running-chat")

    with pytest.raises(AssistantBusyError, match="running-chat"):
        store.delete_project_chat_session(
            "writer-a", project.project_id, session.chat_session_id
        )

    assert store.get_project_chat_session(
        "writer-a", project.project_id, session.chat_session_id
    ).message_count == 1
    store.release_lock("writer-a", "running-chat")
    store.close()


def test_project_chat_session_delete_blocks_pending_then_removes_settled_history(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "待审会话")
    document = store.get_document(
        "writer-a", project.project_id, project.entry_document_id
    )
    session = store.create_project_chat_session("writer-a", project.project_id)
    store.add_project_chat_message(
        "writer-a", project.project_id, session.chat_session_id,
        "user", "生成首稿",
    )
    change = store.create_change_set(
        "writer-a",
        project.project_id,
        document.document_id,
        source="chat",
        start=0,
        end=0,
        original_text="",
        replacement_text="首稿正文",
        base_version=document.version,
        session_id=session.chat_session_id,
    )

    pending = store.list_pending_chat_changes(
        "writer-a", project.project_id, session.chat_session_id
    )
    assert [item.change_set_id for item in pending] == [change.change_set_id]
    with pytest.raises(ResourceConflictError, match="待处理修改"):
        store.delete_project_chat_session(
            "writer-a", project.project_id, session.chat_session_id
        )

    applied, _ = store.apply_change_set(
        "writer-a",
        project.project_id,
        change.change_set_id,
        expected_version=document.version,
    )
    store.delete_project_chat_session(
        "writer-a", project.project_id, session.chat_session_id
    )

    assert applied.content == "首稿正文"
    with pytest.raises(KeyError):
        store.get_project_chat_session(
            "writer-a", project.project_id, session.chat_session_id
        )
    with pytest.raises(KeyError):
        store.get_change_set("writer-a", project.project_id, change.change_set_id)
    store.close()


def test_context_summary_is_scoped_and_reusable(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "摘要项目")
    other = store.create_project("writer-b", "摘要项目")
    session = store.create_project_chat_session("writer-a", project.project_id)
    other_session = store.create_project_chat_session("writer-b", other.project_id)

    assert store.get_project_chat_summary(
        "writer-a", project.project_id, session.chat_session_id
    ) is None
    store.save_project_chat_summary(
        "writer-a", project.project_id, session.chat_session_id, "第一版摘要", 4
    )
    store.save_project_chat_summary(
        "writer-a", project.project_id, session.chat_session_id, "第二版摘要", 9
    )

    stored = store.get_project_chat_summary(
        "writer-a", project.project_id, session.chat_session_id
    )
    assert stored is not None
    assert (stored.summary, stored.covered_through_message_id) == ("第二版摘要", 9)
    assert store.get_project_chat_summary(
        "writer-b", other.project_id, other_session.chat_session_id
    ) is None
    store.close()


def test_deleting_session_removes_its_context_summary(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "摘要清理")
    session = store.create_project_chat_session("writer-a", project.project_id)
    store.add_project_chat_message(
        "writer-a", project.project_id, session.chat_session_id, "user", "问题"
    )
    store.save_project_chat_summary(
        "writer-a", project.project_id, session.chat_session_id, "会被清理的摘要", 1
    )

    store.delete_project_chat_session(
        "writer-a", project.project_id, session.chat_session_id
    )

    rows = store._conn.execute(
        "SELECT COUNT(*) FROM project_chat_summaries WHERE chat_session_id = ?",
        (session.chat_session_id,),
    ).fetchone()[0]
    assert rows == 0
    store.close()


def test_project_and_assistant_purge_remove_project_chat_history(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "清理项目")
    session = store.create_project_chat_session("writer-a", project.project_id)
    store.add_project_chat_message(
        "writer-a", project.project_id, session.chat_session_id,
        "user", "需要清理",
    )
    store.save_project_chat_summary(
        "writer-a", project.project_id, session.chat_session_id, "项目摘要", 1
    )

    store.purge_project("writer-a", project.project_id)
    assert store._conn.execute(
        "SELECT COUNT(*) FROM project_chat_summaries WHERE project_id = ?",
        (project.project_id,),
    ).fetchone()[0] == 0
    with pytest.raises(KeyError):
        store.get_project_chat_session(
            "writer-a", project.project_id, session.chat_session_id
        )

    second_project = store.create_project("writer-a", "助手清理")
    second_session = store.create_project_chat_session(
        "writer-a", second_project.project_id
    )
    store.add_project_chat_message(
        "writer-a", second_project.project_id, second_session.chat_session_id,
        "user", "助手级清理",
    )
    store.save_project_chat_summary(
        "writer-a", second_project.project_id, second_session.chat_session_id, "助手摘要", 1
    )
    store.purge_assistant("writer-a")
    with pytest.raises(KeyError):
        store.get_project_chat_session(
            "writer-a", second_project.project_id, second_session.chat_session_id
        )
    assert store._conn.execute(
        "SELECT COUNT(*) FROM project_chat_summaries WHERE assistant_id = ?",
        ("writer-a",),
    ).fetchone()[0] == 0
    store.close()
