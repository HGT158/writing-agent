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
CREATE TABLE IF NOT EXISTS project_chat_summaries (
    assistant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    chat_session_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    covered_through_message_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (assistant_id, project_id, chat_session_id)
);
CREATE INDEX IF NOT EXISTS idx_project_chat_messages_scope
    ON project_chat_messages(assistant_id, project_id, chat_session_id, message_id);
CREATE INDEX IF NOT EXISTS idx_project_chat_sessions_recent
    ON project_chat_sessions(assistant_id, project_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS project_chat_work_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    assistant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    chat_session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    user_message_id INTEGER NOT NULL,
    event_seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    change_set_id TEXT,
    document_id TEXT,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    tool_name TEXT,
    args_summary TEXT,
    result_summary TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_chat_work_events_seq
    ON project_chat_work_events(task_id, event_seq);
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_chat_work_events_task_terminal
    ON project_chat_work_events(assistant_id, project_id, task_id) WHERE kind = 'task';
CREATE INDEX IF NOT EXISTS idx_project_chat_work_events_scope
    ON project_chat_work_events(assistant_id, project_id, chat_session_id, user_message_id, event_seq);
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


@dataclass(frozen=True)
class ProjectChatSummaryRecord:
    """滑出保留窗口的早期对话压缩结果（架构 §3.3）；派生数据，不进可见历史。"""

    assistant_id: str
    project_id: str
    chat_session_id: str
    summary: str
    covered_through_message_id: int
    created_at: str
    updated_at: str


WORK_EVENT_KINDS = {"progress", "tool", "warning", "changes", "task"}
WORK_EVENT_STATUSES = {"succeeded", "failed", "interrupted"}

_WORK_EVENT_COLUMNS = (
    "event_id, assistant_id, project_id, chat_session_id, task_id, user_message_id, "
    "event_seq, kind, status, change_set_id, document_id, title, detail, "
    "tool_name, args_summary, result_summary, created_at, completed_at"
)


@dataclass(frozen=True)
class ProjectChatWorkEventRecord:
    """项目聊天工作记录（架构 §5.7 v1.19）：只服务界面展示，不进模型上下文。"""

    event_id: int
    assistant_id: str
    project_id: str
    chat_session_id: str
    task_id: str
    user_message_id: int
    event_seq: int
    kind: str
    status: str
    change_set_id: str | None
    document_id: str | None
    title: str
    detail: str
    tool_name: str | None
    args_summary: str | None
    result_summary: str | None
    created_at: str
    completed_at: str | None


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


def get_summary(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    chat_session_id: str,
) -> ProjectChatSummaryRecord | None:
    row = conn.execute(
        "SELECT assistant_id, project_id, chat_session_id, summary, "
        "covered_through_message_id, created_at, updated_at "
        "FROM project_chat_summaries "
        "WHERE assistant_id = ? AND project_id = ? AND chat_session_id = ?",
        (assistant_id, project_id, chat_session_id),
    ).fetchone()
    return ProjectChatSummaryRecord(*row) if row is not None else None


def save_summary(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    chat_session_id: str,
    summary: str,
    covered_through_message_id: int,
) -> ProjectChatSummaryRecord:
    _session_row(conn, assistant_id, project_id, chat_session_id)
    clean = summary.strip()
    if not clean:
        raise ValueError("上下文摘要不能为空")
    now = _now()
    conn.execute(
        "INSERT INTO project_chat_summaries "
        "(assistant_id, project_id, chat_session_id, summary, "
        "covered_through_message_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(assistant_id, project_id, chat_session_id) DO UPDATE SET "
        "summary = excluded.summary, "
        "covered_through_message_id = excluded.covered_through_message_id, "
        "updated_at = excluded.updated_at",
        (
            assistant_id,
            project_id,
            chat_session_id,
            clean,
            covered_through_message_id,
            now,
            now,
        ),
    )
    conn.commit()
    record = get_summary(conn, assistant_id, project_id, chat_session_id)
    assert record is not None
    return record


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
            "DELETE FROM project_chat_work_events "
            "WHERE assistant_id = ? AND project_id = ? AND chat_session_id = ?",
            (assistant_id, project_id, chat_session_id),
        )
        conn.execute(
            "DELETE FROM project_chat_summaries "
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
        if cursor.rowcount == 1:
            conn.execute(
                "DELETE FROM project_chat_summaries "
                "WHERE assistant_id = ? AND project_id = ? AND chat_session_id = ?",
                (assistant_id, project_id, chat_session_id),
            )
            conn.execute(
                "DELETE FROM project_chat_work_events "
                "WHERE assistant_id = ? AND project_id = ? AND chat_session_id = ?",
                (assistant_id, project_id, chat_session_id),
            )
        conn.commit()
        return cursor.rowcount == 1
    except Exception:
        conn.rollback()
        raise


def add_work_event(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    chat_session_id: str,
    *,
    task_id: str,
    user_message_id: int,
    event_seq: int,
    kind: str,
    status: str,
    title: str,
    detail: str = "",
    tool_name: str | None = None,
    args_summary: str | None = None,
    result_summary: str | None = None,
    change_set_id: str | None = None,
    document_id: str | None = None,
    created_at: str,
    completed_at: str | None = None,
) -> ProjectChatWorkEventRecord:
    if kind not in WORK_EVENT_KINDS:
        raise ValueError(f"工作记录类型非法：{kind}")
    if status not in WORK_EVENT_STATUSES:
        raise ValueError(f"工作记录状态非法：{status}")
    _session_row(conn, assistant_id, project_id, chat_session_id)
    if kind == "task":
        # 任务终态幂等：唯一部分索引兜底，重复写入复用既有终态（架构 §5.9）。
        conn.execute(
            "INSERT OR IGNORE INTO project_chat_work_events "
            "(assistant_id, project_id, chat_session_id, task_id, user_message_id, "
            "event_seq, kind, status, change_set_id, document_id, title, detail, "
            "tool_name, args_summary, result_summary, created_at, completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                assistant_id, project_id, chat_session_id, task_id, user_message_id,
                event_seq, kind, status, change_set_id, document_id, title, detail,
                tool_name, args_summary, result_summary, created_at, completed_at,
            ),
        )
        row = conn.execute(
            f"SELECT {_WORK_EVENT_COLUMNS} FROM project_chat_work_events "
            "WHERE assistant_id = ? AND project_id = ? AND task_id = ? AND kind = 'task'",
            (assistant_id, project_id, task_id),
        ).fetchone()
    else:
        cursor = conn.execute(
            "INSERT INTO project_chat_work_events "
            "(assistant_id, project_id, chat_session_id, task_id, user_message_id, "
            "event_seq, kind, status, change_set_id, document_id, title, detail, "
            "tool_name, args_summary, result_summary, created_at, completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                assistant_id, project_id, chat_session_id, task_id, user_message_id,
                event_seq, kind, status, change_set_id, document_id, title, detail,
                tool_name, args_summary, result_summary, created_at, completed_at,
            ),
        )
        row = conn.execute(
            f"SELECT {_WORK_EVENT_COLUMNS} FROM project_chat_work_events "
            "WHERE event_id = ?",
            (cursor.lastrowid,),
        ).fetchone()
    conn.commit()
    return ProjectChatWorkEventRecord(*row)


