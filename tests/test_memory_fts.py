"""Stage 3 memory retrieval: FTS5 trigram with a short-query LIKE fallback."""
from __future__ import annotations

import sqlite3

import pytest

from memory.store import MemoryStore, _like_patterns


def _schema_sql(db_path, name: str) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
        ).fetchone()
        return "" if row is None else row[0]
    finally:
        conn.close()


def test_trigram_indexes_new_messages_and_articles(tmp_path):
    store = MemoryStore(tmp_path)
    store.add_message("tech-writer", "s1", "user", "请写一篇模型蒸馏实践指南")
    store.memorize(
        "tech-writer",
        "article",
        "模型蒸馏入门 | data/articles/tech-writer/distillation.md",
        session_id="s1",
    )
    for index in range(4):
        store.memorize(
            "tech-writer",
            "article",
            f"无关主题 {index} | data/articles/tech-writer/unrelated-{index}.md",
            session_id="s1",
        )

    recalled = store.recall("tech-writer", "再次分析模型蒸馏的工程实践")

    assert "模型蒸馏入门" in recalled
    assert "请写一篇模型蒸馏实践指南" in recalled
    assert "fts5" in _schema_sql(tmp_path / "app.db", "messages_fts").lower()
    assert "trigram" in _schema_sql(tmp_path / "app.db", "articles_fts").lower()
    store.close()


def test_query_shorter_than_three_characters_falls_back_to_like(tmp_path):
    store = MemoryStore(tmp_path)
    store.add_message("tech-writer", "s1", "user", "正文应该突出蒸馏后的推理速度")

    recalled = store.recall("tech-writer", "蒸")

    assert "正文应该突出蒸馏后的推理速度" in recalled
    store.close()


def test_first_fts_initialization_backfills_existing_rows(tmp_path):
    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assistant_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO messages (assistant_id, session_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("tech-writer", "legacy", "user", "历史记录讨论知识蒸馏部署", "2026-08-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    store = MemoryStore(tmp_path)

    assert "历史记录讨论知识蒸馏部署" in store.recall("tech-writer", "知识蒸馏")
    assert "trigram" in _schema_sql(db_path, "messages_fts").lower()
    store.close()


def test_incomplete_fts_migration_rebuilds_existing_empty_index(tmp_path):
    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assistant_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assistant_id TEXT NOT NULL,
            session_id TEXT,
            title TEXT NOT NULL,
            path TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content, assistant_id UNINDEXED,
            content='messages', content_rowid='id', tokenize='trigram'
        );
        CREATE VIRTUAL TABLE articles_fts USING fts5(
            title, assistant_id UNINDEXED,
            content='articles', content_rowid='id', tokenize='trigram'
        );
        """
    )
    conn.execute(
        "INSERT INTO messages (assistant_id, session_id, role, content, created_at) VALUES (?,?,?,?,?)",
        ("tech-writer", "legacy", "user", "未回填的模型蒸馏记录", "2026-08-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    store = MemoryStore(tmp_path)

    assert "未回填的模型蒸馏记录" in store.recall("tech-writer", "模型蒸馏")
    store.close()


def test_old_fts_tokenizer_is_replaced_with_trigram(tmp_path):
    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assistant_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assistant_id TEXT NOT NULL,
            session_id TEXT,
            title TEXT NOT NULL,
            path TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content, assistant_id UNINDEXED,
            content='messages', content_rowid='id', tokenize='unicode61'
        );
        CREATE VIRTUAL TABLE articles_fts USING fts5(
            title, assistant_id UNINDEXED,
            content='articles', content_rowid='id', tokenize='unicode61'
        );
        PRAGMA user_version = 1;
        """
    )
    conn.commit()
    conn.close()

    store = MemoryStore(tmp_path)

    assert "trigram" in _schema_sql(db_path, "messages_fts").lower()
    assert "trigram" in _schema_sql(db_path, "articles_fts").lower()
    store.close()


def test_short_tokens_fall_back_to_like_search(tmp_path):
    store = MemoryStore(tmp_path)
    store.add_message("tech-writer", "s1", "user", "写一篇AI文章，介绍模型蒸馏")
    try:
        recalled = store.recall("tech-writer", "写 AI 文章")
    finally:
        store.close()

    assert "写一篇AI文章，介绍模型蒸馏" in recalled


def test_like_fallback_caps_patterns_and_keeps_tail_terms():
    tokens = [chr(0x4E00 + index) for index in range(50)]

    patterns = _like_patterns(" ".join(tokens))

    assert len(patterns) == 16
    assert patterns[0] == f"%{tokens[0]}%"
    assert patterns[-1] == f"%{tokens[-1]}%"


@pytest.mark.parametrize(
    ("query", "literal_content", "expanded_content"),
    [
        ("a_", "marker a_ marker", "marker xab marker"),
        ("a%", "marker a% marker", "marker abc marker"),
        ("%", "marker % marker", "marker ordinary marker"),
        (r"a\b", r"marker a\b marker", "marker alpha beta marker"),
    ],
)
def test_like_fallback_treats_wildcards_as_literals(
    tmp_path, query, literal_content, expanded_content
):
    store = MemoryStore(tmp_path)
    store.add_message("tech-writer", "s1", "user", literal_content)
    store.add_message("tech-writer", "s1", "user", expanded_content)
    try:
        recalled = store.recall("tech-writer", query)
    finally:
        store.close()

    assert literal_content in recalled
    assert expanded_content not in recalled


def test_long_query_includes_topic_terms_from_the_tail(tmp_path):
    store = MemoryStore(tmp_path)
    store.add_message("tech-writer", "s1", "user", "量子计算是未来重点")
    query = "请按照以下要求撰写一篇结构严谨内容完整包含引用和案例的技术文章 量子计算"
    try:
        recalled = store.recall("tech-writer", query)
    finally:
        store.close()

    assert "量子计算是未来重点" in recalled


def test_invalid_profile_encoding_does_not_block_database_recall(tmp_path, caplog):
    store = MemoryStore(tmp_path)
    store.memorize(
        "tech-writer",
        "article",
        "模型蒸馏入门 | data/articles/tech-writer/distillation.md",
        session_id="s1",
    )
    profile = tmp_path / "assistants" / "tech-writer" / "memory" / "profile.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_bytes(b"\xff\xfe\xfa")
    try:
        recalled = store.recall("tech-writer", "模型蒸馏")
    finally:
        store.close()

    assert "模型蒸馏入门" in recalled
    assert "profile" in caplog.text


def test_fts_failure_falls_back_to_recent_articles(tmp_path, caplog):
    store = MemoryStore(tmp_path)
    store.memorize(
        "tech-writer",
        "article",
        "模型蒸馏入门 | data/articles/tech-writer/distillation.md",
        session_id="s1",
    )
    conn = sqlite3.connect(str(tmp_path / "app.db"))
    conn.executescript(
        """
        DROP TRIGGER articles_fts_ai;
        DROP TRIGGER articles_fts_ad;
        DROP TRIGGER articles_fts_au;
        DROP TABLE articles_fts;
        """
    )
    conn.close()
    try:
        recalled = store.recall("tech-writer", "模型蒸馏")
    finally:
        store.close()

    assert "模型蒸馏入门" in recalled
    assert "FTS" in caplog.text
