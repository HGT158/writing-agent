"""文章项目与项目文件的持久化实现。

SQL 和受管文件系统操作都留在 memory 层，由 MemoryStore 注入 assistant_id
并提供业务门面。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import unicodedata
import uuid
import weakref
from codecs import BOM_UTF8
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable

import psutil

from .errors import (
    ChangeSetStateError,
    DocumentWriteBusyError,
    ResourceConflictError,
    StorageRecoveryPendingError,
)
from .validation import is_valid_id, validate_id


PROJECT_DDL = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    assistant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    root_path TEXT NOT NULL,
    entry_document_id TEXT,
    created_at TEXT NOT NULL,
    archived_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_projects_assistant ON projects(assistant_id);
CREATE TABLE IF NOT EXISTS project_documents (
    document_id TEXT PRIMARY KEY,
    assistant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    editable INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(assistant_id, project_id, relative_path)
);
CREATE INDEX IF NOT EXISTS idx_project_documents_owner
    ON project_documents(assistant_id, project_id);
CREATE TABLE IF NOT EXISTS change_sets (
    change_set_id TEXT PRIMARY KEY,
    assistant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    session_id TEXT,
    source TEXT NOT NULL CHECK (source IN ('selection', 'chat')),
    task_id TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'rejected')),
    created_at TEXT NOT NULL,
    applied_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_change_sets_owner
    ON change_sets(assistant_id, project_id, document_id);
CREATE TABLE IF NOT EXISTS change_set_hunks (
    hunk_id TEXT PRIMARY KEY,
    change_set_id TEXT NOT NULL,
    display_order INTEGER NOT NULL,
    range_start INTEGER NOT NULL,
    range_end INTEGER NOT NULL,
    original_text TEXT NOT NULL,
    new_text TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'rejected', 'stale')),
    created_at TEXT NOT NULL,
    applied_at TEXT,
    UNIQUE (change_set_id, display_order)
);
CREATE INDEX IF NOT EXISTS idx_change_set_hunks_set
    ON change_set_hunks(change_set_id, display_order);
CREATE TABLE IF NOT EXISTS document_write_intents (
    intent_id TEXT PRIMARY KEY,
    assistant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    change_set_id TEXT,
    hunk_id TEXT NOT NULL DEFAULT '',
    expected_version INTEGER NOT NULL,
    target_version INTEGER NOT NULL,
    relative_path TEXT NOT NULL,
    content TEXT NOT NULL,
    utf8_bom INTEGER NOT NULL DEFAULT 0,
    owner_pid INTEGER NOT NULL DEFAULT 0,
    owner_started_at REAL NOT NULL DEFAULT 0,
    claimed_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(assistant_id, project_id, document_id)
);
CREATE INDEX IF NOT EXISTS idx_document_write_intents_owner
    ON document_write_intents(assistant_id, project_id, document_id);
"""

_EDITABLE_EXTENSIONS = {".md", ".markdown", ".txt"}
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_WRITE_GUARDS: weakref.WeakValueDictionary[tuple[str, str, str], threading.Lock] = (
    weakref.WeakValueDictionary()
)
_WRITE_GUARDS_LOCK = threading.Lock()
_WRITE_INTENT_TTL = timedelta(hours=2)
_PROJECT_MARKER_NAME = ".writing-agent-project.json"
_PROJECT_MARKER_FORMAT = 1
_ARTIFACT_GRACE_PERIOD = timedelta(minutes=5)


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    assistant_id: str
    name: str
    root_path: str
    entry_document_id: str | None


@dataclass(frozen=True)
class DocumentRecord:
    document_id: str
    project_id: str
    assistant_id: str
    relative_path: str
    version: int
    editable: bool
    content: str | None = None


@dataclass(frozen=True)
class ChangeSetHunkRecord:
    """单个修改片段；范围是 Unicode code point 半开区间（架构 §4.7 v1.20）。"""

    hunk_id: str
    change_set_id: str
    display_order: int
    start: int
    end: int
    original_text: str
    new_text: str
    status: str
    created_at: str = ""
    applied_at: str | None = None


@dataclass(frozen=True)
class ChangeSetRecord:
    change_set_id: str
    assistant_id: str
    project_id: str
    document_id: str
    session_id: str | None
    source: str
    task_id: str
    base_version: int
    status: str
    hunks: list[ChangeSetHunkRecord] = field(default_factory=list)


@dataclass(frozen=True)
class _WriteIntent:
    intent_id: str
    document_id: str
    change_set_id: str | None
    hunk_id: str
    expected_version: int
    target_version: int
    relative_path: str
    content: str
    utf8_bom: bool
    owner_pid: int
    owner_started_at: float
    claimed_at: str
    created_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _document_write_guard(assistant_id: str, project_id: str, document_id: str) -> threading.Lock:
    key = (assistant_id, project_id, document_id)
    with _WRITE_GUARDS_LOCK:
        return _WRITE_GUARDS.setdefault(key, threading.Lock())


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def _safe_relative_path(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.replace("\\", "/"))
    if not normalized or "\x00" in normalized or normalized.startswith("/"):
        raise ValueError(f"路径非法：{value!r}")
    if re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"路径非法：{value!r}")
    parts = PurePosixPath(normalized).parts
    if len(parts) > 64 or not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"路径非法：{value!r}")
    for part in parts:
        if len(part) > 255 or any(char in part for char in '<>:"|?*'):
            raise ValueError(f"路径非法：{value!r}")
        stem = part.split(".", 1)[0].rstrip(" .").upper()
        if not part or part.endswith((" ", ".")) or stem in _WINDOWS_RESERVED:
            raise ValueError(f"路径非法：{value!r}")
    return "/".join(parts)


def _is_editable(relative_path: str) -> bool:
    return Path(relative_path).suffix.lower() in _EDITABLE_EXTENSIONS


def _project_root(data_dir: Path, assistant_id: str, project_id: str) -> Path:
    validate_id(assistant_id, "assistant_id")
    validate_id(project_id, "project_id")
    root = (data_dir / "assistants" / assistant_id / "projects" / project_id).resolve()
    expected_parent = (data_dir / "assistants" / assistant_id / "projects").resolve()
    if root.parent != expected_parent:
        raise ValueError("项目目录越界")
    return root


