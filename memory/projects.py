"""文章项目与项目文件的持久化实现。

SQL 和受管文件系统操作都留在 memory 层，由 MemoryStore 注入 assistant_id
并提供业务门面。
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import threading
import unicodedata
import uuid
import weakref
from codecs import BOM_UTF8
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable

import psutil

from .errors import (
    DocumentWriteBusyError,
    ResourceConflictError,
    StorageRecoveryPendingError,
)


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
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    original_text TEXT NOT NULL,
    replacement_text TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'rejected')),
    created_at TEXT NOT NULL,
    applied_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_change_sets_owner
    ON change_sets(assistant_id, project_id, document_id);
CREATE TABLE IF NOT EXISTS document_write_intents (
    intent_id TEXT PRIMARY KEY,
    assistant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    change_set_id TEXT,
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

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
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
class ChangeSetRecord:
    change_set_id: str
    assistant_id: str
    project_id: str
    document_id: str
    session_id: str | None
    source: str
    start: int
    end: int
    original_text: str
    replacement_text: str
    base_version: int
    status: str


@dataclass(frozen=True)
class _WriteIntent:
    intent_id: str
    document_id: str
    change_set_id: str | None
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


def _validate_id(value: str, label: str) -> str:
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{label} 非法：{value!r}")
    return value


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
    _validate_id(assistant_id, "assistant_id")
    _validate_id(project_id, "project_id")
    root = (data_dir / "assistants" / assistant_id / "projects" / project_id).resolve()
    expected_parent = (data_dir / "assistants" / assistant_id / "projects").resolve()
    if root.parent != expected_parent:
        raise ValueError("项目目录越界")
    return root


def recover_project_artifacts(conn: sqlite3.Connection, data_dir: Path) -> None:
    """对账项目级崩溃残骸；只处理受管目录中的确定性路径。"""
    rows = conn.execute(
        "SELECT project_id, assistant_id, root_path, archived_at FROM projects"
    ).fetchall()
    projects_by_id = {
        (assistant_id, project_id): (Path(root_path), archived_at)
        for project_id, assistant_id, root_path, archived_at in rows
    }

    def reconcile_purge(staging: Path, assistant_id: str) -> None:
        suffix = staging.name.removeprefix(".purge-")
        project_id = suffix.split("-", 1)[0]
        record = projects_by_id.get((assistant_id, project_id))
        if record is None:
            shutil.rmtree(staging)
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
                    shutil.rmtree(child)
                elif child.name.startswith(".purge-"):
                    reconcile_purge(child, assistant_id)
                elif child.is_dir() and _ID_RE.fullmatch(child.name):
                    if (assistant_id, child.name) not in projects_by_id:
                        shutil.rmtree(child)

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
    }
    for name, declaration in additions.items():
        if name not in columns:
            conn.execute(
                f"ALTER TABLE document_write_intents ADD COLUMN {name} {declaration}"
            )
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'change_sets'"
    ).fetchone()[0]
    if "CHECK" not in schema.upper():
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DROP INDEX IF EXISTS idx_change_sets_owner")
            conn.execute("ALTER TABLE change_sets RENAME TO change_sets_legacy")
            conn.execute(
                """CREATE TABLE change_sets (
                    change_set_id TEXT PRIMARY KEY,
                    assistant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    session_id TEXT,
                    source TEXT NOT NULL CHECK (source IN ('selection', 'chat')),
                    start_offset INTEGER NOT NULL,
                    end_offset INTEGER NOT NULL,
                    original_text TEXT NOT NULL,
                    replacement_text TEXT NOT NULL,
                    base_version INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'rejected')),
                    created_at TEXT NOT NULL,
                    applied_at TEXT
                )"""
            )
            conn.execute(
                "INSERT INTO change_sets SELECT * FROM change_sets_legacy"
            )
            conn.execute("DROP TABLE change_sets_legacy")
            conn.execute(
                "CREATE INDEX idx_change_sets_owner "
                "ON change_sets(assistant_id, project_id, document_id)"
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    else:
        conn.commit()


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


def _row_to_change_set(row: tuple) -> ChangeSetRecord:
    return ChangeSetRecord(
        change_set_id=row[0], assistant_id=row[1], project_id=row[2],
        document_id=row[3], session_id=row[4], source=row[5],
        start=row[6], end=row[7], original_text=row[8], replacement_text=row[9],
        base_version=row[10], status=row[11],
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
        "SELECT intent_id, document_id, change_set_id, expected_version, target_version, "
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
        if intent.change_set_id is not None:
            change_cursor = conn.execute(
                "UPDATE change_sets SET status = 'applied', applied_at = ? "
                "WHERE assistant_id = ? AND project_id = ? AND change_set_id = ? "
                "AND status = 'pending'",
                (_now(), assistant_id, project_id, intent.change_set_id),
            )
            if change_cursor.rowcount != 1:
                status_row = conn.execute(
                    "SELECT status FROM change_sets "
                    "WHERE assistant_id = ? AND project_id = ? AND change_set_id = ?",
                    (assistant_id, project_id, intent.change_set_id),
                ).fetchone()
                if status_row is None or status_row[0] != "applied":
                    raise ResourceConflictError("change set 已处理")
        conn.execute(
            "DELETE FROM document_write_intents "
            "WHERE assistant_id = ? AND project_id = ? AND intent_id = ?",
            (assistant_id, project_id, intent.intent_id),
        )
        conn.commit()
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
    _validate_id(assistant_id, "assistant_id")
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("项目名称不能为空")
    project_id = _new_id()
    root = _project_root(data_dir, assistant_id, project_id)
    root.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir()
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


def archive_project(
    conn: sqlite3.Connection, data_dir: Path, assistant_id: str, project_id: str
) -> Path:
    project = _row_to_project(_project_row(conn, assistant_id, project_id))
    _reject_pending_change_sets(conn, assistant_id, project_id)
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


def purge_project(
    conn: sqlite3.Connection, data_dir: Path, assistant_id: str, project_id: str
) -> None:
    project = _project_row_any(conn, assistant_id, project_id)
    archived = project[5] is not None
    if not archived:
        _reject_pending_change_sets(conn, assistant_id, project_id)
    source = Path(project[3]) if archived else _project_root(data_dir, assistant_id, project_id)
    staging = source.parent / f".purge-{project_id}-{uuid.uuid4().hex}"
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
            "DELETE FROM project_chat_sessions WHERE assistant_id = ? AND project_id = ?",
            (assistant_id, project_id),
        )
        conn.execute(
            "DELETE FROM document_write_intents WHERE assistant_id = ? AND project_id = ?",
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
    shutil.rmtree(staging)


def delete_assistant_rows(conn: sqlite3.Connection, assistant_id: str) -> None:
    """物理清除助手时删除项目元数据，顺序保持子记录先于项目。"""
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
    _validate_id(assistant_id, "assistant_id")
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("项目名称不能为空")
    project_id = _new_id()
    root = _project_root(data_dir, assistant_id, project_id)
    staging = root.parent / f".import-{project_id}"
    staging.mkdir(parents=True, exist_ok=False)
    entries: list[tuple[str, bool]] = []
    total = 0
    try:
        for index, (raw_path, stream) in enumerate(files, start=1):
            if index > max_files:
                raise ValueError("导入文件数量超过限制")
            relative = _safe_relative_path(raw_path)
            if any(existing == relative for existing, _ in entries):
                raise ValueError(f"导入路径重复：{relative}")
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
    return get_document(conn, data_dir, assistant_id, project_id, document_id)


def save_document(
    conn: sqlite3.Connection, data_dir: Path, assistant_id: str, project_id: str,
    document_id: str, content: str, expected_version: int,
) -> DocumentRecord:
    guard = _document_write_guard(assistant_id, project_id, document_id)
    with guard:
        return _save_document_impl(
            conn, data_dir, assistant_id, project_id, document_id, content, expected_version
        )


def create_change_set(
    conn: sqlite3.Connection,
    data_dir: Path,
    assistant_id: str,
    project_id: str,
    document_id: str,
    *,
    source: str,
    start: int,
    end: int,
    original_text: str,
    replacement_text: str,
    base_version: int,
    session_id: str | None = None,
) -> ChangeSetRecord:
    return create_change_sets(
        conn,
        data_dir,
        assistant_id,
        project_id,
        [
            {
                "document_id": document_id,
                "start": start,
                "end": end,
                "original_text": original_text,
                "replacement_text": replacement_text,
                "base_version": base_version,
            }
        ],
        source=source,
        session_id=session_id,
    )[0]


def create_change_sets(
    conn: sqlite3.Connection,
    data_dir: Path,
    assistant_id: str,
    project_id: str,
    drafts: Iterable[dict[str, object]],
    *,
    source: str,
    session_id: str | None = None,
) -> list[ChangeSetRecord]:
    items = list(drafts)
    if not items:
        return []
    if source not in {"selection", "chat"}:
        raise ValueError(f"change set 来源非法：{source}")
    created_ids: list[str] = []
    _recover_write_intents(conn, data_dir, assistant_id, project_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        validated: list[tuple[str, int, int, str, str, int]] = []
        for item in items:
            document_id = str(item["document_id"])
            start = int(item["start"])
            end = int(item["end"])
            original_text = str(item["original_text"])
            replacement_text = str(item["replacement_text"])
            base_version = int(item["base_version"])
            document = _load_document(
                conn, data_dir, assistant_id, project_id, document_id
            )
            if start < 0 or end < start or document.content is None or end > len(document.content):
                raise ValueError("选区范围非法")
            if document.version != base_version:
                raise ResourceConflictError("版本冲突")
            if document.content[start:end] != original_text:
                raise ResourceConflictError("原文快照不匹配")
            validated.append(
                (document_id, start, end, original_text, replacement_text, base_version)
            )
        now = _now()
        for document_id, start, end, original_text, replacement_text, base_version in validated:
            change_set_id = _new_id()
            created_ids.append(change_set_id)
            conn.execute(
                "INSERT INTO change_sets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    change_set_id, assistant_id, project_id, document_id, session_id,
                    source, start, end, original_text, replacement_text,
                    base_version, "pending", now,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return [
        get_change_set(conn, assistant_id, project_id, change_set_id)
        for change_set_id in created_ids
    ]


def get_change_set(
    conn: sqlite3.Connection, assistant_id: str, project_id: str, change_set_id: str
) -> ChangeSetRecord:
    row = conn.execute(
        "SELECT change_set_id, assistant_id, project_id, document_id, session_id, source, "
        "start_offset, end_offset, original_text, replacement_text, base_version, status "
        "FROM change_sets WHERE assistant_id = ? AND project_id = ? AND change_set_id = ?",
        (assistant_id, project_id, change_set_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"change set 不存在：{change_set_id}")
    return _row_to_change_set(row)


def list_pending_chat_changes(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    chat_session_id: str,
) -> list[ChangeSetRecord]:
    _project_row(conn, assistant_id, project_id)
    rows = conn.execute(
        "SELECT change_set_id, assistant_id, project_id, document_id, session_id, source, "
        "start_offset, end_offset, original_text, replacement_text, base_version, status, "
        "created_at, applied_at FROM change_sets "
        "WHERE assistant_id = ? AND project_id = ? AND session_id = ? "
        "AND source = 'chat' AND status = 'pending' ORDER BY created_at, change_set_id",
        (assistant_id, project_id, chat_session_id),
    ).fetchall()
    return [_row_to_change_set(row) for row in rows]


def reject_change_set(
    conn: sqlite3.Connection, assistant_id: str, project_id: str, change_set_id: str
) -> ChangeSetRecord:
    get_change_set(conn, assistant_id, project_id, change_set_id)
    cursor = conn.execute(
        "UPDATE change_sets SET status = 'rejected' "
        "WHERE assistant_id = ? AND project_id = ? AND change_set_id = ? AND status = 'pending'",
        (assistant_id, project_id, change_set_id),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise ResourceConflictError("change set 已处理")
    conn.commit()
    return get_change_set(conn, assistant_id, project_id, change_set_id)


def _apply_change_set_impl(
    conn: sqlite3.Connection,
    data_dir: Path,
    assistant_id: str,
    project_id: str,
    change_set_id: str,
    expected_version: int,
) -> tuple[DocumentRecord, ChangeSetRecord]:
    intent_id = _new_id()
    _recover_write_intents(conn, data_dir, assistant_id, project_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        change = get_change_set(conn, assistant_id, project_id, change_set_id)
        if change.status != "pending":
            raise ResourceConflictError("change set 已处理")
        current = _load_document(
            conn, data_dir, assistant_id, project_id, change.document_id
        )
        if current.version != expected_version or current.version != change.base_version:
            raise ResourceConflictError("版本冲突")
        if current.content is None:
            raise ValueError("该文件不可编辑")
        if current.content[change.start:change.end] != change.original_text:
            raise ResourceConflictError("原文快照不匹配")
        content = (
            current.content[:change.start]
            + change.replacement_text
            + current.content[change.end:]
        )
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
                    intent_id, assistant_id, project_id, change.document_id,
                    change_set_id, expected_version, expected_version + 1,
                    current.relative_path, content, int(utf8_bom), os.getpid(),
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
        _write_atomic(path, content, utf8_bom=utf8_bom)
    except Exception:
        _discard_failed_write_intent(conn, assistant_id, project_id, intent)
        raise
    _finalize_write_intent(conn, assistant_id, project_id, intent)
    return (
        get_document(conn, data_dir, assistant_id, project_id, change.document_id),
        get_change_set(conn, assistant_id, project_id, change_set_id),
    )


def apply_change_set(
    conn: sqlite3.Connection, data_dir: Path, assistant_id: str, project_id: str,
    change_set_id: str, expected_version: int,
) -> tuple[DocumentRecord, ChangeSetRecord]:
    change = get_change_set(conn, assistant_id, project_id, change_set_id)
    guard = _document_write_guard(assistant_id, project_id, change.document_id)
    with guard:
        return _apply_change_set_impl(
            conn, data_dir, assistant_id, project_id, change_set_id, expected_version
        )
