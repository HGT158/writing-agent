"""文章项目存储：受管目录、版本、导入与隔离。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from memory.store import MemoryStore
from memory import projects as project_storage
from memory import store as store_storage
from memory.errors import ResourceConflictError


def test_create_project_creates_entry_document_under_assistant(tmp_path):
    store = MemoryStore(tmp_path)

    project = store.create_project("writer-a", "长篇小说")
    document = store.get_document(
        "writer-a", project.project_id, project.entry_document_id
    )

    assert project.name == "长篇小说"
    assert document.relative_path == "article.md"
    assert document.content == ""
    assert document.version == 1
    assert (tmp_path / "assistants" / "writer-a" / "projects" / project.project_id / "article.md").exists()
    store.close()


def test_shared_connection_uses_autocommit_without_implicit_transaction_leak(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "事务项目")

    assert store._conn.isolation_level is None
    store._conn.execute(
        "UPDATE projects SET name = ? WHERE assistant_id = ? AND project_id = ?",
        ("已更新", "writer-a", project.project_id),
    )
    assert store._conn.in_transaction is False
    store._conn.execute("BEGIN IMMEDIATE")
    assert store._conn.in_transaction is True
    store._conn.rollback()
    store.close()


def test_same_display_name_creates_distinct_projects_without_overwrite(tmp_path):
    store = MemoryStore(tmp_path)

    first = store.create_project("writer-a", "同名文章")
    second = store.create_project("writer-a", "同名文章")

    assert first.project_id != second.project_id
    assert [item.name for item in store.list_projects("writer-a")] == ["同名文章", "同名文章"]
    store.close()


def test_import_text_file_creates_project_and_copies_original_name(tmp_path):
    store = MemoryStore(tmp_path)

    project = store.import_text_project(
        "writer-a", "chapter-one.md", BytesIO("第一章\n".encode("utf-8"))
    )
    document = store.get_document(
        "writer-a", project.project_id, project.entry_document_id
    )

    assert project.name == "chapter-one"
    assert document.relative_path == "chapter-one.md"
    assert document.content == "第一章\n"
    store.close()


def test_import_folder_preserves_tree_and_marks_text_documents(tmp_path):
    store = MemoryStore(tmp_path)

    project = store.import_folder_project(
        "writer-a",
        "研究稿",
        [
            ("draft.md", BytesIO("正文".encode("utf-8"))),
            ("notes/source.txt", BytesIO("素材".encode("utf-8"))),
            ("images/cover.png", BytesIO(b"\x89PNG\r\n")),
        ],
    )
    tree = store.get_project_tree("writer-a", project.project_id)

    assert [item.relative_path for item in tree] == [
        "draft.md",
        "images/cover.png",
        "notes/source.txt",
    ]
    assert {item.relative_path: item.editable for item in tree} == {
        "draft.md": True,
        "images/cover.png": False,
        "notes/source.txt": True,
    }
    store.close()


@pytest.mark.parametrize("relative_path", ["../escape.md", "/absolute.md", "C:/outside.md"])
def test_import_folder_rejects_paths_outside_project(tmp_path, relative_path):
    store = MemoryStore(tmp_path)

    with pytest.raises(ValueError, match="路径"):
        store.import_folder_project(
            "writer-a", "bad", [(relative_path, BytesIO(b"unsafe"))]
        )

    assert store.list_projects("writer-a") == []
    store.close()


@pytest.mark.parametrize(
    "relative_path",
    ["a<b.txt", "bad|name.md", "question?.txt", "con .txt", "nul .md"],
)
def test_import_folder_rejects_windows_invalid_file_names(tmp_path, relative_path):
    store = MemoryStore(tmp_path)
    with pytest.raises(ValueError, match="路径"):
        store.import_folder_project(
            "writer-a", "bad", [(relative_path, BytesIO(b"unsafe"))]
        )
    store.close()


def test_import_text_project_rejects_non_utf8_content(tmp_path):
    store = MemoryStore(tmp_path)
    with pytest.raises(ValueError, match="UTF-8"):
        store.import_text_project("writer-a", "legacy.txt", BytesIO("正文".encode("gbk")))
    assert store.list_projects("writer-a") == []
    store.close()


def test_save_document_preserves_imported_utf8_bom(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.import_text_project(
        "writer-a", "bom.md", BytesIO(b"\xef\xbb\xbf" + "原文".encode("utf-8"))
    )

    store.save_document(
        "writer-a", project.project_id, project.entry_document_id,
        "新正文", expected_version=1,
    )

    path = tmp_path / "assistants" / "writer-a" / "projects" / project.project_id / "bom.md"
    assert path.read_bytes() == b"\xef\xbb\xbf" + "新正文".encode("utf-8")
    store.close()


def test_save_document_uses_optimistic_version_and_preserves_content_on_conflict(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "版本测试")

    saved, _staled = store.save_document(
        "writer-a",
        project.project_id,
        project.entry_document_id,
        "版本二",
        expected_version=1,
    )

    assert saved.version == 2
    assert saved.content == "版本二"
    with pytest.raises(RuntimeError, match="版本冲突"):
        store.save_document(
            "writer-a",
            project.project_id,
            project.entry_document_id,
            "过期覆盖",
            expected_version=1,
        )
    current = store.get_document("writer-a", project.project_id, project.entry_document_id)
    assert current.content == "版本二"
    assert current.version == 2
    store.close()


def test_concurrent_save_conflict_never_restores_stale_file_content(tmp_path, monkeypatch):
    owner = MemoryStore(tmp_path)
    project = owner.create_project("writer-a", "并发保存")
    document_id = project.entry_document_id
    owner.close()
    barrier = threading.Barrier(2)
    real_replace = project_storage.os.replace

    def synchronized_replace(source, target):
        if str(source).endswith(".tmp"):
            try:
                barrier.wait(timeout=0.5)
            except threading.BrokenBarrierError:
                pass
        return real_replace(source, target)

    monkeypatch.setattr(project_storage.os, "replace", synchronized_replace)

    stores = [MemoryStore(tmp_path), MemoryStore(tmp_path)]

    def save(store: MemoryStore, content: str):
        return store.save_document(
            "writer-a", project.project_id, document_id,
            content, expected_version=1,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(save, store, content): content
            for store, content in zip(stores, ("写入 A", "写入 B"), strict=True)
        }
        successes = []
        failures = []
        for future, content in futures.items():
            try:
                successes.append((content, future.result()))
            except Exception as exc:
                failures.append(exc)
    for store in stores:
        store.close()

    assert len(successes) == 1, failures
    assert len(failures) == 1, failures
    current_store = MemoryStore(tmp_path)
    current = current_store.get_document("writer-a", project.project_id, document_id)
    current_store.close()
    assert current.version == 2
    assert current.content == successes[0][0]


def test_save_document_recovers_committed_write_intent_after_interruption(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "写入恢复")
    document_id = project.entry_document_id
    real_finalize = project_storage._finalize_write_intent
    calls = 0

    def interrupt_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt("模拟文件替换后的进程退出")
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(project_storage, "_finalize_write_intent", interrupt_once)
    with pytest.raises(KeyboardInterrupt):
        store.save_document(
            "writer-a", project.project_id, document_id,
            "恢复后的正文", expected_version=1,
        )
    store.close()

    recovered_store = MemoryStore(tmp_path)
    recovered = recovered_store.get_document("writer-a", project.project_id, document_id)
    recovered_store.close()
    assert recovered.content == "恢复后的正文"
    assert recovered.version == 2


def test_write_intent_pid_reuse_does_not_block_recovery(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "PID 复用恢复")
    document_id = project.entry_document_id
    intent_columns = {
        row[1] for row in store._conn.execute("PRAGMA table_info(document_write_intents)")
    }
    if "owner_started_at" not in intent_columns:
        store._conn.execute(
            "ALTER TABLE document_write_intents ADD COLUMN owner_started_at REAL NOT NULL DEFAULT 0"
        )
    if "claimed_at" not in intent_columns:
        store._conn.execute(
            "ALTER TABLE document_write_intents ADD COLUMN claimed_at TEXT NOT NULL DEFAULT ''"
        )
    now = datetime.now(timezone.utc).isoformat()
    store._conn.execute(
        "INSERT INTO document_write_intents "
        "(intent_id, assistant_id, project_id, document_id, change_set_id, expected_version, "
        "target_version, relative_path, content, owner_pid, created_at, owner_started_at, claimed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "stale-intent", "writer-a", project.project_id, document_id, None, 1, 2,
            "article.md", "恢复正文", 424242, now, 111.0, now,
        ),
    )
    store._conn.commit()

    class ReusedProcess:
        def create_time(self):
            return 222.0

    monkeypatch.setattr(project_storage.psutil, "pid_exists", lambda _pid: True)
    monkeypatch.setattr(project_storage.psutil, "Process", lambda _pid: ReusedProcess())

    recovered = store.get_document("writer-a", project.project_id, document_id)

    assert recovered.content == "恢复正文"
    assert recovered.version == 2
    store.close()


def test_write_intent_file_io_runs_outside_sqlite_write_transaction(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "短事务恢复")
    document_id = project.entry_document_id
    store._conn.execute(
        "INSERT INTO document_write_intents "
        "(intent_id, assistant_id, project_id, document_id, change_set_id, expected_version, "
        "target_version, relative_path, content, owner_pid, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "pending-intent", "writer-a", project.project_id, document_id, None, 1, 2,
            "article.md", "恢复正文", 0, datetime.now(timezone.utc).isoformat(),
        ),
    )
    store._conn.commit()
    real_write = project_storage._write_atomic

    def checked_write(path, content, *args, **kwargs):
        assert not store._conn.in_transaction
        return real_write(path, content, *args, **kwargs)

    monkeypatch.setattr(project_storage, "_write_atomic", checked_write)

    recovered = store.get_document("writer-a", project.project_id, document_id)

    assert recovered.content == "恢复正文"
    assert recovered.version == 2
    store.close()


def test_failed_recovery_keeps_write_intent_for_a_later_retry(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "恢复重试")
    document_id = project.entry_document_id
    store._conn.execute(
        "INSERT INTO document_write_intents "
        "(intent_id, assistant_id, project_id, document_id, change_set_id, expected_version, "
        "target_version, relative_path, content, owner_pid, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "retry-intent", "writer-a", project.project_id, document_id, None, 1, 2,
            "article.md", "最终恢复正文", 0, datetime.now(timezone.utc).isoformat(),
        ),
    )
    store._conn.commit()
    real_write = project_storage._write_atomic
    attempts = 0

    def fail_once(path, content, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("磁盘暂时不可写")
        return real_write(path, content, *args, **kwargs)

    monkeypatch.setattr(project_storage, "_write_atomic", fail_once)

    with pytest.raises(OSError, match="暂时不可写"):
        store.get_document("writer-a", project.project_id, document_id)
    remaining = store._conn.execute(
        "SELECT COUNT(*) FROM document_write_intents WHERE intent_id = ?",
        ("retry-intent",),
    ).fetchone()[0]
    recovered = store.get_document("writer-a", project.project_id, document_id)

    assert remaining == 1
    assert recovered.content == "最终恢复正文"
    assert recovered.version == 2
    store.close()


def test_memory_store_startup_recovers_project_operation_artifacts(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "崩溃恢复")
    root = tmp_path / "assistants" / "writer-a" / "projects" / project.project_id
    purge_staging = root.parent / f".purge-{project.project_id}-crashed"
    os.replace(root, purge_staging)
    orphan_import = root.parent / ".import-orphan1"
    orphan_import.mkdir()
    project_storage._write_project_marker(orphan_import, "writer-a", "orphan1")
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp()
    os.utime(purge_staging, (old, old))
    os.utime(orphan_import, (old, old))
    store.close()

    recovered_store = MemoryStore(tmp_path)

    assert root.is_dir()
    assert not purge_staging.exists()
    assert not orphan_import.exists()
    assert recovered_store.get_document(
        "writer-a", project.project_id, project.entry_document_id
    ).content == ""
    recovered_store.close()


def test_startup_reconciliation_only_deletes_old_validly_marked_orphans(tmp_path):
    store = MemoryStore(tmp_path)
    store.close()
    projects_root = tmp_path / "assistants" / "writer-a" / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)
    old_marked = projects_root / "oldmarked"
    fresh_marked = projects_root / "freshmarked"
    unmarked = projects_root / "unmarked"
    mismatched = projects_root / "mismatched"
    for directory in (old_marked, fresh_marked, unmarked, mismatched):
        directory.mkdir()
    project_storage._write_project_marker(old_marked, "writer-a", "oldmarked")
    project_storage._write_project_marker(fresh_marked, "writer-a", "freshmarked")
    project_storage._write_project_marker(mismatched, "writer-b", "mismatched")
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp()
    for directory in (old_marked, unmarked, mismatched):
        os.utime(directory, (old, old))

    recovered_store = MemoryStore(tmp_path)

    assert not old_marked.exists()
    assert fresh_marked.is_dir()
    assert unmarked.is_dir()
    assert mismatched.is_dir()
    recovered_store.close()


def test_memory_store_startup_rolls_back_half_finished_archive(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "归档崩溃恢复")
    root = tmp_path / "assistants" / "writer-a" / "projects" / project.project_id
    archive_root = tmp_path / "archive" / "projects" / "writer-a"
    archive_root.mkdir(parents=True)
    half_archived = archive_root / f"{project.project_id}-20260810-000000-000000"
    os.replace(root, half_archived)
    store.close()

    recovered_store = MemoryStore(tmp_path)

    assert root.is_dir()
    assert not half_archived.exists()
    assert recovered_store.list_projects("writer-a") == [project]
    recovered_store.close()


def test_change_set_table_enforces_source_and_status_values(tmp_path):
    store = MemoryStore(tmp_path)
    schema = store._conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'change_sets'"
    ).fetchone()[0]

    assert "CHECK" in schema.upper()
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO change_sets VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "invalid", "writer-a", "project", "document", None, "unsafe",
                "task-x", 1, "unknown",
                datetime.now(timezone.utc).isoformat(), None,
            ),
        )
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO change_set_hunks VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "hunk-invalid", "missing-set", 0, 0, 0, "", "", "unknown",
                datetime.now(timezone.utc).isoformat(), None,
            ),
        )
    store.close()


def test_run_lock_pid_reuse_is_reclaimed_without_waiting_for_ttl(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path)
    lock_columns = {
        row[1] for row in store._conn.execute("PRAGMA table_info(run_locks)")
    }
    if "pid_started_at" not in lock_columns:
        store._conn.execute(
            "ALTER TABLE run_locks ADD COLUMN pid_started_at REAL NOT NULL DEFAULT 0"
        )
    store._conn.execute(
        "INSERT INTO run_locks (assistant_id, task_id, pid, acquired_at, pid_started_at) "
        "VALUES (?,?,?,?,?)",
        (
            "writer-a", "stale-task", 424242,
            datetime.now(timezone.utc).isoformat(), 111.0,
        ),
    )
    store._conn.commit()

    class ReusedProcess:
        def create_time(self):
            return 222.0

    monkeypatch.setattr(store_storage.psutil, "pid_exists", lambda _pid: True)
    monkeypatch.setattr(store_storage.psutil, "Process", lambda _pid: ReusedProcess())

    store.acquire_lock("writer-a", "new-task")

    row = store._conn.execute(
        "SELECT task_id FROM run_locks WHERE assistant_id = ?", ("writer-a",)
    ).fetchone()
    assert row == ("new-task",)
    store.close()


def test_legacy_run_lock_is_reclaimed_after_ttl_even_if_pid_is_alive(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path)
    old_at = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    store._conn.execute(
        "INSERT INTO run_locks (assistant_id, task_id, pid, acquired_at, pid_started_at) "
        "VALUES (?,?,?,?,?)",
        ("writer-a", "legacy-task", 424242, old_at, 0),
    )
    store._conn.commit()
    monkeypatch.setattr(store_storage.psutil, "pid_exists", lambda _pid: True)

    assert store.is_locked("writer-a") is False
    store.acquire_lock("writer-a", "new-task")
    assert store._conn.execute(
        "SELECT task_id FROM run_locks WHERE assistant_id = ?", ("writer-a",)
    ).fetchone() == ("new-task",)
    store.close()


def test_change_set_migration_failure_keeps_legacy_table_for_retry(tmp_path):
    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE change_sets ("
        "change_set_id TEXT PRIMARY KEY, assistant_id TEXT NOT NULL, project_id TEXT NOT NULL, "
        "document_id TEXT NOT NULL, session_id TEXT, source TEXT NOT NULL, start_offset INTEGER NOT NULL, "
        "end_offset INTEGER NOT NULL, original_text TEXT NOT NULL, replacement_text TEXT NOT NULL, "
        "base_version INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, applied_at TEXT)"
    )
    conn.execute(
        "INSERT INTO change_sets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("legacy", "writer-a", "project", "document", None, "unsafe", 0, 0,
         "", "", 1, "unknown", datetime.now(timezone.utc).isoformat(), None),
    )
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.IntegrityError):
        MemoryStore(tmp_path).close()

    conn = sqlite3.connect(db_path)
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "change_sets" in tables
    assert "change_sets_legacy" not in tables
    assert conn.execute("SELECT COUNT(*) FROM change_sets").fetchone() == (1,)
    conn.close()


def test_write_intent_insert_race_maps_to_resource_conflict(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "写入竞态")
    document_id = project.entry_document_id

    def inject_intent(conn, data_dir, assistant_id, project_id, document_id=None):
        conn.execute(
            "INSERT INTO document_write_intents "
            "(intent_id, assistant_id, project_id, document_id, expected_version, target_version, relative_path, content, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("racing-intent", assistant_id, project_id, document_id, 1, 2,
             "article.md", "另一个进程的正文", datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    monkeypatch.setattr(project_storage, "_recover_write_intents", inject_intent)
    with pytest.raises(ResourceConflictError):
        store.save_document(
            "writer-a", project.project_id, document_id, "我的正文", expected_version=1,
        )
    store.close()


def test_project_queries_require_owning_assistant(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "私有文章")

    assert store.list_projects("writer-b") == []
    with pytest.raises(KeyError):
        store.get_document("writer-b", project.project_id, project.entry_document_id)

    store.close()


def test_rename_project_changes_display_name_without_changing_identity(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "旧标题")

    renamed = store.rename_project("writer-a", project.project_id, "新标题")

    assert renamed.project_id == project.project_id
    assert renamed.name == "新标题"
    assert store.list_projects("writer-a")[0].name == "新标题"
    store.close()


def test_archive_project_moves_directory_and_hides_project(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "待归档")
    root = (tmp_path / "assistants" / "writer-a" / "projects" / project.project_id)

    archived = store.archive_project("writer-a", project.project_id)

    assert not root.exists()
    assert archived.exists()
    assert store.list_projects("writer-a") == []
    with pytest.raises(KeyError):
        store.get_project_tree("writer-a", project.project_id)
    store.close()


def test_archive_project_rejects_pending_change_set_without_moving_files(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "待确认修改")
    document, _staled = store.save_document(
        "writer-a", project.project_id, project.entry_document_id,
        "原始正文", expected_version=1,
    )
    store.create_selection_change_set(
        "writer-a", project.project_id, document.document_id,
        task_id="task-archive-pending", start=0, end=4, original_text="原始正文",
        replacement_text="修改正文", base_version=document.version,
        source="selection",
    )
    root = tmp_path / "assistants" / "writer-a" / "projects" / project.project_id

    with pytest.raises(RuntimeError, match="待处理"):
        store.archive_project("writer-a", project.project_id)

    assert root.exists()
    assert store.list_projects("writer-a") == [project]
    store.close()


@pytest.mark.parametrize("operation", ["archive", "purge"])
def test_project_archive_and_purge_reject_active_write_intents(
    tmp_path, monkeypatch, operation
):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "写入中的项目")
    root = tmp_path / "assistants" / "writer-a" / "projects" / project.project_id
    store._conn.execute(
        "INSERT INTO document_write_intents "
        "(intent_id, assistant_id, project_id, document_id, change_set_id, hunk_id, "
        "expected_version, target_version, relative_path, content, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"intent-{operation}", "writer-a", project.project_id,
            project.entry_document_id, None, "", 1, 2, "article.md",
            "尚未写完", datetime.now(timezone.utc).isoformat(),
        ),
    )
    monkeypatch.setattr(project_storage, "_owner_is_live", lambda *_args: True)

    with pytest.raises(ResourceConflictError, match="写入"):
        getattr(store, f"{operation}_project")("writer-a", project.project_id)

    assert root.is_dir()
    assert store.list_projects("writer-a") == [project]
    assert store._conn.execute(
        "SELECT COUNT(*) FROM document_write_intents WHERE project_id = ?",
        (project.project_id,),
    ).fetchone()[0] == 1
    store.close()


def test_create_change_set_rejects_mismatched_original_text(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "快照校验")
    document, _staled = store.save_document(
        "writer-a", project.project_id, project.entry_document_id,
        "真实原文", expected_version=1,
    )

    with pytest.raises(RuntimeError, match="原文快照"):
        store.create_selection_change_set(
            "writer-a", project.project_id, document.document_id,
            task_id="task-bad-snapshot", start=0, end=4, original_text="错误原文",
            replacement_text="替换", base_version=document.version,
            source="chat",
        )
    store.close()


def test_archive_project_rejects_when_assistant_is_running(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "运行中项目")
    store.acquire_lock("writer-a", "task-1")

    with pytest.raises(RuntimeError, match="正忙"):
        store.archive_project("writer-a", project.project_id)

    assert store.list_projects("writer-a") == [project]
    store.release_lock("writer-a", "task-1")
    store.close()


def test_purge_project_removes_files_and_all_project_metadata(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "临时项目")
    root = tmp_path / "assistants" / "writer-a" / "projects" / project.project_id

    store.purge_project("writer-a", project.project_id)

    assert not root.exists()
    assert store.list_projects("writer-a") == []
    with pytest.raises(KeyError):
        store.get_project_tree("writer-a", project.project_id)
    store.close()


def test_purge_project_can_remove_an_archived_project(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "归档后清除")
    archived = store.archive_project("writer-a", project.project_id)
    assert archived.exists()

    store.purge_project("writer-a", project.project_id)

    assert not archived.exists()
    assert store.list_projects("writer-a") == []
    store.close()


def test_purge_cleanup_is_idempotent_after_commit(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "幂等清理")
    real_rmtree = project_storage.shutil.rmtree
    calls: list[bool] = []

    def record_rmtree(path, *args, **kwargs):
        calls.append(bool(kwargs.get("ignore_errors")))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(project_storage.shutil, "rmtree", record_rmtree)
    store.purge_project("writer-a", project.project_id)

    assert calls[-1] is True
    assert store.list_projects("writer-a") == []
    store.close()


def test_import_folder_enforces_file_count_and_total_size_limits(tmp_path):
    store = MemoryStore(tmp_path)

    with pytest.raises(ValueError, match="数量"):
        store.import_folder_project(
            "writer-a", "too-many", [("a.txt", BytesIO(b"a")), ("b.txt", BytesIO(b"b"))], max_files=1
        )
    with pytest.raises(ValueError, match="总大小"):
        store.import_folder_project(
            "writer-a", "too-large", [("a.txt", BytesIO(b"1234"))], max_total_bytes=3
        )
    assert store.list_projects("writer-a") == []
    store.close()
