"""Memory 统一入口（架构 §5.7）。

红线：所有接口把 assistant_id 作为第一个必填参数，从签名层面杜绝串记忆。
"""
from __future__ import annotations

import os
import logging
import re
import sqlite3
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Iterable, Literal

import psutil

from . import long_term, project_chat, projects, short_term
from .project_chat import (
    ProjectChatMessageRecord,
    ProjectChatSessionRecord,
    ProjectChatSummaryRecord,
)
from .projects import ChangeSetRecord, DocumentRecord, ProjectRecord
from .validation import validate_id

logger = logging.getLogger(__name__)


def _process_started_at(pid: int) -> float:
    try:
        return float(psutil.Process(pid).create_time())
    except (psutil.Error, OSError):
        return 0.0


@dataclass(frozen=True)
class ArticleRecord:
    article_id: int
    assistant_id: str
    title: str
    path: str
    created_at: str


@dataclass(frozen=True)
class RecallTrace:
    """recall 的结构化命中结果（架构 §5.7 v1.30）：文本组装 + 可观测命中。"""

    profile_text: str
    profile_entries: int
    article_hits: list[tuple[str, str, str]]  # (title, path, created_at)
    message_hits: list[tuple[str, str]]       # (content, created_at)
    degraded: list[str]                       # 分路降级标记（只观测，不二次查询）
    text: str                                 # 与 recall 返回一致的组装文本


class AssistantBusyError(RuntimeError):
    """同一助手已有运行中任务（run_locks 主键冲突且锁未过期/持有进程仍存活）。"""

    def __init__(self, assistant_id: str, detail: str = "") -> None:
        super().__init__(f"助手 {assistant_id} 正忙{('：' + detail) if detail else ''}")
        self.assistant_id = assistant_id


def _query_segments(query: str) -> list[str]:
    return re.findall(r"[\w一-鿿]+", query, flags=re.UNICODE)