def list_work_events(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    chat_session_id: str,
) -> list[ProjectChatWorkEventRecord]:
    _session_row(conn, assistant_id, project_id, chat_session_id)
    rows = conn.execute(
        f"SELECT {_WORK_EVENT_COLUMNS} FROM project_chat_work_events "
        "WHERE assistant_id = ? AND project_id = ? AND chat_session_id = ? "
        "ORDER BY user_message_id, event_seq",
        (assistant_id, project_id, chat_session_id),
    ).fetchall()
    return [ProjectChatWorkEventRecord(*row) for row in rows]


def list_unfinished_work_task_ids(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    chat_session_id: str,
) -> list[str]:
    _session_row(conn, assistant_id, project_id, chat_session_id)
    rows = conn.execute(
        "SELECT DISTINCT task_id FROM project_chat_work_events "
        "WHERE assistant_id = ? AND project_id = ? AND chat_session_id = ? "
        "AND NOT EXISTS ("
        "SELECT 1 FROM project_chat_work_events AS t "
        "WHERE t.assistant_id = project_chat_work_events.assistant_id "
        "AND t.project_id = project_chat_work_events.project_id "
        "AND t.task_id = project_chat_work_events.task_id AND t.kind = 'task') "
        "ORDER BY task_id",
        (assistant_id, project_id, chat_session_id),
    ).fetchall()
    return [row[0] for row in rows]


def interrupt_work_task(
    conn: sqlite3.Connection,
    assistant_id: str,
    project_id: str,
    chat_session_id: str,
    task_id: str,
) -> None:
    """对账补写：无终态且任务已不活动时幂等写入 interrupted 终态（架构 §5.9）。"""
    _session_row(conn, assistant_id, project_id, chat_session_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT 1 FROM project_chat_work_events "
            "WHERE assistant_id = ? AND project_id = ? AND task_id = ? AND kind = 'task'",
            (assistant_id, project_id, task_id),
        ).fetchone()
        if existing is not None:
            conn.commit()
            return
        user_message_id = conn.execute(
            "SELECT COALESCE(MAX(user_message_id), 0) FROM project_chat_work_events "
            "WHERE assistant_id = ? AND project_id = ? AND chat_session_id = ? AND task_id = ?",
            (assistant_id, project_id, chat_session_id, task_id),
        ).fetchone()[0]
        next_seq = conn.execute(
            "SELECT COALESCE(MAX(event_seq), 0) + 1 FROM project_chat_work_events "
            "WHERE assistant_id = ? AND project_id = ? AND chat_session_id = ? AND task_id = ?",
            (assistant_id, project_id, chat_session_id, task_id),
        ).fetchone()[0]
        now = _now()
        conn.execute(
            "INSERT OR IGNORE INTO project_chat_work_events "
            "(assistant_id, project_id, chat_session_id, task_id, user_message_id, "
            "event_seq, kind, status, title, detail, created_at, completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                assistant_id, project_id, chat_session_id, task_id, user_message_id,
                next_seq, "task", "interrupted", "任务中断",
                "进程退出或连接中断，记录由对账补写", now, now,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def delete_assistant_rows(conn: sqlite3.Connection, assistant_id: str) -> None:
    conn.execute(
        "DELETE FROM project_chat_work_events WHERE assistant_id = ?", (assistant_id,)
    )
    conn.execute(
        "DELETE FROM project_chat_messages WHERE assistant_id = ?", (assistant_id,)
    )
    conn.execute(
        "DELETE FROM project_chat_summaries WHERE assistant_id = ?", (assistant_id,)
    )
    conn.execute(
        "DELETE FROM project_chat_sessions WHERE assistant_id = ?", (assistant_id,)
    )
