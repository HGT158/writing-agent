"""项目 Agent 多会话历史：按助手、项目、会话三层隔离。"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .errors import ResourceConflictError

DDL = """
CREATE TABLE IF NOT EXISTS project_chat_sessions (
    assistant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    chat_session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (assistant_id, project_id, chat_session_id)
);
CREATE TABLE IF NOT EXISTS project_chat_messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    assistant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    chat_session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_project_chat_messages_scope
    ON project_chat_messages(assistant_id, project_id, chat_session_id, message_id);
CREATE INDEX IF NOT EXISTS idx_project_chat_sessions_recent
    ON project_chat_sessions(assistant_id, project_id, updated_at DESC);
"""


@dataclass(frozen=True)
class ProjectChatSessionRecord:
    chat_session_id: str
    assistant_id: str
    project_id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int = 0


@dataclass(frozen=True)
class ProjectChatMessageRecord:
    message_id: int
    assistant_id: str
    project_id: str
    chat_session_id: str
    role: str
    content: str
    created_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    conn.commit()


def _require_project(
    conn: sqlite3.Connection, assistant_id: str, project_id: str
) -> None:
    row = conn.execute(
        "SELECT 1 FROM projects "
        "WHERE assistant_id = ? AND project_id = ? AND archived_at IS NULL",
        (assistant_id, project_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"项目不存在：{project_id}")


def _session_row(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    chat_session_id: str,
) -> tuple:
    _require_project(conn, assistant_id, project_id)
    row = conn.execute(
        "SELECT chat_session_id, assistant_id, project_id, title, created_at, updated_at "
        "FROM project_chat_sessions "
        "WHERE assistant_id = ? AND project_id = ? AND chat_session_id = ?",
        (assistant_id, project_id, chat_session_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"项目聊天会话不存在：{chat_session_id}")
    return row


def create_session(
    conn: sqlite3.Connection, assistant_id: str, project_id: str
) -> ProjectChatSessionRecord:
    _require_project(conn, assistant_id, project_id)
    chat_session_id = uuid.uuid4().hex[:16]
    now = _now()
    conn.execute(
        "INSERT INTO project_chat_sessions "
        "(assistant_id, project_id, chat_session_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (assistant_id, project_id, chat_session_id, "新对话", now, now),
    )
    conn.commit()
    return get_session(conn, assistant_id, project_id, chat_session_id)


def get_session(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    chat_session_id: str,
) -> ProjectChatSessionRecord:
    row = _session_row(conn, assistant_id, project_id, chat_session_id)
    count = conn.execute(
        "SELECT COUNT(*) FROM project_chat_messages "
        "WHERE assistant_id = ? AND project_id = ? AND chat_session_id = ?",
        (assistant_id, project_id, chat_session_id),
    ).fetchone()[0]
    return ProjectChatSessionRecord(*row, int(count))


def list_sessions(
    conn: sqlite3.Connection, assistant_id: str, project_id: str
) -> list[ProjectChatSessionRecord]:
    _require_project(conn, assistant_id, project_id)
    rows = conn.execute(
        "SELECT s.chat_session_id, s.assistant_id, s.project_id, s.title, "
        "s.created_at, s.updated_at, COUNT(m.message_id) "
        "FROM project_chat_sessions AS s "
        "LEFT JOIN project_chat_messages AS m "
        "ON m.assistant_id = s.assistant_id AND m.project_id = s.project_id "
        "AND m.chat_session_id = s.chat_session_id "
        "WHERE s.assistant_id = ? AND s.project_id = ? "
        "GROUP BY s.assistant_id, s.project_id, s.chat_session_id "
        "ORDER BY s.updated_at DESC, s.chat_session_id DESC",
        (assistant_id, project_id),
    ).fetchall()
    return [ProjectChatSessionRecord(*row) for row in rows]


def add_message(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    chat_session_id: str,
    role: str,
    content: str,
) -> ProjectChatMessageRecord:
    if role not in {"user", "assistant"}:
        raise ValueError(f"项目聊天消息角色非法：{role}")
    clean = content.strip()
    if not clean:
        raise ValueError("项目聊天消息不能为空")
    _session_row(conn, assistant_id, project_id, chat_session_id)
    now = _now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        first_user = role == "user" and conn.execute(
            "SELECT 1 FROM project_chat_messages "
            "WHERE assistant_id = ? AND project_id = ? AND chat_session_id = ? "
            "AND role = 'user' LIMIT 1",
            (assistant_id, project_id, chat_session_id),
        ).fetchone() is None
        cursor = conn.execute(
            "INSERT INTO project_chat_messages "
            "(assistant_id, project_id, chat_session_id, role, content, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (assistant_id, project_id, chat_session_id, role, content, now),
        )
        if first_user:
            title = next(
                (line.strip() for line in clean.splitlines() if line.strip()),
                "新对话",
            )[:80]
            conn.execute(
                "UPDATE project_chat_sessions SET title = ?, updated_at = ? "
                "WHERE assistant_id = ? AND project_id = ? AND chat_session_id = ?",
                (title, now, assistant_id, project_id, chat_session_id),
            )
        else:
            conn.execute(
                "UPDATE project_chat_sessions SET updated_at = ? "
                "WHERE assistant_id = ? AND project_id = ? AND chat_session_id = ?",
                (now, assistant_id, project_id, chat_session_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    row = conn.execute(
        "SELECT message_id, assistant_id, project_id, chat_session_id, role, content, created_at "
        "FROM project_chat_messages WHERE message_id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return ProjectChatMessageRecord(*row)


def list_messages(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    chat_session_id: str,
) -> list[ProjectChatMessageRecord]:
    _session_row(conn, assistant_id, project_id, chat_session_id)
    rows = conn.execute(
        "SELECT message_id, assistant_id, project_id, chat_session_id, role, content, created_at "
        "FROM project_chat_messages "
        "WHERE assistant_id = ? AND project_id = ? AND chat_session_id = ? "
        "ORDER BY message_id",
        (assistant_id, project_id, chat_session_id),
    ).fetchall()
    return [ProjectChatMessageRecord(*row) for row in rows]


def delete_session(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    chat_session_id: str,
) -> None:
    _session_row(conn, assistant_id, project_id, chat_session_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        pending = conn.execute(
            "SELECT 1 FROM change_sets "
            "WHERE assistant_id = ? AND project_id = ? AND session_id = ? "
            "AND source = 'chat' AND status = 'pending' LIMIT 1",
            (assistant_id, project_id, chat_session_id),
        ).fetchone()
        if pending is not None:
            raise ResourceConflictError("会话存在待处理修改，拒绝删除")
        conn.execute(
            "DELETE FROM change_sets "
            "WHERE assistant_id = ? AND project_id = ? AND session_id = ? "
            "AND source = 'chat' AND status != 'pending'",
            (assistant_id, project_id, chat_session_id),
        )
        conn.execute(
            "DELETE FROM project_chat_messages "
            "WHERE assistant_id = ? AND project_id = ? AND chat_session_id = ?",
            (assistant_id, project_id, chat_session_id),
        )
        conn.execute(
            "DELETE FROM project_chat_sessions "
            "WHERE assistant_id = ? AND project_id = ? AND chat_session_id = ?",
            (assistant_id, project_id, chat_session_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def delete_empty_session(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    chat_session_id: str,
) -> bool:
    """失败补偿：仅删除仍无消息、无 change set 的新会话。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "DELETE FROM project_chat_sessions "
            "WHERE assistant_id = ? AND project_id = ? AND chat_session_id = ? "
            "AND NOT EXISTS ("
            "SELECT 1 FROM project_chat_messages AS m "
            "WHERE m.assistant_id = project_chat_sessions.assistant_id "
            "AND m.project_id = project_chat_sessions.project_id "
            "AND m.chat_session_id = project_chat_sessions.chat_session_id"
            ") AND NOT EXISTS ("
            "SELECT 1 FROM change_sets AS c "
            "WHERE c.assistant_id = project_chat_sessions.assistant_id "
            "AND c.project_id = project_chat_sessions.project_id "
            "AND c.session_id = project_chat_sessions.chat_session_id"
            ")",
            (assistant_id, project_id, chat_session_id),
        )
        conn.commit()
        return cursor.rowcount == 1
    except Exception:
        conn.rollback()
        raise


def delete_assistant_rows(conn: sqlite3.Connection, assistant_id: str) -> None:
    conn.execute(
        "DELETE FROM project_chat_messages WHERE assistant_id = ?", (assistant_id,)
    )
    conn.execute(
        "DELETE FROM project_chat_sessions WHERE assistant_id = ?", (assistant_id,)
    )
