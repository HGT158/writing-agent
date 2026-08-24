"""短期记忆：sessions / messages 表的 SQL 操作（均强制 assistant_id 过滤）。

所有函数接收 sqlite3.Connection，由 store.py 统一调用——业务代码禁止裸写 SQL。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    assistant_id TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    task         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (assistant_id, session_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    assistant_id TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_assistant ON messages(assistant_id);
CREATE TABLE IF NOT EXISTS articles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    assistant_id TEXT NOT NULL,
    session_id   TEXT,
    title        TEXT NOT NULL,
    path         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_assistant ON articles(assistant_id);
CREATE TABLE IF NOT EXISTS sources (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    assistant_id TEXT NOT NULL,
    session_id   TEXT,
    url          TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    fulltext     TEXT NOT NULL DEFAULT '',
    fetched_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_assistant ON sources(assistant_id);
CREATE TABLE IF NOT EXISTS run_locks (
    assistant_id TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    pid          INTEGER NOT NULL,
    acquired_at  TEXT NOT NULL,
    pid_started_at REAL NOT NULL DEFAULT 0
);
"""

FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    assistant_id UNINDEXED,
    content='messages',
    content_rowid='id',
    tokenize='trigram'
);
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title,
    assistant_id UNINDEXED,
    content='articles',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content, assistant_id)
    VALUES (new.id, new.content, new.assistant_id);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, assistant_id)
    VALUES ('delete', old.id, old.content, old.assistant_id);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, assistant_id)
    VALUES ('delete', old.id, old.content, old.assistant_id);
    INSERT INTO messages_fts(rowid, content, assistant_id)
    VALUES (new.id, new.content, new.assistant_id);
END;

CREATE TRIGGER IF NOT EXISTS articles_fts_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, title, assistant_id)
    VALUES (new.id, new.title, new.assistant_id);
END;
CREATE TRIGGER IF NOT EXISTS articles_fts_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, assistant_id)
    VALUES ('delete', old.id, old.title, old.assistant_id);
END;
CREATE TRIGGER IF NOT EXISTS articles_fts_au AFTER UPDATE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, assistant_id)
    VALUES ('delete', old.id, old.title, old.assistant_id);
    INSERT INTO articles_fts(rowid, title, assistant_id)
    VALUES (new.id, new.title, new.assistant_id);
END;
"""

FTS_SCHEMA_VERSION = 1
FTS_TRIGGER_NAMES = {
    "messages_fts_ai",
    "messages_fts_ad",
    "messages_fts_au",
    "articles_fts_ai",
    "articles_fts_ad",
    "articles_fts_au",
}
FTS_DROP_DDL = """
DROP TRIGGER IF EXISTS messages_fts_ai;
DROP TRIGGER IF EXISTS messages_fts_ad;
DROP TRIGGER IF EXISTS messages_fts_au;
DROP TRIGGER IF EXISTS articles_fts_ai;
DROP TRIGGER IF EXISTS articles_fts_ad;
DROP TRIGGER IF EXISTS articles_fts_au;
DROP TABLE IF EXISTS messages_fts;
DROP TABLE IF EXISTS articles_fts;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fts_schema_is_current(conn: sqlite3.Connection) -> bool:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < FTS_SCHEMA_VERSION:
        return False
    for table in ("messages_fts", "articles_fts"):
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if row is None or "tokenize='trigram'" not in "".join(row[0].lower().split()):
            return False
    triggers = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    }
    return FTS_TRIGGER_NAMES.issubset(triggers)


def _migrate_fts(conn: sqlite3.Connection) -> None:
    script = f"""
    BEGIN IMMEDIATE;
    {FTS_DROP_DDL}
    {FTS_DDL}
    INSERT INTO messages_fts(messages_fts) VALUES ('rebuild');
    INSERT INTO articles_fts(articles_fts) VALUES ('rebuild');
    PRAGMA user_version = {FTS_SCHEMA_VERSION};
    COMMIT;
    """
    try:
        conn.executescript(script)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(run_locks)")}
    if "pid_started_at" not in columns:
        conn.execute(
            "ALTER TABLE run_locks ADD COLUMN pid_started_at REAL NOT NULL DEFAULT 0"
        )
        conn.commit()
    if _fts_schema_is_current(conn):
        return
    _migrate_fts(conn)


def create_session(conn: sqlite3.Connection, assistant_id: str, session_id: str, task: str) -> None:
    # OR IGNORE：resume 已存在会话时不覆盖原始 task/created_at（审查 P2-17）
    conn.execute(
        "INSERT OR IGNORE INTO sessions (assistant_id, session_id, task, created_at) VALUES (?,?,?,?)",
        (assistant_id, session_id, task, _now()),
    )
    conn.commit()


def session_assistant_ids(conn: sqlite3.Connection, session_id: str) -> set[str]:
    return {
        row[0] for row in conn.execute(
            "SELECT assistant_id FROM sessions WHERE session_id = ?", (session_id,)
        )
    }


def add_message(conn: sqlite3.Connection, assistant_id: str, session_id: str, role: str, content: str) -> None:
    conn.execute(
        "INSERT INTO messages (assistant_id, session_id, role, content, created_at) VALUES (?,?,?,?,?)",
        (assistant_id, session_id, role, content, _now()),
    )
    conn.commit()


