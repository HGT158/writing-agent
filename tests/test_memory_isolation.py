"""记忆隔离红线测试（架构 §8 注 3）：助手 B 的任何 recall/数据不得出现助手 A 的内容。"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from memory.store import AssistantBusyError, MemoryStore


def test_recall_isolated_between_assistants(tmp_path):
    store = MemoryStore(tmp_path)
    store.memorize("tech-writer", "preference", "偏好冷静精确的技术文风")
    store.memorize("tech-writer", "article", "模型蒸馏入门 | data/articles/tech-writer/x.md", session_id="s1")
    store.add_message("tech-writer", "s1", "user", "写一篇关于模型蒸馏的文章")
    store.memorize("marketing", "preference", "偏好短平快的促销文案")

    recalled_a = store.recall("tech-writer", "模型蒸馏")
    assert "冷静精确" in recalled_a
    assert "模型蒸馏入门" in recalled_a

    recalled_b = store.recall("marketing", "模型蒸馏")
    assert "冷静精确" not in recalled_b
    assert "模型蒸馏入门" not in recalled_b
    assert "短平快" in store.recall("marketing", "文案")
    store.close()


def test_profile_files_physically_separated(tmp_path):
    store = MemoryStore(tmp_path)
    store.memorize("tech-writer", "style", "结构先行")
    profile_a = tmp_path / "assistants" / "tech-writer" / "memory" / "profile.md"
    profile_b = tmp_path / "assistants" / "marketing" / "memory" / "profile.md"
    assert profile_a.exists() and "结构先行" in profile_a.read_text(encoding="utf-8")
    assert not profile_b.exists()
    store.close()


def test_run_lock_conflict_and_release(tmp_path):
    store = MemoryStore(tmp_path)
    store.acquire_lock("tech-writer", "task-1", ttl_hours=2)
    with pytest.raises(AssistantBusyError):
        store.acquire_lock("tech-writer", "task-2", ttl_hours=2)
    store.acquire_lock("marketing", "task-3", ttl_hours=2)  # 不同助手互不影响
    store.release_lock("tech-writer", "task-1")
    store.acquire_lock("tech-writer", "task-4", ttl_hours=2)  # 释放后可再获锁
    store.close()


def test_run_lock_race_between_two_connections(tmp_path):
    """P0-2 回归：两个独立连接（模拟两个进程）抢同一把锁，败者得到 AssistantBusyError 而非 IntegrityError。"""
    s1, s2 = MemoryStore(tmp_path), MemoryStore(tmp_path)
    s1.acquire_lock("tech-writer", "task-A", ttl_hours=2)
    with pytest.raises(AssistantBusyError):
        s2.acquire_lock("tech-writer", "task-B", ttl_hours=2)
    s1.close()
    s2.close()


def test_release_lock_only_releases_own_task(tmp_path):
    """P1-3 回归：release 带 task_id 条件，不得误删回收后新持有者的锁。"""
    store = MemoryStore(tmp_path)
    store.acquire_lock("tech-writer", "task-old", ttl_hours=2)
    conn = sqlite3.connect(str(tmp_path / "app.db"))  # 模拟锁易主：换成新持有者
    conn.execute("UPDATE run_locks SET task_id = 'task-new' WHERE assistant_id = 'tech-writer'")
    conn.commit()
    conn.close()
    store.release_lock("tech-writer", "task-old")   # 释放"自己"的锁 → 不得删除新持有者的行
    assert store.is_locked("tech-writer")
    store.release_lock("tech-writer", "task-new")   # 真正的持有者释放 → 成功
    assert not store.is_locked("tech-writer")
    store.close()


def test_run_lock_reclaims_crashed_process(tmp_path):
    """过期 + PID 已死 → 回收；过期 + PID 存活 → 仍拒绝（TTL+PID 双保险）。"""
    store = MemoryStore(tmp_path)
    dead_pid = 999_999_999
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    conn = sqlite3.connect(str(tmp_path / "app.db"))  # 测试侧自开连接，不碰私有实现
    conn.execute(
        "INSERT INTO run_locks (assistant_id, task_id, pid, acquired_at) VALUES (?,?,?,?)",
        ("tech-writer", "crashed-task", dead_pid, old),
    )
    conn.commit()
    conn.close()
    store.acquire_lock("tech-writer", "new-task", ttl_hours=2)  # 回收残留后获锁成功

    conn = sqlite3.connect(str(tmp_path / "app.db"))
    conn.execute(
        "UPDATE run_locks SET task_id = ?, pid = ?, acquired_at = ? WHERE assistant_id = ?",
        ("slow-task", os.getpid(), old, "tech-writer"),
    )
    conn.commit()
    conn.close()
    with pytest.raises(AssistantBusyError, match="仍存活"):
        store.acquire_lock("tech-writer", "another-task", ttl_hours=2)
    store.close()


def test_project_files_and_metadata_are_isolated_between_assistants(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("tech-writer", "隔离项目")
    store.save_document(
        "tech-writer",
        project.project_id,
        project.entry_document_id,
        "只属于科技助手的正文",
        expected_version=1,
    )

    assert store.list_projects("marketing") == []
    with pytest.raises(KeyError):
        store.get_document("marketing", project.project_id, project.entry_document_id)
    store.close()


def test_project_chat_history_isolated_between_assistants(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("tech-writer", "私有会话")
    session = store.create_project_chat_session("tech-writer", project.project_id)
    store.add_project_chat_message(
        "tech-writer", project.project_id, session.chat_session_id,
        "user", "科技助手私有对话",
    )

    with pytest.raises(KeyError):
        store.get_project_chat_session(
            "marketing", project.project_id, session.chat_session_id
        )
    assert store.list_projects("marketing") == []
    store.close()


def test_purge_assistant_rejects_running_lock_and_removes_article_files(tmp_path):
    store = MemoryStore(tmp_path)
    store.create_project("tech-writer", "项目")
    article_dir = tmp_path / "articles" / "tech-writer"
    article_dir.mkdir(parents=True)
    (article_dir / "done.md").write_text("正文", encoding="utf-8")
    store.acquire_lock("tech-writer", "running-task")

    with pytest.raises(AssistantBusyError):
        store.purge_assistant("tech-writer")
    assert article_dir.exists()

    store.release_lock("tech-writer", "running-task")
    store.purge_assistant("tech-writer")
    assert not article_dir.exists()
    store.close()


def test_purge_checkpoint_prefix_treats_underscore_literally(tmp_path):
    store = MemoryStore(tmp_path)
    store.close()
    checkpoint = tmp_path / "checkpoints.db"
    conn = sqlite3.connect(checkpoint)
    conn.executescript("CREATE TABLE checkpoints (thread_id TEXT); CREATE TABLE writes (thread_id TEXT);")
    conn.execute("INSERT INTO checkpoints VALUES (?)", ("my_bot:s1",))
    conn.execute("INSERT INTO checkpoints VALUES (?)", ("myXbot:s1",))
    conn.commit()
    conn.close()

    store = MemoryStore(tmp_path)
    store.purge_assistant("my_bot")
    store.close()
    conn = sqlite3.connect(checkpoint)
    remaining = [row[0] for row in conn.execute("SELECT thread_id FROM checkpoints")]
    conn.close()
    assert remaining == ["myXbot:s1"]