def _write_project_marker(directory: Path, assistant_id: str, project_id: str) -> None:
    """写入只供残骸对账使用的内部身份标记；业务元数据仍以 SQLite 为准。"""
    marker = directory / _PROJECT_MARKER_NAME
    temporary = marker.with_suffix(f"{marker.suffix}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(
        {
            "format": _PROJECT_MARKER_FORMAT,
            "assistant_id": assistant_id,
            "project_id": project_id,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)


def _has_valid_project_marker(
    directory: Path, assistant_id: str, project_id: str
) -> bool:
    try:
        payload = json.loads(
            (directory / _PROJECT_MARKER_NAME).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return payload == {
        "format": _PROJECT_MARKER_FORMAT,
        "assistant_id": assistant_id,
        "project_id": project_id,
    }


def _artifact_is_recent(path: Path) -> bool:
    try:
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return True
    return datetime.now(timezone.utc) - modified_at < _ARTIFACT_GRACE_PERIOD


def _backfill_registered_project_marker(
    data_dir: Path,
    assistant_id: str,
    project_id: str,
    root_path: Path,
    archived_at: str | None,
) -> None:
    """升级旧项目标记；只向数据库可唯一归属且仍位于受管根下的目录写入。"""
    if not root_path.is_dir():
        return
    resolved = root_path.resolve()
    if archived_at is None:
        if resolved != _project_root(data_dir, assistant_id, project_id):
            return
    else:
        archive_parent = (
            data_dir / "archive" / "projects" / assistant_id
        ).resolve()
        if resolved.parent != archive_parent or not resolved.name.startswith(
            f"{project_id}-"
        ):
            return
    if not _has_valid_project_marker(resolved, assistant_id, project_id):
        _write_project_marker(resolved, assistant_id, project_id)


def recover_project_artifacts(conn: sqlite3.Connection, data_dir: Path) -> None:
    """对账项目级崩溃残骸；只处理受管目录中的确定性路径。"""
    rows = conn.execute(
        "SELECT project_id, assistant_id, root_path, archived_at FROM projects"
    ).fetchall()
    projects_by_id = {
        (assistant_id, project_id): (Path(root_path), archived_at)
        for project_id, assistant_id, root_path, archived_at in rows
    }

    for (assistant_id, project_id), (root_path, archived_at) in projects_by_id.items():
        _backfill_registered_project_marker(
            data_dir, assistant_id, project_id, root_path, archived_at
        )

    def reconcile_purge(staging: Path, assistant_id: str) -> None:
        suffix = staging.name.removeprefix(".purge-")
        project_id = suffix.split("-", 1)[0]
        if (
            not is_valid_id(project_id)
            or _artifact_is_recent(staging)
            or not _has_valid_project_marker(staging, assistant_id, project_id)
        ):
            return
        record = projects_by_id.get((assistant_id, project_id))
        if record is None:
            shutil.rmtree(staging, ignore_errors=True)
            return
        root_path, archived_at = record
        target = (
            root_path
            if archived_at is not None
            else _project_root(data_dir, assistant_id, project_id)
        )
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, target)

    assistants_root = data_dir / "assistants"
    if assistants_root.is_dir():
        for assistant_root in assistants_root.iterdir():
            projects_root = assistant_root / "projects"
            if not projects_root.is_dir():
                continue
            assistant_id = assistant_root.name
            for child in list(projects_root.iterdir()):
                if child.name.startswith(".import-"):
                    project_id = child.name.removeprefix(".import-")
                    if (
                        child.is_dir()
                        and is_valid_id(project_id)
                        and not _artifact_is_recent(child)
                        and _has_valid_project_marker(child, assistant_id, project_id)
                    ):
                        shutil.rmtree(child, ignore_errors=True)
                elif child.name.startswith(".purge-"):
                    reconcile_purge(child, assistant_id)
                elif child.is_dir() and is_valid_id(child.name):
                    if (
                        (assistant_id, child.name) not in projects_by_id
                        and not _artifact_is_recent(child)
                        and _has_valid_project_marker(child, assistant_id, child.name)
                    ):
                        shutil.rmtree(child, ignore_errors=True)

    archive_projects_root = data_dir / "archive" / "projects"
    if archive_projects_root.is_dir():
        for assistant_root in archive_projects_root.iterdir():
            if not assistant_root.is_dir():
                continue
            for child in list(assistant_root.iterdir()):
                if child.name.startswith(".purge-"):
                    reconcile_purge(child, assistant_root.name)

    for (assistant_id, project_id), (root_path, archived_at) in projects_by_id.items():
        if archived_at is not None or root_path.exists():
            continue
        archive_root = data_dir / "archive" / "projects" / assistant_id
        candidates = (
            [item for item in archive_root.glob(f"{project_id}-*") if item.is_dir()]
            if archive_root.is_dir()
            else []
        )
        if len(candidates) == 1:
            root_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(candidates[0], root_path)


def create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(PROJECT_DDL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(document_write_intents)")}
    additions = {
        "utf8_bom": "INTEGER NOT NULL DEFAULT 0",
        "owner_pid": "INTEGER NOT NULL DEFAULT 0",
        "owner_started_at": "REAL NOT NULL DEFAULT 0",
        "claimed_at": "TEXT NOT NULL DEFAULT ''",
        "hunk_id": "TEXT NOT NULL DEFAULT ''",
    }
    for name, declaration in additions.items():
        if name not in columns:
            conn.execute(
                f"ALTER TABLE document_write_intents ADD COLUMN {name} {declaration}"
            )
    conn.commit()
    change_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(change_sets)")
    }
    if "task_id" not in change_columns:
        _migrate_change_sets_to_hunks(conn)
    else:
        index_columns = [
            row[2] for row in conn.execute(
                "PRAGMA index_info(idx_change_sets_task_document)"
            )
        ]
        if index_columns != ["assistant_id", "task_id", "document_id"]:
            conn.execute("DROP INDEX IF EXISTS idx_change_sets_task_document")
            conn.execute(
                "CREATE UNIQUE INDEX idx_change_sets_task_document "
                "ON change_sets(assistant_id, task_id, document_id)"
            )
    conn.commit()


def _migrate_change_sets_to_hunks(conn: sqlite3.Connection) -> None:
    """v1.20 拆表迁移：单范围 change_sets → 父级 + 单 hunk，任一步失败整体回滚。

    历史记录没有任务 id，生成确定性合成值 `legacy-<change_set_id>`，避免
    `(task_id, document_id)` 唯一索引把 NULL 视为互不冲突。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP INDEX IF EXISTS idx_change_sets_owner")
        conn.execute("DROP INDEX IF EXISTS idx_change_sets_task_document")
        conn.execute("ALTER TABLE change_sets RENAME TO change_sets_legacy")
        conn.execute(
            """CREATE TABLE change_sets (
                change_set_id TEXT PRIMARY KEY,
                assistant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                session_id TEXT,
                source TEXT NOT NULL CHECK (source IN ('selection', 'chat')),
                task_id TEXT NOT NULL,
                base_version INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'rejected')),
                created_at TEXT NOT NULL,
                applied_at TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO change_sets (change_set_id, assistant_id, project_id, document_id, "
            "session_id, source, task_id, base_version, status, created_at, applied_at) "
            "SELECT change_set_id, assistant_id, project_id, document_id, session_id, source, "
            "'legacy-' || change_set_id, base_version, status, created_at, applied_at "
            "FROM change_sets_legacy"
        )
        conn.execute(
            "INSERT INTO change_set_hunks (hunk_id, change_set_id, display_order, "
            "range_start, range_end, original_text, new_text, status, created_at, applied_at) "
            "SELECT change_set_id || '-h0', change_set_id, 0, start_offset, end_offset, "
            "original_text, replacement_text, status, created_at, applied_at "
            "FROM change_sets_legacy"
        )
        conn.execute("DROP TABLE change_sets_legacy")
        conn.execute(
            "CREATE UNIQUE INDEX idx_change_sets_task_document "
            "ON change_sets(assistant_id, task_id, document_id)"
        )
        conn.execute(
            "CREATE INDEX idx_change_sets_owner "
            "ON change_sets(assistant_id, project_id, document_id)"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _row_to_project(row: tuple) -> ProjectRecord:
    return ProjectRecord(
        project_id=row[0], assistant_id=row[1], name=row[2],
        root_path=row[3], entry_document_id=row[4],
    )


def _row_to_document(row: tuple, content: str | None = None) -> DocumentRecord:
    return DocumentRecord(
        document_id=row[0], assistant_id=row[1], project_id=row[2],
        relative_path=row[3], version=row[4], editable=bool(row[5]), content=content,
    )


def _project_row(conn: sqlite3.Connection, assistant_id: str, project_id: str) -> tuple:
    row = conn.execute(
        "SELECT project_id, assistant_id, name, root_path, entry_document_id "
        "FROM projects WHERE assistant_id = ? AND project_id = ? AND archived_at IS NULL",
        (assistant_id, project_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"项目不存在：{project_id}")
    return row


def _project_row_any(conn: sqlite3.Connection, assistant_id: str, project_id: str) -> tuple:
    row = conn.execute(
        "SELECT project_id, assistant_id, name, root_path, entry_document_id, archived_at "
        "FROM projects WHERE assistant_id = ? AND project_id = ?",
        (assistant_id, project_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"项目不存在：{project_id}")
    return row


def _document_row(
    conn: sqlite3.Connection, assistant_id: str, project_id: str, document_id: str
) -> tuple:
    _project_row(conn, assistant_id, project_id)
    row = conn.execute(
        "SELECT document_id, assistant_id, project_id, relative_path, version, editable "
        "FROM project_documents WHERE assistant_id = ? AND project_id = ? AND document_id = ?",
        (assistant_id, project_id, document_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"文档不存在：{document_id}")
    return row


def _document_path(
    data_dir: Path, assistant_id: str, project_id: str, relative_path: str
) -> Path:
    relative = _safe_relative_path(relative_path)
    path = (_project_root(data_dir, assistant_id, project_id) / Path(relative)).resolve()
    root = _project_root(data_dir, assistant_id, project_id)
    if root not in path.parents:
        raise ValueError("项目文件路径越界")
    return path


def _load_document(
    conn: sqlite3.Connection, data_dir: Path, assistant_id: str,
    project_id: str, document_id: str,
) -> DocumentRecord:
    row = _document_row(conn, assistant_id, project_id, document_id)
    document = _row_to_document(row)
    path = _document_path(data_dir, assistant_id, project_id, document.relative_path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"项目文件不存在：{document.relative_path}")
    content = path.read_bytes().decode("utf-8-sig") if document.editable else None
    return _row_to_document(row, content)


def _write_atomic(path: Path, content: str, *, utf8_bom: bool = False) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        payload = content.encode("utf-8")
        temporary.write_bytes((BOM_UTF8 + payload) if utf8_bom else payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _process_started_at(pid: int) -> float:
    try:
        return float(psutil.Process(pid).create_time())
    except (psutil.Error, OSError):
        return 0.0


def _timestamp_expired(value: str) -> bool:
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return datetime.now(timezone.utc) - timestamp > _WRITE_INTENT_TTL


def _owner_is_live(
    owner_pid: int,
    owner_started_at: float,
    claimed_at: str,
    created_at: str,
) -> bool:
    if not owner_pid or owner_pid == os.getpid() or not psutil.pid_exists(owner_pid):
        return False
    if owner_started_at:
        actual_started_at = _process_started_at(owner_pid)
        if actual_started_at:
            return abs(actual_started_at - owner_started_at) < 1.0
    return not _timestamp_expired(claimed_at or created_at)


def _write_intent_row(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    intent_id: str,
) -> _WriteIntent | None:
    row = conn.execute(
        "SELECT intent_id, document_id, change_set_id, hunk_id, expected_version, target_version, "
        "relative_path, content, utf8_bom, owner_pid, owner_started_at, claimed_at, created_at "
        "FROM document_write_intents "
        "WHERE assistant_id = ? AND project_id = ? AND intent_id = ?",
        (assistant_id, project_id, intent_id),
    ).fetchone()
    return _WriteIntent(*row) if row is not None else None


def _claim_write_intent(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    intent_id: str,
) -> _WriteIntent | None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        intent = _write_intent_row(conn, assistant_id, project_id, intent_id)
        if intent is None:
            conn.commit()
            return None
        if _owner_is_live(
            intent.owner_pid,
            intent.owner_started_at,
            intent.claimed_at,
            intent.created_at,
        ):
            raise DocumentWriteBusyError("文档正在被其他进程写入")
        started_at = _process_started_at(os.getpid())
        claimed_at = _now()
        conn.execute(
            "UPDATE document_write_intents SET owner_pid = ?, owner_started_at = ?, claimed_at = ? "
            "WHERE assistant_id = ? AND project_id = ? AND intent_id = ?",
            (os.getpid(), started_at, claimed_at, assistant_id, project_id, intent_id),
        )
        conn.commit()
        return _WriteIntent(
            intent.intent_id,
            intent.document_id,
            intent.change_set_id,
            intent.hunk_id,
            intent.expected_version,
            intent.target_version,
            intent.relative_path,
            intent.content,
            intent.utf8_bom,
            os.getpid(),
            started_at,
            claimed_at,
            intent.created_at,
        )
    except Exception:
        conn.rollback()
        raise


def _finalize_write_intent(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    intent: _WriteIntent,
) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        current_intent = _write_intent_row(
            conn, assistant_id, project_id, intent.intent_id
        )
        if current_intent is None:
            conn.commit()
            return
        if (
            current_intent.owner_pid != intent.owner_pid
            or current_intent.owner_started_at != intent.owner_started_at
            or current_intent.claimed_at != intent.claimed_at
        ):
            raise DocumentWriteBusyError("文档写入意图已由其他进程接管")
        version_row = conn.execute(
            "SELECT version FROM project_documents "
            "WHERE assistant_id = ? AND project_id = ? AND document_id = ?",
            (assistant_id, project_id, intent.document_id),
        ).fetchone()
        if version_row is None:
            raise KeyError(f"文档不存在：{intent.document_id}")
        current_version = version_row[0]
        if current_version == intent.expected_version:
            cursor = conn.execute(
                "UPDATE project_documents SET version = ?, updated_at = ? "
                "WHERE assistant_id = ? AND project_id = ? AND document_id = ? AND version = ?",
                (
                    intent.target_version,
                    _now(),
                    assistant_id,
                    project_id,
                    intent.document_id,
                    intent.expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ResourceConflictError("版本冲突")
        elif current_version != intent.target_version:
            raise ResourceConflictError("写入意图版本冲突")
        staled: list[str] = []
        if intent.change_set_id is not None:
            now = _now()
            if intent.hunk_id:
                hunk_cursor = conn.execute(
                    "UPDATE change_set_hunks SET status = 'applied', applied_at = ? "
                    "WHERE change_set_id = ? AND hunk_id = ? AND status = 'pending'",
                    (now, intent.change_set_id, intent.hunk_id),
                )
                if hunk_cursor.rowcount != 1:
                    status_row = conn.execute(
                        "SELECT status FROM change_set_hunks WHERE hunk_id = ?",
                        (intent.hunk_id,),
                    ).fetchone()
                    if status_row is None or status_row[0] != "applied":
                        raise ResourceConflictError("change set 已处理")
            else:
                conn.execute(
                    "UPDATE change_set_hunks SET status = 'applied', applied_at = ? "
                    "WHERE change_set_id = ? AND status = 'pending'",
                    (now, intent.change_set_id),
                )
            _refresh_change_set_status(conn, intent.change_set_id)
            # 其他任务的建议整组失效；同组其余 hunk 保留内容复检机会。
            staled = _stale_outdated_hunks(
                conn, assistant_id, project_id, intent.document_id,
                exclude_change_set_id=intent.change_set_id,
                below_version=intent.target_version,
            )
        conn.execute(
            "DELETE FROM document_write_intents "
            "WHERE assistant_id = ? AND project_id = ? AND intent_id = ?",
            (assistant_id, project_id, intent.intent_id),
        )
        conn.commit()
        return staled
    except Exception:
        conn.rollback()
        raise


def _discard_failed_write_intent(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    intent: _WriteIntent,
) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM document_write_intents "
            "WHERE assistant_id = ? AND project_id = ? AND intent_id = ? "
            "AND owner_pid = ? AND owner_started_at = ? AND claimed_at = ?",
            (
                assistant_id,
                project_id,
                intent.intent_id,
                intent.owner_pid,
                intent.owner_started_at,
                intent.claimed_at,
            ),
        )
        conn.commit()
    except Exception as cleanup_error:
        conn.rollback()
        raise StorageRecoveryPendingError(
            "文档写入失败且清理受阻，写入意图将在后续操作中恢复"
        ) from cleanup_error


def _release_write_intent_claim(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    intent: _WriteIntent,
) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE document_write_intents "
            "SET owner_pid = 0, owner_started_at = 0, claimed_at = '' "
            "WHERE assistant_id = ? AND project_id = ? AND intent_id = ? "
            "AND owner_pid = ? AND owner_started_at = ? AND claimed_at = ?",
            (
                assistant_id,
                project_id,
                intent.intent_id,
                intent.owner_pid,
                intent.owner_started_at,
                intent.claimed_at,
            ),
        )
        conn.commit()
    except Exception as cleanup_error:
        conn.rollback()
        raise StorageRecoveryPendingError(
            "文档恢复失败且写入意图认领释放受阻，后续操作将继续恢复"
        ) from cleanup_error


def _recover_one_write_intent(
    conn: sqlite3.Connection,
    data_dir: Path,
    assistant_id: str,
    project_id: str,
    intent_id: str,
) -> None:
    intent = _claim_write_intent(conn, assistant_id, project_id, intent_id)
    if intent is None:
        return
    path = _document_path(data_dir, assistant_id, project_id, intent.relative_path)
    try:
        _write_atomic(path, intent.content, utf8_bom=intent.utf8_bom)
    except Exception:
        _release_write_intent_claim(conn, assistant_id, project_id, intent)
        raise
    _finalize_write_intent(conn, assistant_id, project_id, intent)


def _recover_write_intents(
    conn: sqlite3.Connection,
    data_dir: Path,
    assistant_id: str,
    project_id: str,
    document_id: str | None = None,
) -> None:
    sql = (
        "SELECT intent_id FROM document_write_intents "
        "WHERE assistant_id = ? AND project_id = ?"
    )
    params: tuple[object, ...] = (assistant_id, project_id)
    if document_id is not None:
        sql += " AND document_id = ?"
        params += (document_id,)
    intent_ids = [row[0] for row in conn.execute(sql, params).fetchall()]
    for intent_id in intent_ids:
        _recover_one_write_intent(
            conn, data_dir, assistant_id, project_id, intent_id
        )


def create_project(conn: sqlite3.Connection, data_dir: Path, assistant_id: str, name: str) -> ProjectRecord:
    validate_id(assistant_id, "assistant_id")
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("项目名称不能为空")
    project_id = _new_id()
    root = _project_root(data_dir, assistant_id, project_id)
    root.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir()
    _write_project_marker(root, assistant_id, project_id)
    entry = root / "article.md"
    entry.write_text("", encoding="utf-8")
    now = _now()
    document_id = _new_id()
    try:
        conn.execute(
            "INSERT INTO projects VALUES (?,?,?,?,?,?,NULL)",
            (project_id, assistant_id, clean_name, str(root), document_id, now),
        )
        conn.execute(
            "INSERT INTO project_documents VALUES (?,?,?,?,?,?,?,?)",
            (document_id, assistant_id, project_id, "article.md", 1, 1, now, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        shutil.rmtree(root, ignore_errors=True)
        raise
    return ProjectRecord(project_id, assistant_id, clean_name, str(root), document_id)


def list_projects(conn: sqlite3.Connection, assistant_id: str) -> list[ProjectRecord]:
    rows = conn.execute(
        "SELECT project_id, assistant_id, name, root_path, entry_document_id "
        "FROM projects WHERE assistant_id = ? AND archived_at IS NULL ORDER BY created_at, project_id",
        (assistant_id,),
    ).fetchall()
    return [_row_to_project(row) for row in rows]


def rename_project(
    conn: sqlite3.Connection, assistant_id: str, project_id: str, name: str
) -> ProjectRecord:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("项目名称不能为空")
    _project_row(conn, assistant_id, project_id)
    conn.execute(
        "UPDATE projects SET name = ? WHERE assistant_id = ? AND project_id = ? AND archived_at IS NULL",
        (clean_name, assistant_id, project_id),
    )
    conn.commit()
    return _row_to_project(_project_row(conn, assistant_id, project_id))


_EDITABLE_EXTENSIONS = (".md", ".markdown", ".txt")


def _reject_document_mutation(
    conn: sqlite3.Connection, assistant_id: str, project_id: str, document_id: str
) -> None:
    pending = conn.execute(
        "SELECT 1 FROM change_sets "
        "WHERE assistant_id = ? AND project_id = ? AND document_id = ? AND status = 'pending' LIMIT 1",
        (assistant_id, project_id, document_id),
    ).fetchone()
    if pending is not None:
        raise ResourceConflictError("文档存在待处理修改建议，拒绝操作")
    busy = conn.execute(
        "SELECT 1 FROM document_write_intents "
        "WHERE assistant_id = ? AND project_id = ? AND document_id = ? LIMIT 1",
        (assistant_id, project_id, document_id),
    ).fetchone()
    if busy is not None:
        raise DocumentWriteBusyError("文档正在被写入，稍后再试")


def rename_document(
    conn: sqlite3.Connection, data_dir: Path, assistant_id: str, project_id: str,
    document_id: str, new_path: str,
) -> DocumentRecord:
    """重命名可编辑文档：磁盘改名先行、元数据随后、失败回滚（对齐项目归档模式）。"""
    document = _row_to_document(_document_row(conn, assistant_id, project_id, document_id))
    if not document.editable:
        raise ValueError("只读文档不支持重命名")
    normalized = _safe_relative_path(new_path)
    if PurePosixPath(normalized).suffix.lower() not in _EDITABLE_EXTENSIONS:
        raise ValueError("可编辑文档的重命名仅支持 .md/.markdown/.txt 扩展名")
    if normalized == document.relative_path:
        return document
    collision = conn.execute(
        "SELECT 1 FROM project_documents "
        "WHERE assistant_id = ? AND project_id = ? AND relative_path = ?",
        (assistant_id, project_id, normalized),
    ).fetchone()
    if collision is not None:
        raise ResourceConflictError("目标路径已被项目内其他文档占用")
    _recover_write_intents(conn, data_dir, assistant_id, project_id, document_id)
    _reject_document_mutation(conn, assistant_id, project_id, document_id)

    old_file = _document_path(data_dir, assistant_id, project_id, document.relative_path)
    if not old_file.exists() or not old_file.is_file():
        raise FileNotFoundError(f"项目文件不存在：{document.relative_path}")
    new_file = _document_path(data_dir, assistant_id, project_id, normalized)
    new_file.parent.mkdir(parents=True, exist_ok=True)
    if new_file.exists():
        raise ResourceConflictError("目标路径已被项目内其他文件占用")
    os.rename(old_file, new_file)
    try:
        conn.execute(
            "UPDATE project_documents SET relative_path = ? "
            "WHERE assistant_id = ? AND project_id = ? AND document_id = ?",
            (normalized, assistant_id, project_id, document_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        os.rename(new_file, old_file)
        raise
    return _row_to_document(_document_row(conn, assistant_id, project_id, document_id))


def delete_document(
    conn: sqlite3.Connection, data_dir: Path, assistant_id: str, project_id: str,
    document_id: str,
) -> dict:
    """删除可编辑文档：物理文件与元数据行同删，入口文档删除时改指向其余可编辑文档。"""
    document = _row_to_document(_document_row(conn, assistant_id, project_id, document_id))
    if not document.editable:
        raise ValueError("只读文档不支持删除")
    _recover_write_intents(conn, data_dir, assistant_id, project_id, document_id)
    _reject_document_mutation(conn, assistant_id, project_id, document_id)

    entry_row = conn.execute(
        "SELECT entry_document_id FROM projects "
        "WHERE assistant_id = ? AND project_id = ? AND archived_at IS NULL",
        (assistant_id, project_id),
    ).fetchone()
    is_entry = entry_row is not None and entry_row[0] == document_id
    next_entry_row = conn.execute(
        "SELECT document_id FROM project_documents "
        "WHERE assistant_id = ? AND project_id = ? AND document_id != ? AND editable = 1 "
        "ORDER BY relative_path LIMIT 1",
        (assistant_id, project_id, document_id),
    ).fetchone()
    next_entry = next_entry_row[0] if next_entry_row is not None else None
    final_entry = next_entry if is_entry else (entry_row[0] if entry_row is not None else None)

    target = _document_path(data_dir, assistant_id, project_id, document.relative_path)
    payload = target.read_bytes() if target.exists() else None
    if target.exists():
        target.unlink()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM change_set_hunks WHERE change_set_id IN ("
            "SELECT change_set_id FROM change_sets "
            "WHERE assistant_id = ? AND project_id = ? AND document_id = ?)",
            (assistant_id, project_id, document_id),
        )
        conn.execute(
            "DELETE FROM change_sets "
            "WHERE assistant_id = ? AND project_id = ? AND document_id = ?",
            (assistant_id, project_id, document_id),
        )
        conn.execute(
            "DELETE FROM project_documents "
            "WHERE assistant_id = ? AND project_id = ? AND document_id = ?",
            (assistant_id, project_id, document_id),
        )
        if is_entry:
            conn.execute(
                "UPDATE projects SET entry_document_id = ? "
                "WHERE assistant_id = ? AND project_id = ?",
                (next_entry, assistant_id, project_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        if payload is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        raise
    return {"deleted": True, "entry_document_id": final_entry}


def archive_project(
    conn: sqlite3.Connection, data_dir: Path, assistant_id: str, project_id: str
) -> Path:
    project = _row_to_project(_project_row(conn, assistant_id, project_id))
    _recover_write_intents(conn, data_dir, assistant_id, project_id)
    _reject_pending_change_sets(conn, assistant_id, project_id)
    _reject_project_write_intents(conn, assistant_id, project_id)
    source = _project_root(data_dir, assistant_id, project_id)
    archive_root = data_dir / "archive" / "projects" / assistant_id
    archive_root.mkdir(parents=True, exist_ok=True)
    target = archive_root / f"{project_id}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}"
    shutil.move(str(source), str(target))
    try:
        conn.execute(
            "UPDATE projects SET archived_at = ?, root_path = ? "
            "WHERE assistant_id = ? AND project_id = ? AND archived_at IS NULL",
            (_now(), str(target), assistant_id, project_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        shutil.move(str(target), str(source))
        raise
    return target


def _reject_pending_change_sets(
    conn: sqlite3.Connection, assistant_id: str, project_id: str
) -> None:
    pending = conn.execute(
        "SELECT 1 FROM change_sets "
        "WHERE assistant_id = ? AND project_id = ? AND status = 'pending' LIMIT 1",
        (assistant_id, project_id),
    ).fetchone()
    if pending is not None:
        raise ResourceConflictError("项目存在待处理 change set，拒绝归档")


def _reject_project_write_intents(
    conn: sqlite3.Connection, assistant_id: str, project_id: str
) -> None:
    busy = conn.execute(
        "SELECT 1 FROM document_write_intents "
        "WHERE assistant_id = ? AND project_id = ? LIMIT 1",
        (assistant_id, project_id),
    ).fetchone()
    if busy is not None:
        raise DocumentWriteBusyError("项目内文档正在被写入，稍后再试")


def purge_project(
    conn: sqlite3.Connection, data_dir: Path, assistant_id: str, project_id: str
) -> None:
    project = _project_row_any(conn, assistant_id, project_id)
    archived = project[5] is not None
    if not archived:
        _recover_write_intents(conn, data_dir, assistant_id, project_id)
        _reject_pending_change_sets(conn, assistant_id, project_id)
    _reject_project_write_intents(conn, assistant_id, project_id)
    source = Path(project[3]) if archived else _project_root(data_dir, assistant_id, project_id)
    staging = source.parent / f".purge-{project_id}-{uuid.uuid4().hex}"
    # rename 会保留原目录 mtime；先刷新时间，确保并发启动对账把该 staging
    # 视为正在操作中的新近目录，而不会在 purge 提交前擅自移回。
    os.utime(source, None)
    os.replace(source, staging)
    try:
        conn.execute(
            "DELETE FROM project_chat_messages WHERE assistant_id = ? AND project_id = ?",
            (assistant_id, project_id),
        )
        conn.execute(
            "DELETE FROM project_chat_summaries WHERE assistant_id = ? AND project_id = ?",
            (assistant_id, project_id),
        )
        conn.execute(
            "DELETE FROM project_chat_work_events WHERE assistant_id = ? AND project_id = ?",
            (assistant_id, project_id),
        )
        conn.execute(
            "DELETE FROM project_chat_sessions WHERE assistant_id = ? AND project_id = ?",
            (assistant_id, project_id),
        )
        conn.execute(
            "DELETE FROM document_write_intents WHERE assistant_id = ? AND project_id = ?",
            (assistant_id, project_id),
        )
        conn.execute(
            "DELETE FROM change_set_hunks WHERE change_set_id IN ("
            "SELECT change_set_id FROM change_sets WHERE assistant_id = ? AND project_id = ?)",
            (assistant_id, project_id),
        )
        conn.execute(
            "DELETE FROM change_sets WHERE assistant_id = ? AND project_id = ?",
            (assistant_id, project_id),
        )
        conn.execute(
            "DELETE FROM project_documents WHERE assistant_id = ? AND project_id = ?",
            (assistant_id, project_id),
        )
        conn.execute(
            "DELETE FROM projects WHERE assistant_id = ? AND project_id = ?",
            (assistant_id, project_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        os.replace(staging, source)
        raise
    # 提交后目录已在逻辑上被清除；并发对账或外部清理使 staging 不存在时
    # 也不应把成功的 purge 误报为失败。
    shutil.rmtree(staging, ignore_errors=True)


def delete_assistant_rows(conn: sqlite3.Connection, assistant_id: str) -> None:
    """物理清除助手时删除项目元数据，顺序保持子记录先于项目。"""
    conn.execute(
        "DELETE FROM change_set_hunks WHERE change_set_id IN ("
        "SELECT change_set_id FROM change_sets WHERE assistant_id = ?)",
        (assistant_id,),
    )
    for table in ("document_write_intents", "change_sets", "project_documents", "projects"):
        conn.execute(f"DELETE FROM {table} WHERE assistant_id = ?", (assistant_id,))
    conn.commit()


def get_document(
    conn: sqlite3.Connection, data_dir: Path, assistant_id: str, project_id: str, document_id: str
) -> DocumentRecord:
    _recover_write_intents(conn, data_dir, assistant_id, project_id, document_id)
    document = _load_document(conn, data_dir, assistant_id, project_id, document_id)
    if conn.execute(
        "SELECT 1 FROM document_write_intents "
        "WHERE assistant_id = ? AND project_id = ? AND document_id = ?",
        (assistant_id, project_id, document_id),
    ).fetchone() is not None:
        _recover_write_intents(conn, data_dir, assistant_id, project_id, document_id)
        document = _load_document(conn, data_dir, assistant_id, project_id, document_id)
    return document


def get_project_tree(conn: sqlite3.Connection, assistant_id: str, project_id: str) -> list[DocumentRecord]:
    _project_row(conn, assistant_id, project_id)
    rows = conn.execute(
        "SELECT document_id, assistant_id, project_id, relative_path, version, editable "
        "FROM project_documents WHERE assistant_id = ? AND project_id = ? ORDER BY relative_path",
        (assistant_id, project_id),
    ).fetchall()
    return [_row_to_document(row) for row in rows]


def _read_stream(stream: BinaryIO, limit: int) -> bytes:
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("导入文件超过大小限制")
    return data


def _import_project(
    conn: sqlite3.Connection,
    data_dir: Path,
    assistant_id: str,
    name: str,
    files: Iterable[tuple[str, BinaryIO]],
    *,
    max_files: int = 5000,
    max_total_bytes: int = 512 * 1024 * 1024,
    max_file_bytes: int = 100 * 1024 * 1024,
) -> ProjectRecord:
    validate_id(assistant_id, "assistant_id")
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("项目名称不能为空")
    project_id = _new_id()
    root = _project_root(data_dir, assistant_id, project_id)
    staging = root.parent / f".import-{project_id}"
    staging.mkdir(parents=True, exist_ok=False)
    entries: list[tuple[str, bool]] = []
    path_identities: set[str] = set()
    total = 0
    try:
        _write_project_marker(staging, assistant_id, project_id)
        for index, (raw_path, stream) in enumerate(files, start=1):
            if index > max_files:
                raise ValueError("导入文件数量超过限制")
            relative = _safe_relative_path(raw_path)
            identity = unicodedata.normalize("NFC", relative).casefold()
            if identity in path_identities:
                raise ValueError(f"导入路径重复：{relative}")
            path_identities.add(identity)
            data = _read_stream(stream, max_file_bytes)
            if _is_editable(relative):
                try:
                    data.decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    raise ValueError("导入文本必须是 UTF-8 编码") from exc
            total += len(data)
            if total > max_total_bytes:
                raise ValueError("导入总大小超过限制")
            target = staging / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            entries.append((relative, _is_editable(relative)))
        if not entries:
            raise ValueError("导入内容不能为空")
        root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, root)
        now = _now()
        document_rows = [(_new_id(), relative, editable) for relative, editable in entries]
        entry_row = next((row for row in document_rows if row[1].lower().endswith((".md", ".markdown", ".txt"))), None)
        entry_document_id = entry_row[0] if entry_row else None
        conn.execute(
            "INSERT INTO projects VALUES (?,?,?,?,?,?,NULL)",
            (project_id, assistant_id, clean_name, str(root), entry_document_id, now),
        )
        for document_id, relative, editable in document_rows:
            conn.execute(
                "INSERT INTO project_documents VALUES (?,?,?,?,?,?,?,?)",
                (document_id, assistant_id, project_id, relative, 1, int(editable), now, now),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)
        raise
    return ProjectRecord(project_id, assistant_id, clean_name, str(root), entry_document_id)


def import_text_project(conn: sqlite3.Connection, data_dir: Path, assistant_id: str, filename: str, stream: BinaryIO, **limits: int) -> ProjectRecord:
    relative = _safe_relative_path(filename)
    if not _is_editable(relative):
        raise ValueError("仅支持导入 .md、.markdown 或 .txt 文本文件")
    return _import_project(conn, data_dir, assistant_id, Path(relative).stem, [(relative, stream)], **limits)


def import_folder_project(conn: sqlite3.Connection, data_dir: Path, assistant_id: str, name: str, files: Iterable[tuple[str, BinaryIO]], **limits: int) -> ProjectRecord:
    return _import_project(conn, data_dir, assistant_id, name, files, **limits)


def _save_document_impl(conn: sqlite3.Connection, data_dir: Path, assistant_id: str, project_id: str, document_id: str, content: str, expected_version: int) -> DocumentRecord:
    intent_id = _new_id()
    _recover_write_intents(conn, data_dir, assistant_id, project_id, document_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = _load_document(conn, data_dir, assistant_id, project_id, document_id)
        if not current.editable:
            raise ValueError("该文件不可编辑")
        if current.version != expected_version:
            raise ResourceConflictError("版本冲突")
        path = _document_path(
            data_dir, assistant_id, project_id, current.relative_path
        )
        utf8_bom = path.read_bytes().startswith(BOM_UTF8)
        now = _now()
        started_at = _process_started_at(os.getpid())
        try:
            conn.execute(
                "INSERT INTO document_write_intents "
                "(intent_id, assistant_id, project_id, document_id, change_set_id, "
                "expected_version, target_version, relative_path, content, utf8_bom, owner_pid, "
                "owner_started_at, claimed_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    intent_id, assistant_id, project_id, document_id, None,
                    expected_version, expected_version + 1, current.relative_path,
                    content, int(utf8_bom), os.getpid(), started_at, now, now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ResourceConflictError("文档正在被其他进程写入") from exc
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    intent = _write_intent_row(conn, assistant_id, project_id, intent_id)
    if intent is None:
        raise StorageRecoveryPendingError("文档写入意图意外丢失")
    try:
        _write_atomic(path, content, utf8_bom=utf8_bom)
    except Exception:
        _discard_failed_write_intent(conn, assistant_id, project_id, intent)
        raise
    _finalize_write_intent(conn, assistant_id, project_id, intent)
    staled: list[str] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        staled = _stale_outdated_hunks(
            conn, assistant_id, project_id, document_id,
            exclude_change_set_id=None, below_version=expected_version + 1,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_document(conn, data_dir, assistant_id, project_id, document_id), staled


def save_document(
    conn: sqlite3.Connection, data_dir: Path, assistant_id: str, project_id: str,
    document_id: str, content: str, expected_version: int,
) -> tuple[DocumentRecord, list[str]]:
    guard = _document_write_guard(assistant_id, project_id, document_id)
    with guard:
        return _save_document_impl(
            conn, data_dir, assistant_id, project_id, document_id, content, expected_version
        )


_MAX_HUNKS_PER_SET = 100
_MAX_SET_UTF8_BYTES = 1024 * 1024

_CHANGE_SET_COLUMNS = (
    "change_set_id, assistant_id, project_id, document_id, session_id, source, "
    "task_id, base_version, status"
)
_HUNK_COLUMNS = (
    "hunk_id, change_set_id, display_order, range_start, range_end, "
    "original_text, new_text, status, created_at, applied_at"
)


def _hunks_of(conn: sqlite3.Connection, change_set_id: str) -> list[ChangeSetHunkRecord]:
    rows = conn.execute(
        f"SELECT {_HUNK_COLUMNS} FROM change_set_hunks "
        "WHERE change_set_id = ? ORDER BY display_order",
        (change_set_id,),
    ).fetchall()
    return [ChangeSetHunkRecord(*row) for row in rows]


def _refresh_change_set_status(conn: sqlite3.Connection, change_set_id: str) -> None:
    """父级状态由 hunk 派生：有 pending/stale 则 pending，否则任一 applied 则 applied。"""
    statuses = [
        row[0] for row in conn.execute(
            "SELECT status FROM change_set_hunks WHERE change_set_id = ?",
            (change_set_id,),
        )
    ]
    if any(item in {"pending", "stale"} for item in statuses):
        status = "pending"
    elif "applied" in statuses:
        status = "applied"
    else:
        status = "rejected"
    conn.execute(
        "UPDATE change_sets SET status = ?, applied_at = COALESCE("
        "(SELECT MAX(applied_at) FROM change_set_hunks WHERE change_set_id = ?), applied_at"
        ") WHERE change_set_id = ?",
        (status, change_set_id, change_set_id),
    )


def _stale_outdated_hunks(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    document_id: str,
    *,
    exclude_change_set_id: str | None,
    below_version: int,
) -> list[str]:
    """版本推进后把其他 change set 的 pending hunk 转 stale，返回受影响的 set id。

    `exclude_change_set_id` 为 None 时作用于全部（手工保存场景）；指定时排除
    当前 set（其同组 hunk 走内容复检，不整组失效）。
    """
    params: list[object] = [assistant_id, project_id, document_id, below_version]
    exclude_sql = ""
    if exclude_change_set_id is not None:
        exclude_sql = " AND change_set_id != ?"
        params.append(exclude_change_set_id)
    rows = conn.execute(
        "SELECT DISTINCT change_set_id FROM change_sets "
        "WHERE assistant_id = ? AND project_id = ? AND document_id = ? "
        "AND base_version < ?" + exclude_sql,
        params,
    ).fetchall()
    staled = [row[0] for row in rows]
    for change_set_id in staled:
        conn.execute(
            "UPDATE change_set_hunks SET status = 'stale' "
            "WHERE change_set_id = ? AND status = 'pending'",
            (change_set_id,),
        )
        _refresh_change_set_status(conn, change_set_id)
    return staled


def _require_unique_task_document(
    conn: sqlite3.Connection, assistant_id: str, task_id: str, document_id: str
) -> None:
    if conn.execute(
        "SELECT 1 FROM change_sets "
        "WHERE assistant_id = ? AND task_id = ? AND document_id = ?",
        (assistant_id, task_id, document_id),
    ).fetchone() is not None:
        raise ResourceConflictError("该任务已提交过此文档的修改建议")


def _validate_hunk_layout(hunks: list[dict]) -> None:
    """相邻合法、重叠非法、两个零长度插入不得同位（架构 §4.7）。"""
    previous = None
    for item in hunks:
        if previous is not None:
            if item["start"] < previous["end"]:
                raise ResourceConflictError("hunk 范围重叠")
            if item["start"] == item["end"] == previous["start"] == previous["end"]:
                raise ResourceConflictError("两个零长度插入不得位于同一位置")
        previous = item


def _recover_write_intents_scope(
    conn: sqlite3.Connection, data_dir: Path, assistant_id: str, project_id: str,
    documents: list[dict],
) -> None:
    for entry in documents:
        _recover_write_intents(
            conn, data_dir, assistant_id, project_id, str(entry["document_id"])
        )


def create_change_set_hunks(
    conn: sqlite3.Connection,
    data_dir: Path,
    assistant_id: str,
    project_id: str,
    *,
    task_id: str,
    source: str,
    documents: list[dict],
    session_id: str | None = None,
) -> list[ChangeSetRecord]:
    """编辑工具路径：按 old_text 唯一匹配定位，原子创建父级 change set 与全部 hunk。"""
    if source not in {"selection", "chat"}:
        raise ValueError(f"change set 来源非法：{source}")
    if not documents:
        raise ValueError("documents 不能为空")
    _recover_write_intents_scope(conn, data_dir, assistant_id, project_id, documents)
    created: list[str] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        seen_documents: set[str] = set()
        for entry in documents:
            document_id = str(entry["document_id"])
            if document_id in seen_documents:
                raise ValueError("同一次请求中文档重复")
            seen_documents.add(document_id)
            base_version = int(entry["document_version"])
            raw_hunks = list(entry["hunks"])
            if not 1 <= len(raw_hunks) <= _MAX_HUNKS_PER_SET:
                raise ValueError("每个 change set 的 hunk 数量必须在 1 到 100 之间")
            document = _load_document(conn, data_dir, assistant_id, project_id, document_id)
            if document.version != base_version:
                raise ResourceConflictError("版本冲突")
            content = document.content if document.content is not None else ""
            total_bytes = sum(
                len(str(item.get("old_text", "")).encode("utf-8"))
                + len(str(item.get("new_text", "")).encode("utf-8"))
                for item in raw_hunks
            )
            if total_bytes > _MAX_SET_UTF8_BYTES:
                raise ValueError("change set 总量超过 1 MiB 上限")
            _require_unique_task_document(conn, assistant_id, task_id, document_id)
            located: list[dict] = []
            for item in raw_hunks:
                old_text = str(item.get("old_text", ""))
                new_text = str(item.get("new_text", ""))
                if old_text == "":
                    if content:
                        raise ResourceConflictError("非空文档不能使用空旧文本")
                    start = end = 0
                else:
                    start = content.find(old_text)
                    if start < 0:
                        raise ResourceConflictError("旧文本不存在")
                    if content.find(old_text, start + 1) >= 0:
                        raise ResourceConflictError("旧文本匹配多处，请提供更多上下文")
                    end = start + len(old_text)
                located.append(
                    {"start": start, "end": end, "original": old_text, "replacement": new_text}
                )
            located.sort(key=lambda item: item["start"])
            _validate_hunk_layout(located)
            change_set_id = _new_id()
            created.append(change_set_id)
            now = _now()
            conn.execute(
                "INSERT INTO change_sets (change_set_id, assistant_id, project_id, document_id, "
                "session_id, source, task_id, base_version, status, created_at, applied_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    change_set_id, assistant_id, project_id, document_id, session_id,
                    source, task_id, base_version, "pending", now,
                ),
            )
            for order, item in enumerate(located):
                conn.execute(
                    "INSERT INTO change_set_hunks (hunk_id, change_set_id, display_order, "
                    "range_start, range_end, original_text, new_text, status, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        _new_id(), change_set_id, order, item["start"], item["end"],
                        item["original"], item["replacement"], "pending", now,
                    ),
                )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ResourceConflictError("该任务已提交过此文档的修改建议") from exc
    except Exception:
        conn.rollback()
        raise
    return [get_change_set(conn, assistant_id, project_id, change_set_id) for change_set_id in created]


def create_selection_change_set(
    conn: sqlite3.Connection,
    data_dir: Path,
    assistant_id: str,
    project_id: str,
    document_id: str,
    *,
    task_id: str,
    start: int,
    end: int,
    original_text: str,
    replacement_text: str,
    base_version: int,
    source: str,
    session_id: str | None = None,
) -> ChangeSetRecord:
    """选区改写路径：使用服务端已掌握的选区范围，但必须复核原文快照。"""
    if source not in {"selection", "chat"}:
        raise ValueError(f"change set 来源非法：{source}")
    _recover_write_intents(conn, data_dir, assistant_id, project_id, document_id)
    change_set_id = _new_id()
    try:
        conn.execute("BEGIN IMMEDIATE")
        document = _load_document(conn, data_dir, assistant_id, project_id, document_id)
        if start < 0 or end < start or document.content is None or end > len(document.content):
            raise ValueError("选区范围非法")
        if document.version != base_version:
            raise ResourceConflictError("版本冲突")
        if document.content[start:end] != original_text:
            raise ResourceConflictError("原文快照不匹配")
        _require_unique_task_document(conn, assistant_id, task_id, document_id)
        now = _now()
        conn.execute(
            "INSERT INTO change_sets (change_set_id, assistant_id, project_id, document_id, "
            "session_id, source, task_id, base_version, status, created_at, applied_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
            (
                change_set_id, assistant_id, project_id, document_id, session_id,
                source, task_id, base_version, "pending", now,
            ),
        )
        conn.execute(
            "INSERT INTO change_set_hunks (hunk_id, change_set_id, display_order, "
            "range_start, range_end, original_text, new_text, status, created_at) "
            "VALUES (?,?,0,?,?,?,?,?,?)",
            (
                _new_id(), change_set_id, start, end,
                original_text, replacement_text, "pending", now,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ResourceConflictError("该任务已提交过此文档的修改建议") from exc
    except Exception:
        conn.rollback()
        raise
    return get_change_set(conn, assistant_id, project_id, change_set_id)


def get_change_set(
    conn: sqlite3.Connection, assistant_id: str, project_id: str, change_set_id: str
) -> ChangeSetRecord:
    row = conn.execute(
        f"SELECT {_CHANGE_SET_COLUMNS} FROM change_sets "
        "WHERE assistant_id = ? AND project_id = ? AND change_set_id = ?",
        (assistant_id, project_id, change_set_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"change set 不存在：{change_set_id}")
    return ChangeSetRecord(*row, hunks=_hunks_of(conn, change_set_id))


def list_pending_chat_changes(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    chat_session_id: str,
) -> list[ChangeSetRecord]:
    _project_row(conn, assistant_id, project_id)
    rows = conn.execute(
        f"SELECT {_CHANGE_SET_COLUMNS} FROM change_sets "
        "WHERE assistant_id = ? AND project_id = ? AND session_id = ? "
        "AND source = 'chat' AND status = 'pending' ORDER BY created_at, change_set_id",
        (assistant_id, project_id, chat_session_id),
    ).fetchall()
    return [ChangeSetRecord(*row, hunks=_hunks_of(conn, row[0])) for row in rows]


def list_change_sets_for_document(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    document_id: str,
    *,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """按文档分页查询 change set（含全部 hunk），供前端状态对账（架构 §5.9）。"""
    _project_row(conn, assistant_id, project_id)
    if page < 1 or page_size < 1:
        raise ValueError("分页参数非法")
    total = conn.execute(
        "SELECT COUNT(*) FROM change_sets "
        "WHERE assistant_id = ? AND project_id = ? AND document_id = ?",
        (assistant_id, project_id, document_id),
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT {_CHANGE_SET_COLUMNS}, created_at FROM change_sets "
        "WHERE assistant_id = ? AND project_id = ? AND document_id = ? "
        "ORDER BY created_at DESC, change_set_id DESC LIMIT ? OFFSET ?",
        (assistant_id, project_id, document_id, page_size, (page - 1) * page_size),
    ).fetchall()
    items = [ChangeSetRecord(*row[:9], hunks=_hunks_of(conn, row[0])) for row in rows]
    return {"items": items, "total": int(total), "page": page, "page_size": page_size}


def _mark_hunk(conn: sqlite3.Connection, hunk: ChangeSetHunkRecord, status: str) -> None:
    conn.execute(
        "UPDATE change_set_hunks SET status = ?, applied_at = ? WHERE hunk_id = ?",
        (status, _now() if status == "applied" else None, hunk.hunk_id),
    )
    _refresh_change_set_status(conn, hunk.change_set_id)


def _accept_hunk_impl(
    conn: sqlite3.Connection,
    data_dir: Path,
    assistant_id: str,
    project_id: str,
    change_set_id: str,
    hunk_id: str,
) -> tuple[DocumentRecord, ChangeSetRecord, ChangeSetHunkRecord, list[str]]:
    intent_id = _new_id()
    change = get_change_set(conn, assistant_id, project_id, change_set_id)
    document_id = change.document_id
    _recover_write_intents(conn, data_dir, assistant_id, project_id, document_id)
    new_content = ""
    path = None
    utf8_bom = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        change = get_change_set(conn, assistant_id, project_id, change_set_id)
        hunk = next((item for item in change.hunks if item.hunk_id == hunk_id), None)
        if hunk is None:
            raise KeyError(f"hunk 不存在：{hunk_id}")
        if hunk.status == "applied":
            raise ChangeSetStateError("already_applied", "该 hunk 已应用")
        if hunk.status == "rejected":
            raise ChangeSetStateError("already_rejected", "该 hunk 已放弃")
        if hunk.status == "stale":
            raise ChangeSetStateError("stale", "该 hunk 已失效，请重新生成")
        current = _load_document(conn, data_dir, assistant_id, project_id, document_id)
        content = current.content
        if content is None:
            raise ValueError("该文件不可编辑")
        if current.version == change.base_version:
            if content[hunk.start:hunk.end] != hunk.original_text:
                _mark_hunk(conn, hunk, "stale")
                conn.commit()
                raise ChangeSetStateError("stale", "原文快照不匹配，该 hunk 已失效")
            start, end = hunk.start, hunk.end
        else:
            # 内容复检：同组其余 hunk 在版本推进后凭 old_text 唯一匹配继续可用。
            start = content.find(hunk.original_text)
            if start < 0 or content.find(hunk.original_text, start + 1) >= 0:
                _mark_hunk(conn, hunk, "stale")
                conn.commit()
                raise ChangeSetStateError("stale", "修改位置已变化，该 hunk 已失效")
            end = start + len(hunk.original_text)
        new_content = content[:start] + hunk.new_text + content[end:]
        path = _document_path(data_dir, assistant_id, project_id, current.relative_path)
        utf8_bom = path.read_bytes().startswith(BOM_UTF8)
        now = _now()
        started_at = _process_started_at(os.getpid())
        try:
            conn.execute(
                "INSERT INTO document_write_intents "
                "(intent_id, assistant_id, project_id, document_id, change_set_id, hunk_id, "
                "expected_version, target_version, relative_path, content, utf8_bom, owner_pid, "
                "owner_started_at, claimed_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    intent_id, assistant_id, project_id, document_id, change_set_id,
                    hunk.hunk_id, current.version, current.version + 1,
                    current.relative_path, new_content, int(utf8_bom), os.getpid(),
                    started_at, now, now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ResourceConflictError("文档正在被其他进程写入") from exc
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    intent = _write_intent_row(conn, assistant_id, project_id, intent_id)
    if intent is None:
        raise StorageRecoveryPendingError("文档写入意图意外丢失")
    try:
        _write_atomic(path, new_content, utf8_bom=utf8_bom)
    except Exception:
        _discard_failed_write_intent(conn, assistant_id, project_id, intent)
        raise
    staled = _finalize_write_intent(conn, assistant_id, project_id, intent)
    document = get_document(conn, data_dir, assistant_id, project_id, document_id)
    final_set = get_change_set(conn, assistant_id, project_id, change_set_id)
    final_hunk = next(item for item in final_set.hunks if item.hunk_id == hunk_id)
    return document, final_set, final_hunk, staled


def accept_change_hunk(
    conn: sqlite3.Connection, data_dir: Path, assistant_id: str, project_id: str,
    change_set_id: str, hunk_id: str,
) -> tuple[DocumentRecord, ChangeSetRecord, ChangeSetHunkRecord, list[str]]:
    change = get_change_set(conn, assistant_id, project_id, change_set_id)
    guard = _document_write_guard(assistant_id, project_id, change.document_id)
    with guard:
        return _accept_hunk_impl(
            conn, data_dir, assistant_id, project_id, change_set_id, hunk_id
        )


def reject_change_hunk(
    conn: sqlite3.Connection, data_dir: Path, assistant_id: str, project_id: str,
    change_set_id: str, hunk_id: str,
) -> ChangeSetRecord:
    change = get_change_set(conn, assistant_id, project_id, change_set_id)
    # 与 accept/save/create 一致：先清理死进程的孤儿写意图，
    # 否则崩溃残留会让同组任意 hunk 的放弃持续 409（phase7 P2-1）。
    _recover_write_intents(conn, data_dir, assistant_id, project_id, change.document_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM document_write_intents "
            "WHERE assistant_id = ? AND project_id = ? AND change_set_id = ?",
            (assistant_id, project_id, change_set_id),
        ).fetchone() is not None:
            raise ResourceConflictError("文档正在被写入，稍后再试")
        hunk = next(
            (item for item in _hunks_of(conn, change_set_id) if item.hunk_id == hunk_id), None
        )
        if hunk is None:
            raise KeyError(f"hunk 不存在：{hunk_id}")
        if hunk.status == "applied":
            raise ChangeSetStateError("already_applied", "该 hunk 已应用")
        if hunk.status == "rejected":
            raise ChangeSetStateError("already_rejected", "该 hunk 已放弃")
        _mark_hunk(conn, hunk, "rejected")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_change_set(conn, assistant_id, project_id, change_set_id)


def accept_all_change_hunks(
    conn: sqlite3.Connection, data_dir: Path, assistant_id: str, project_id: str,
    change_set_id: str,
) -> dict:
    """全部接受：按范围倒序串行应用；任一 hunk 复检失败即停止，已应用不回滚。"""
    change = get_change_set(conn, assistant_id, project_id, change_set_id)
    guard = _document_write_guard(assistant_id, project_id, change.document_id)
    with guard:
        applied: list[str] = []
        staled_union: list[str] = []
        stopped: dict | None = None
        document: DocumentRecord | None = None
        pending = sorted(
            (item for item in change.hunks if item.status == "pending"),
            key=lambda item: item.start,
            reverse=True,
        )
        for hunk in pending:
            try:
                document, _, _, staled = _accept_hunk_impl(
                    conn, data_dir, assistant_id, project_id, change_set_id, hunk.hunk_id
                )
            except ChangeSetStateError as exc:
                stopped = {"hunk_id": hunk.hunk_id, "reason": exc.code}
                break
            applied.append(hunk.hunk_id)
            staled_union.extend(item for item in staled if item not in staled_union)
        final = get_change_set(conn, assistant_id, project_id, change_set_id)
        if document is None:
            document = get_document(
                conn, data_dir, assistant_id, project_id, change.document_id
            )
        return {
            "document": document,
            "change_set": final,
            "applied_hunk_ids": applied,
            "stopped": stopped,
            "staled_change_set_ids": staled_union,
        }