def search_messages(conn: sqlite3.Connection, assistant_id: str, like_clauses: list[str], limit: int) -> list[tuple[str, str]]:
    """按 assistant_id 强制过滤的 LIKE 检索，返回 (content, created_at)。"""
    if not like_clauses:
        return []
    where = " OR ".join("content LIKE ? ESCAPE '\\'" for _ in like_clauses)
    sql = f"SELECT content, created_at FROM messages WHERE assistant_id = ? AND ({where}) ORDER BY id DESC LIMIT ?"
    return conn.execute(sql, (assistant_id, *like_clauses, limit)).fetchall()


def search_messages_fts(
    conn: sqlite3.Connection, assistant_id: str, match_query: str, limit: int
) -> list[tuple[str, str]]:
    return conn.execute(
        """
        SELECT messages.content, messages.created_at
        FROM messages_fts
        JOIN messages ON messages.id = messages_fts.rowid
        WHERE messages_fts MATCH ? AND messages.assistant_id = ?
        ORDER BY bm25(messages_fts), messages.id DESC
        LIMIT ?
        """,
        (match_query, assistant_id, limit),
    ).fetchall()


def register_article(conn: sqlite3.Connection, assistant_id: str, session_id: str | None, title: str, path: str) -> None:
    conn.execute(
        "INSERT INTO articles (assistant_id, session_id, title, path, created_at) VALUES (?,?,?,?,?)",
        (assistant_id, session_id, title, path, _now()),
    )
    conn.commit()


def search_articles(conn: sqlite3.Connection, assistant_id: str, like_clauses: list[str], limit: int) -> list[tuple[str, str, str]]:
    if not like_clauses:
        return []
    where = " OR ".join("title LIKE ? ESCAPE '\\'" for _ in like_clauses)
    sql = f"SELECT title, path, created_at FROM articles WHERE assistant_id = ? AND ({where}) ORDER BY id DESC LIMIT ?"
    return conn.execute(sql, (assistant_id, *like_clauses, limit)).fetchall()


def search_articles_fts(
    conn: sqlite3.Connection, assistant_id: str, match_query: str, limit: int
) -> list[tuple[str, str, str]]:
    return conn.execute(
        """
        SELECT articles.title, articles.path, articles.created_at
        FROM articles_fts
        JOIN articles ON articles.id = articles_fts.rowid
        WHERE articles_fts MATCH ? AND articles.assistant_id = ?
        ORDER BY bm25(articles_fts), articles.id DESC
        LIMIT ?
        """,
        (match_query, assistant_id, limit),
    ).fetchall()


def recent_articles(conn: sqlite3.Connection, assistant_id: str, limit: int) -> list[tuple[str, str, str]]:
    return conn.execute(
        "SELECT title, path, created_at FROM articles WHERE assistant_id = ? ORDER BY id DESC LIMIT ?",
        (assistant_id, limit),
    ).fetchall()


def list_articles(conn: sqlite3.Connection, assistant_id: str) -> list[tuple[int, str, str, str, str]]:
    return conn.execute(
        "SELECT id, assistant_id, title, path, created_at FROM articles "
        "WHERE assistant_id = ? ORDER BY id DESC",
        (assistant_id,),
    ).fetchall()


def get_article(
    conn: sqlite3.Connection, assistant_id: str, article_id: int
) -> tuple[int, str, str, str, str] | None:
    return conn.execute(
        "SELECT id, assistant_id, title, path, created_at FROM articles "
        "WHERE assistant_id = ? AND id = ?",
        (assistant_id, article_id),
    ).fetchone()


def save_source(conn: sqlite3.Connection, assistant_id: str, session_id: str | None, url: str, title: str, fulltext: str) -> None:
    conn.execute(
        "INSERT INTO sources (assistant_id, session_id, url, title, fulltext, fetched_at) VALUES (?,?,?,?,?,?)",
        (assistant_id, session_id, url, title, fulltext[:20000], _now()),
    )
    conn.commit()


def get_sources(conn: sqlite3.Connection, assistant_id: str, session_id: str | None, limit: int = 5) -> list[tuple[str, str, str]]:
    """回查本会话抓取的全文素材（审查 P1-4：sources 表不再只写不读），返回 (url, title, fulltext)。"""
    if session_id is not None:
        return conn.execute(
            "SELECT url, title, fulltext FROM sources WHERE assistant_id = ? AND session_id = ? ORDER BY id DESC LIMIT ?",
            (assistant_id, session_id, limit),
        ).fetchall()
    return conn.execute(
        "SELECT url, title, fulltext FROM sources WHERE assistant_id = ? ORDER BY id DESC LIMIT ?",
        (assistant_id, limit),
    ).fetchall()


def delete_assistant_rows(conn: sqlite3.Connection, assistant_id: str) -> None:
    """--purge 专用：级联删除某助手的全部 SQL 数据。"""
    for table in ("sessions", "messages", "articles", "sources", "run_locks"):
        conn.execute(f"DELETE FROM {table} WHERE assistant_id = ?", (assistant_id,))
    conn.commit()