def _fts_query(query: str, cap: int = 16) -> str:
    """Build a bounded literal OR query with samples spanning the whole task."""
    terms: list[str] = []
    for segment in _query_segments(query):
        if len(segment) < 3:
            continue
        if re.search(r"[一-鿿]", segment):
            candidates = [segment[i : i + 3] for i in range(len(segment) - 2)]
        else:
            candidates = [segment]
        for candidate in candidates:
            if candidate not in terms:
                terms.append(candidate)
    if not terms:
        return ""
    if len(terms) > cap:
        indexes = [index * (len(terms) - 1) // (cap - 1) for index in range(cap)]
        terms = [terms[index] for index in indexes]
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def _like_patterns(query: str, cap: int = 16) -> list[str]:
    tokens = query.split()
    patterns: list[str] = []
    for token in tokens:
        escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        if pattern not in patterns:
            patterns.append(pattern)
    if len(patterns) > cap:
        indexes = [index * (len(patterns) - 1) // (cap - 1) for index in range(cap)]
        patterns = [patterns[index] for index in indexes]
    return patterns


class MemoryStore:
    def __init__(self, data_dir: Path | str, *, run_lock_ttl_hours: float = 2.0) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.run_lock_ttl_hours = run_lock_ttl_hours
        self._conn = sqlite3.connect(
            str(self.data_dir / "app.db"),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        short_term.create_tables(self._conn)
        projects.create_tables(self._conn)
        project_chat.create_tables(self._conn)
        projects.recover_project_artifacts(self._conn, self.data_dir)

    def close(self) -> None:
        self._conn.close()

    # ---------- 短期 ----------

    def create_session(self, assistant_id: str, session_id: str, task: str) -> None:
        with self._lock:
            short_term.create_session(self._conn, assistant_id, session_id, task)

    def validate_session_owner(self, assistant_id: str, session_id: str) -> None:
        with self._lock:
            owners = short_term.session_assistant_ids(self._conn, session_id)
        if not owners:
            raise ValueError("resume session 不存在")
        if assistant_id not in owners:
            raise ValueError("resume session 不属于当前助手")

    def add_message(self, assistant_id: str, session_id: str, role: str, content: str) -> None:
        with self._lock:
            short_term.add_message(self._conn, assistant_id, session_id, role, content[:8000])

    def save_source(self, assistant_id: str, session_id: str | None, url: str, title: str, fulltext: str) -> None:
        with self._lock:
            short_term.save_source(self._conn, assistant_id, session_id, url, title, fulltext)

    def get_sources(self, assistant_id: str, session_id: str | None, *, limit: int = 5) -> list[tuple[str, str, str]]:
        """回查全文素材，返回 (url, title, fulltext)，供成文节点注入（审查 P1-4）。"""
        with self._lock:
            return short_term.get_sources(self._conn, assistant_id, session_id, limit)

    # ---------- 项目 Agent 会话 ----------

    def create_project_chat_session(
        self, assistant_id: str, project_id: str
    ) -> ProjectChatSessionRecord:
        with self._lock:
            return project_chat.create_session(self._conn, assistant_id, project_id)

    def list_project_chat_sessions(
        self, assistant_id: str, project_id: str
    ) -> list[ProjectChatSessionRecord]:
        with self._lock:
            return project_chat.list_sessions(self._conn, assistant_id, project_id)

    def get_project_chat_session(
        self, assistant_id: str, project_id: str, chat_session_id: str
    ) -> ProjectChatSessionRecord:
        with self._lock:
            return project_chat.get_session(
                self._conn, assistant_id, project_id, chat_session_id
            )

    def list_project_chat_messages(
        self, assistant_id: str, project_id: str, chat_session_id: str
    ) -> list[ProjectChatMessageRecord]:
        with self._lock:
            return project_chat.list_messages(
                self._conn, assistant_id, project_id, chat_session_id
            )

    def add_project_chat_message(
        self,
        assistant_id: str,
        project_id: str,
        chat_session_id: str,
        role: str,
        content: str,
    ) -> ProjectChatMessageRecord:
        with self._lock:
            return project_chat.add_message(
                self._conn,
                assistant_id,
                project_id,
                chat_session_id,
                role,
                content,
            )

    def get_project_chat_summary(
        self, assistant_id: str, project_id: str, chat_session_id: str
    ) -> ProjectChatSummaryRecord | None:
        with self._lock:
            return project_chat.get_summary(
                self._conn, assistant_id, project_id, chat_session_id
            )

    def save_project_chat_summary(
        self,
        assistant_id: str,
        project_id: str,
        chat_session_id: str,
        summary: str,
        covered_through_message_id: int,
    ) -> ProjectChatSummaryRecord:
        with self._lock:
            return project_chat.save_summary(
                self._conn,
                assistant_id,
                project_id,
                chat_session_id,
                summary,
                covered_through_message_id,
            )

    def list_pending_chat_changes(
        self, assistant_id: str, project_id: str, chat_session_id: str
    ) -> list[ChangeSetRecord]:
        with self._lock:
            project_chat.get_session(
                self._conn, assistant_id, project_id, chat_session_id
            )
            return projects.list_pending_chat_changes(
                self._conn, assistant_id, project_id, chat_session_id
            )

    def delete_project_chat_session(
        self, assistant_id: str, project_id: str, chat_session_id: str
    ) -> None:
        mutation_task = f"project-chat-delete-{uuid.uuid4().hex[:12]}"
        self.acquire_lock(assistant_id, mutation_task)
        try:
            with self._lock:
                project_chat.delete_session(
                    self._conn, assistant_id, project_id, chat_session_id
                )
        finally:
            self.release_lock(assistant_id, mutation_task)

    def delete_empty_project_chat_session(
        self, assistant_id: str, project_id: str, chat_session_id: str
    ) -> bool:
        with self._lock:
            return project_chat.delete_empty_session(
                self._conn, assistant_id, project_id, chat_session_id
            )

    def add_project_chat_work_event(
        self,
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
    ) -> project_chat.ProjectChatWorkEventRecord:
        with self._lock:
            return project_chat.add_work_event(
                self._conn, assistant_id, project_id, chat_session_id,
                task_id=task_id, user_message_id=user_message_id, event_seq=event_seq,
                kind=kind, status=status, title=title, detail=detail,
                tool_name=tool_name, args_summary=args_summary,
                result_summary=result_summary, change_set_id=change_set_id,
                document_id=document_id, created_at=created_at,
                completed_at=completed_at,
            )

    def list_project_chat_work_events(
        self, assistant_id: str, project_id: str, chat_session_id: str
    ) -> list[project_chat.ProjectChatWorkEventRecord]:
        with self._lock:
            return project_chat.list_work_events(
                self._conn, assistant_id, project_id, chat_session_id
            )

    def list_unfinished_project_chat_work_task_ids(
        self, assistant_id: str, project_id: str, chat_session_id: str
    ) -> list[str]:
        with self._lock:
            return project_chat.list_unfinished_work_task_ids(
                self._conn, assistant_id, project_id, chat_session_id
            )

    def interrupt_project_chat_work_task(
        self, assistant_id: str, project_id: str, chat_session_id: str, task_id: str
    ) -> None:
        with self._lock:
            project_chat.interrupt_work_task(
                self._conn, assistant_id, project_id, chat_session_id, task_id
            )

    # ---------- 长期 ----------

    def recall(self, assistant_id: str, query: str, *, limit: int = 10) -> str:
        """检索范围 = 本助手 profile.md + 本助手历史文章/消息（跨助手物理不可见）。"""
        return self.recall_trace(assistant_id, query, limit=limit).text

    def recall_trace(self, assistant_id: str, query: str, *, limit: int = 10) -> RecallTrace:
        """结构化命中（v1.30）：与 recall 相同的检索与降级语义，另暴露命中明细。"""
        parts: list[str] = []
        degraded: list[str] = []
        try:
            profile = long_term.read_profile(self.data_dir, assistant_id)
        except (OSError, UnicodeError):
            logger.warning("recall profile 读取失败（assistant=%s）", assistant_id, exc_info=True)
            profile = ""
            degraded.append("profile")
        if profile:
            parts.append("## 长期画像（风格/偏好/常用主题）\n" + profile)

        normalized_query = query.strip()
        articles: list[tuple[str, str, str]] = []
        messages: list[tuple[str, str]] = []
        recent: list[tuple[str, str, str]] = []
        with self._lock:
            match_query = _fts_query(normalized_query) if len(normalized_query) >= 3 else ""
            if match_query:
                try:
                    articles = short_term.search_articles_fts(self._conn, assistant_id, match_query, limit)
                except sqlite3.Error:
                    logger.warning("FTS 文章 recall 失败（assistant=%s）", assistant_id, exc_info=True)
                    degraded.append("articles_fts")
                try:
                    messages = short_term.search_messages_fts(self._conn, assistant_id, match_query, limit)
                except sqlite3.Error:
                    logger.warning("FTS 消息 recall 失败（assistant=%s）", assistant_id, exc_info=True)
                    degraded.append("messages_fts")
            elif normalized_query:
                logger.debug("recall 使用 LIKE 降级（assistant=%s, query=%r）", assistant_id, normalized_query)
                like = _like_patterns(normalized_query)
                try:
                    articles = short_term.search_articles(self._conn, assistant_id, like, limit)
                except sqlite3.Error:
                    logger.warning("LIKE 文章 recall 失败（assistant=%s）", assistant_id, exc_info=True)
                    degraded.append("articles_like")
                try:
                    messages = short_term.search_messages(self._conn, assistant_id, like, limit)
                except sqlite3.Error:
                    logger.warning("LIKE 消息 recall 失败（assistant=%s）", assistant_id, exc_info=True)
                    degraded.append("messages_like")
            try:
                recent = short_term.recent_articles(self._conn, assistant_id, 3)
            except sqlite3.Error:
                logger.warning("最近文章 recall 失败（assistant=%s）", assistant_id, exc_info=True)
                degraded.append("recent_articles")

        seen = {a[1] for a in articles}
        merged = articles + [a for a in recent if a[1] not in seen]
        if merged:
            lines = [f"- 《{t}》（{ts[:10]}）{p}" for t, p, ts in merged]
            parts.append("## 本助手历史文章索引\n" + "\n".join(lines))
        if messages:
            snippets = [f"- {c[:150]}" for c, _ in messages[:5]]
            parts.append("## 相关历史对话片段\n" + "\n".join(snippets))
        profile_entries = sum(
            1 for line in profile.splitlines() if line.strip().startswith("- ")
        )
        return RecallTrace(
            profile_text=profile,
            profile_entries=profile_entries,
            article_hits=merged,
            message_hits=messages,
            degraded=degraded,
            text="\n\n".join(parts),
        )

    def memorize(
        self,
        assistant_id: str,
        kind: Literal["preference", "style", "topic", "article"],
        content: str,
        *,
        session_id: str | None = None,
    ) -> None:
        """kind=article 时 content 格式为 "标题 | 文件路径"，登记 articles 索引；其余改写本助手 profile.md。"""
        if kind == "article":
            title, _, path = content.partition("|")
            with self._lock:
                short_term.register_article(self._conn, assistant_id, session_id, title.strip(), path.strip())
        else:
            long_term.append_profile(self.data_dir, assistant_id, kind, content)

    def recall_semantic(self, assistant_id: str, query: str) -> str:
        """预留向量检索接口（架构 §5.7，后期扩展）。"""
        raise NotImplementedError("向量检索为预留接口，当前使用关键词检索")

    # ---------- 长期画像白盒读写（v1.30） ----------

    def get_assistant_profile(self, assistant_id: str) -> str:
        """profile.md 原文；文件不存在返回空串。"""
        return long_term.read_profile(self.data_dir, assistant_id)

    def replace_assistant_profile(self, assistant_id: str, content: str) -> None:
        """整文替换 profile.md（白盒）：原样写入、不重排；超上限抛 ValueError。"""
        long_term.replace_profile(self.data_dir, assistant_id, content)

    # ---------- 文章项目（架构 §4.7 / §5.7） ----------

    def create_project(self, assistant_id: str, name: str) -> ProjectRecord:
        with self._lock:
            return projects.create_project(self._conn, self.data_dir, assistant_id, name)

    def list_projects(self, assistant_id: str) -> list[ProjectRecord]:
        with self._lock:
            return projects.list_projects(self._conn, assistant_id)

    def rename_project(self, assistant_id: str, project_id: str, name: str) -> ProjectRecord:
        with self._lock:
            return projects.rename_project(self._conn, assistant_id, project_id, name)

    def rename_document(
        self, assistant_id: str, project_id: str, document_id: str, relative_path: str
    ):
        mutation_task = f"document-rename-{uuid.uuid4().hex[:12]}"
        self.acquire_lock(assistant_id, mutation_task)
        try:
            with self._lock:
                return projects.rename_document(
                    self._conn, self.data_dir, assistant_id, project_id,
                    document_id, relative_path,
                )
        finally:
            self.release_lock(assistant_id, mutation_task)

    def delete_document(
        self, assistant_id: str, project_id: str, document_id: str
    ) -> dict:
        mutation_task = f"document-delete-{uuid.uuid4().hex[:12]}"
        self.acquire_lock(assistant_id, mutation_task)
        try:
            with self._lock:
                return projects.delete_document(
                    self._conn, self.data_dir, assistant_id, project_id, document_id
                )
        finally:
            self.release_lock(assistant_id, mutation_task)

    def archive_project(self, assistant_id: str, project_id: str) -> Path:
        mutation_task = f"project-archive-{uuid.uuid4().hex[:12]}"
        self.acquire_lock(assistant_id, mutation_task)
        try:
            with self._lock:
                return projects.archive_project(
                    self._conn, self.data_dir, assistant_id, project_id
                )
        finally:
            self.release_lock(assistant_id, mutation_task)

    def purge_project(self, assistant_id: str, project_id: str) -> None:
        mutation_task = f"project-purge-{uuid.uuid4().hex[:12]}"
        self.acquire_lock(assistant_id, mutation_task)
        try:
            with self._lock:
                projects.purge_project(
                    self._conn, self.data_dir, assistant_id, project_id
                )
        finally:
            self.release_lock(assistant_id, mutation_task)

    def _reject_project_mutation_while_running(self, assistant_id: str) -> None:
        running = self._live_lock_locked(assistant_id)
        if running is not None:
            raise AssistantBusyError(assistant_id, f"任务 {running[0]} 运行中，拒绝删除项目")

    def list_articles(self, assistant_id: str) -> list[ArticleRecord]:
        with self._lock:
            rows = short_term.list_articles(self._conn, assistant_id)
        return [ArticleRecord(*row) for row in rows]

    def get_article(self, assistant_id: str, article_id: int) -> tuple[ArticleRecord, str]:
        with self._lock:
            row = short_term.get_article(self._conn, assistant_id, article_id)
        if row is None:
            raise KeyError(f"文章不存在：{article_id}")
        record = ArticleRecord(*row)
        raw_path = Path(record.path)
        candidates = [raw_path] if raw_path.is_absolute() else [self.data_dir / raw_path, self.data_dir.parent / raw_path]
        allowed_root = (self.data_dir / "articles" / assistant_id).resolve()
        path = next(
            (candidate.resolve() for candidate in candidates if candidate.resolve() == allowed_root or allowed_root in candidate.resolve().parents),
            None,
        )
        if path is None:
            raise ValueError("文章路径越界")
        if not path.is_file():
            raise FileNotFoundError(f"文章文件不存在：{path}")
        return record, path.read_text(encoding="utf-8-sig")

    def import_text_project(
        self, assistant_id: str, filename: str, stream: BinaryIO,
        *, max_files: int = 5000, max_total_bytes: int = 512 * 1024 * 1024,
        max_file_bytes: int = 100 * 1024 * 1024,
    ) -> ProjectRecord:
        with self._lock:
            return projects.import_text_project(
                self._conn, self.data_dir, assistant_id, filename, stream,
                max_files=max_files, max_total_bytes=max_total_bytes, max_file_bytes=max_file_bytes,
            )

    def import_folder_project(
        self, assistant_id: str, name: str, files: Iterable[tuple[str, BinaryIO]],
        *, max_files: int = 5000, max_total_bytes: int = 512 * 1024 * 1024,
        max_file_bytes: int = 100 * 1024 * 1024,
    ) -> ProjectRecord:
        with self._lock:
            return projects.import_folder_project(
                self._conn, self.data_dir, assistant_id, name, files,
                max_files=max_files, max_total_bytes=max_total_bytes, max_file_bytes=max_file_bytes,
            )

    def get_project_tree(self, assistant_id: str, project_id: str) -> list[DocumentRecord]:
        with self._lock:
            return projects.get_project_tree(self._conn, assistant_id, project_id)

    def get_document(
        self, assistant_id: str, project_id: str, document_id: str
    ) -> DocumentRecord:
        with self._lock:
            return projects.get_document(
                self._conn, self.data_dir, assistant_id, project_id, document_id
            )

    def save_document(
        self, assistant_id: str, project_id: str, document_id: str,
        content: str, *, expected_version: int,
    ) -> tuple[DocumentRecord, list[str]]:
        with self._lock:
            return projects.save_document(
                self._conn, self.data_dir, assistant_id, project_id,
                document_id, content, expected_version,
            )

    def create_change_set_hunks(
        self, assistant_id: str, project_id: str, *,
        task_id: str, source: str, documents: list[dict],
        session_id: str | None = None,
    ) -> list[ChangeSetRecord]:
        with self._lock:
            return projects.create_change_set_hunks(
                self._conn, self.data_dir, assistant_id, project_id,
                task_id=task_id, source=source, documents=documents,
                session_id=session_id,
            )

    def create_selection_change_set(
        self, assistant_id: str, project_id: str, document_id: str, *,
        task_id: str, start: int, end: int, original_text: str,
        replacement_text: str, base_version: int, source: str,
        session_id: str | None = None,
    ) -> ChangeSetRecord:
        with self._lock:
            return projects.create_selection_change_set(
                self._conn, self.data_dir, assistant_id, project_id, document_id,
                task_id=task_id, start=start, end=end, original_text=original_text,
                replacement_text=replacement_text, base_version=base_version,
                source=source, session_id=session_id,
            )

    def get_change_set(
        self, assistant_id: str, project_id: str, change_set_id: str
    ) -> ChangeSetRecord:
        with self._lock:
            return projects.get_change_set(
                self._conn, assistant_id, project_id, change_set_id
            )

    def accept_change_hunk(
        self, assistant_id: str, project_id: str, change_set_id: str, hunk_id: str
    ) -> tuple[DocumentRecord, ChangeSetRecord, "projects.ChangeSetHunkRecord", list[str]]:
        with self._lock:
            return projects.accept_change_hunk(
                self._conn, self.data_dir, assistant_id, project_id,
                change_set_id, hunk_id,
            )

    def reject_change_hunk(
        self, assistant_id: str, project_id: str, change_set_id: str, hunk_id: str
    ) -> ChangeSetRecord:
        with self._lock:
            return projects.reject_change_hunk(
                self._conn, self.data_dir, assistant_id, project_id,
                change_set_id, hunk_id,
            )

    def accept_all_change_hunks(
        self, assistant_id: str, project_id: str, change_set_id: str
    ) -> dict:
        with self._lock:
            return projects.accept_all_change_hunks(
                self._conn, self.data_dir, assistant_id, project_id, change_set_id
            )

    def list_change_sets_for_document(
        self, assistant_id: str, project_id: str, document_id: str, *,
        page: int = 1, page_size: int = 20,
    ) -> dict:
        with self._lock:
            return projects.list_change_sets_for_document(
                self._conn, assistant_id, project_id, document_id,
                page=page, page_size=page_size,
            )

    # ---------- 运行锁（架构 §4.6：跨进程，TTL + PID 存活校验） ----------

    def acquire_lock(self, assistant_id: str, task_id: str) -> None:
        """原子占位（INSERT OR IGNORE），杜绝 check-then-insert 竞态（审查 P0-2）。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO run_locks "
                "(assistant_id, task_id, pid, acquired_at, pid_started_at) VALUES (?,?,?,?,?)",
                (
                    assistant_id,
                    task_id,
                    os.getpid(),
                    datetime.now(timezone.utc).isoformat(),
                    _process_started_at(os.getpid()),
                ),
            )
            self._conn.commit()
            if cur.rowcount == 1:
                return  # 获锁成功

            # 已被占用：读出现有锁行，按 TTL + PID 存活校验判断是否可回收
            row = self._conn.execute(
                "SELECT task_id, pid, acquired_at, pid_started_at FROM run_locks WHERE assistant_id = ?", (assistant_id,)
            ).fetchone()
            if row is None:
                # 持有者恰在两次操作之间释放了锁（复审 R1 竞态窗口）：锁已空，顺势原子重试
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO run_locks "
                    "(assistant_id, task_id, pid, acquired_at, pid_started_at) VALUES (?,?,?,?,?)",
                    (
                        assistant_id,
                        task_id,
                        os.getpid(),
                        datetime.now(timezone.utc).isoformat(),
                        _process_started_at(os.getpid()),
                    ),
                )
                self._conn.commit()
                if cur.rowcount == 1:
                    return
                raise AssistantBusyError(assistant_id, "锁竞争失败，请重试")
            old_task, old_pid, old_at, old_started_at = row
            expired = self._lock_timestamp_expired(old_at)
            owner_reused = False
            if old_started_at:
                current_started_at = _process_started_at(old_pid)
                owner_reused = bool(
                    current_started_at
                    and abs(current_started_at - old_started_at) >= 1.0
                )
            if not expired and not owner_reused:
                raise AssistantBusyError(assistant_id, f"任务 {old_task} 运行中")
            if psutil.pid_exists(old_pid) and old_started_at and not owner_reused:
                raise AssistantBusyError(assistant_id, f"任务 {old_task} 已超时但进程 {old_pid} 仍存活，请人工检查")
            # 进程已死 → 强杀残留，回收后原子重试
            self._conn.execute(
                "DELETE FROM run_locks WHERE assistant_id = ? AND task_id = ?", (assistant_id, old_task)
            )
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO run_locks "
                "(assistant_id, task_id, pid, acquired_at, pid_started_at) VALUES (?,?,?,?,?)",
                (
                    assistant_id,
                    task_id,
                    os.getpid(),
                    datetime.now(timezone.utc).isoformat(),
                    _process_started_at(os.getpid()),
                ),
            )
            self._conn.commit()
            if cur.rowcount != 1:
                raise AssistantBusyError(assistant_id, "锁回收竞争失败，请重试")

    def release_lock(self, assistant_id: str, task_id: str | None = None) -> None:
        """只释放自己持有的锁（带 task_id 条件），防止误删回收后新持有者的锁（审查 P1-3）。"""
        with self._lock:
            if task_id is None:
                self._conn.execute("DELETE FROM run_locks WHERE assistant_id = ?", (assistant_id,))
            else:
                self._conn.execute(
                    "DELETE FROM run_locks WHERE assistant_id = ? AND task_id = ?", (assistant_id, task_id)
                )
            self._conn.commit()

    def is_locked(self, assistant_id: str) -> bool:
        with self._lock:
            return self._live_lock_locked(assistant_id) is not None

    def current_lock_task_id(self, assistant_id: str) -> str | None:
        """当前有效运行锁的任务 id；无锁或残留已被回收时为 None。"""
        with self._lock:
            live = self._live_lock_locked(assistant_id)
            return live[0] if live is not None else None

    def _live_lock_locked(self, assistant_id: str) -> tuple[str, int, str, float] | None:
        row = self._conn.execute(
            "SELECT task_id, pid, acquired_at, pid_started_at FROM run_locks WHERE assistant_id = ?",
            (assistant_id,),
        ).fetchone()
        if row is None:
            return None
        task_id, pid, acquired_at, pid_started_at = row
        expired = self._lock_timestamp_expired(acquired_at)
        owner_reused = False
        if pid_started_at:
            current_started_at = _process_started_at(pid)
            owner_reused = bool(
                current_started_at
                and abs(current_started_at - pid_started_at) >= 1.0
            )
        if owner_reused or (expired and (not pid_started_at or not psutil.pid_exists(pid))):
            self._conn.execute(
                "DELETE FROM run_locks WHERE assistant_id = ? AND task_id = ?",
                (assistant_id, task_id),
            )
            self._conn.commit()
            return None
        return row

    def _lock_timestamp_expired(self, value: str) -> bool:
        try:
            acquired_at = datetime.fromisoformat(value)
            if acquired_at.tzinfo is None:
                acquired_at = acquired_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return True
        return datetime.now(timezone.utc) - acquired_at > timedelta(
            hours=self.run_lock_ttl_hours
        )

    # ---------- 助手删除（--purge 级联） ----------

    def purge_assistant(self, assistant_id: str, *, owner_task_id: str | None = None) -> None:
        validate_id(assistant_id, "assistant_id")
        with self._lock:
            running = self._live_lock_locked(assistant_id)
            if running is not None and running[0] != owner_task_id:
                raise AssistantBusyError(assistant_id, f"任务 {running[0]} 运行中，拒绝清除助手")
            project_chat.delete_assistant_rows(self._conn, assistant_id)
            projects.delete_assistant_rows(self._conn, assistant_id)
            short_term.delete_assistant_rows(self._conn, assistant_id)
        shutil.rmtree(self.data_dir / "articles" / assistant_id, ignore_errors=True)
        # checkpoint 库单独一个文件（thread_id = <assistant_id>:<session_id>），一并清理
        cp_path = self.data_dir / "checkpoints.db"
        if cp_path.exists():
            conn = sqlite3.connect(str(cp_path))
            try:
                escaped = assistant_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                for table in ("checkpoints", "writes"):
                    try:
                        conn.execute(
                            f"DELETE FROM {table} WHERE thread_id LIKE ? ESCAPE '\\'",
                            (f"{escaped}:%",),
                        )
                    except sqlite3.OperationalError:
                        pass  # 表尚不存在
                conn.commit()
            finally:
                conn.close()
