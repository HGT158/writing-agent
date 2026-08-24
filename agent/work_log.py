"""项目聊天工作记录记录器（架构 §5.4/§5.7 v1.19）。

SSE 三类事件：work_item_start / work_item_delta / work_item_done。
delta 与 start 只走流；明细事件仅在 done 时落库，单任务明细上限 199 条，
第 200 位固定为溢出摘要，任务终态不受限、幂等写入。
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent.events import EventBus

import logging

logger = logging.getLogger(__name__)

REDACT_KEYS = ("api_key", "token", "authorization", "cookie", "secret", "password")
REDACTED = "***"
ARGS_MAX_CHARS = 4_000
RESULT_MAX_CHARS = 8_000
RESULT_HEAD_CHARS = 6_000
RESULT_TAIL_CHARS = 2_000
DETAIL_MAX_CHARS = 2_000
DETAIL_EVENT_LIMIT = 199
OVERFLOW_SEQ = 200
# 值级敏感串模式：常见密钥前缀与 key=value/JSON 内嵌形态（phase7 P2-3）。
_SECRET_VALUE_PATTERN = re.compile(
    r"(?i)(?:"
    r"sk-[a-z0-9_-]{8,}"                     # OpenAI/DeepSeek 风格
    r"|tvly-[a-z0-9_-]{8,}"                  # Tavily
    r"|bearer\s+[a-z0-9._~+/=-]{8,}"         # Authorization: Bearer …
    r"|(?:api[_-]?key|token|secret|password|authorization|cookie)"
    r"\s*[=:]\s*[\"']?([a-z0-9._~+/=-]{8,})"
    r")"
)


def _redact_secrets_in_text(text: str) -> str:
    """按键名与值级模式脱敏自由文本；捕获组形式的只替换捕获的值。"""
    def replace(match: re.Match) -> str:
        value = match.group(1)
        if value is None:
            return REDACTED
        return match.string[match.start():match.end() - len(value)] + REDACTED

    return _SECRET_VALUE_PATTERN.sub(replace, text)


_KIND_LABELS = {"progress": "进度", "tool": "工具", "warning": "警告", "changes": "修改建议"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(value):
    """递归脱敏：敏感键整值替换，字符串叶子追加值级扫描（架构 §5.7）。"""
    if isinstance(value, dict):
        return {
            key: (REDACTED if any(word in str(key).lower() for word in REDACT_KEYS)
                  else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _redact_secrets_in_text(value)
    return value


def summarize_detail(detail: str | None) -> str | None:
    """失败详情：截断到上限并做值级敏感串脱敏（异常文本是最可能携带凭据的载体）。"""
    if detail is None:
        return None
    text = _redact_secrets_in_text(str(detail))
    if len(text) > DETAIL_MAX_CHARS:
        return text[:DETAIL_MAX_CHARS] + f"…[详情已截断：原始 {len(text)} 字符]"
    return text


def _redact_string(text: str) -> str:
    """字符串形态的 JSON 载荷先解析再递归脱敏；解析失败按原文返回（phase7 P1-1）。"""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return text
    if not isinstance(parsed, (dict, list)):
        # 标量 JSON（"3"、true 等）与普通文本无法区分，保留原文。
        return text
    return json.dumps(redact(parsed), ensure_ascii=False)


def summarize_args(args) -> str | None:
    if args is None:
        return None
    if isinstance(args, (dict, list)):
        text = json.dumps(redact(args), ensure_ascii=False)
    elif isinstance(args, str):
        text = _redact_string(args)
    else:
        text = str(args)
    if len(text) > ARGS_MAX_CHARS:
        return text[:ARGS_MAX_CHARS] + f"…[参数已截断：原始 {len(text)} 字符]"
    return text


def summarize_result(result) -> str | None:
    if result is None:
        return None
    if isinstance(result, str):
        text = _redact_string(result)
    else:
        text = json.dumps(redact(result), ensure_ascii=False)
    if len(text) <= RESULT_MAX_CHARS:
        return text
    head = text[:RESULT_HEAD_CHARS]
    tail = text[-RESULT_TAIL_CHARS:]
    return f"{head}\n…[结果已截断：保留前 {RESULT_HEAD_CHARS} 与后 {RESULT_TAIL_CHARS} 字符，原始 {len(text)} 字符]\n{tail}"


@dataclass
class _WorkItem:
    work_id: str
    seq: int
    kind: str
    title: str
    status: str = "running"
    tool_name: str | None = None
    args_summary: str | None = None
    result_summary: str | None = None
    change_set_id: str | None = None
    document_id: str | None = None
    detail: str = ""
    created_at: str = field(default_factory=_now)
    completed_at: str | None = None


class WorkLogRecorder:
    """一个聊天任务的工作记录：SSE 流式投影 + done 时受控落库。"""

    def __init__(
        self,
        store,
        bus: EventBus,
        *,
        assistant_id: str,
        project_id: str,
        chat_session_id: str,
        task_id: str,
        user_message_id: int,
    ) -> None:
        self.store = store
        self.bus = bus
        self.assistant_id = assistant_id
        self.project_id = project_id
        self.chat_session_id = chat_session_id
        self.task_id = task_id
        self.user_message_id = user_message_id
        self.started_at = _now()
        self._items: dict[str, _WorkItem] = {}
        self._next_seq = 1
        self._dropped_counts: dict[str, int] = {}
        self._dropped = 0
        self._finished = False

    def start(
        self,
        kind: str,
        title: str,
        *,
        tool_name: str | None = None,
        args=None,
        change_set_id: str | None = None,
        document_id: str | None = None,
    ) -> str:
        work_id = uuid.uuid4().hex[:10]
        item = _WorkItem(
            work_id=work_id,
            seq=self._next_seq,
            kind=kind,
            title=title,
            tool_name=tool_name,
            args_summary=summarize_args(args),
            change_set_id=change_set_id,
            document_id=document_id,
        )
        self._next_seq += 1
        self._items[work_id] = item
        self.bus.emit(
            "work_item_start",
            work_id=work_id,
            kind=kind,
            title=title,
            tool_name=tool_name,
            args_summary=item.args_summary,
            change_set_id=change_set_id,
            document_id=document_id,
        )
        return work_id

    def delta(self, work_id: str, text: str) -> None:
        """进度增量只走 SSE，永不落库。"""
        self.bus.emit("work_item_delta", work_id=work_id, text=text)

    def done(
        self,
        work_id: str,
        *,
        status: str = "succeeded",
        detail: str = "",
        result=None,
    ) -> None:
        item = self._items.get(work_id)
        if item is None or item.status != "running":
            return
        item.status = status
        item.completed_at = _now()
        if result is not None:
            item.result_summary = summarize_result(result)
        safe_detail = summarize_detail(detail) or ""
        item.detail = safe_detail
        self.bus.emit(
            "work_item_done",
            work_id=work_id,
            kind=item.kind,
            status=status,
            title=item.title,
            detail=safe_detail,
            result_summary=item.result_summary,
        )
        if item.seq <= DETAIL_EVENT_LIMIT:
            try:
                self._persist_item(item)
            except Exception:
                # 明细落库失败只降级为 warning 工作项，不得打断任务
                # （对齐终态写入"只记 warning 不掩盖原始错误"原则，phase7 P2-2）。
                self._note_persist_failure(item)
        else:
            self._dropped += 1
            self._dropped_counts[item.kind] = self._dropped_counts.get(item.kind, 0) + 1

    def _persist_item(self, item: _WorkItem) -> None:
        self.store.add_project_chat_work_event(
            self.assistant_id,
            self.project_id,
            self.chat_session_id,
            task_id=self.task_id,
            user_message_id=self.user_message_id,
            event_seq=item.seq,
            kind=item.kind,
            status=item.status,
            title=item.title,
            detail=item.detail,
            tool_name=item.tool_name,
            args_summary=item.args_summary,
            result_summary=item.result_summary,
            change_set_id=item.change_set_id,
            document_id=item.document_id,
            created_at=item.created_at,
            completed_at=item.completed_at,
        )

    def _note_persist_failure(self, item: _WorkItem) -> None:
        """落库失败时补一条 warning 工作项（尽力而为，自身失败只忽略）。"""
        logger.warning(
            "工作记录明细落库失败（assistant=%s project=%s task=%s seq=%s）",
            self.assistant_id, self.project_id, self.task_id, item.seq, exc_info=True,
        )
        warning = _WorkItem(
            work_id=uuid.uuid4().hex[:10],
            # 原明细未落库，直接复用它留下的序号空缺；不能递增到 200，
            # 该位置固定保留给溢出摘要。
            seq=item.seq,
            kind="warning",
            title=f"工作记录明细落库失败：{item.title}",
        )
        warning.status = "succeeded"
        warning.completed_at = _now()
        self._items[warning.work_id] = warning
        # 实时视图按 start 建条目：降级 warning 若只发 done，前端对未知
        # work_id 会静默丢弃，补偿信号永远不可见（phase8 P2-1）。
        self.bus.emit(
            "work_item_start",
            work_id=warning.work_id,
            kind="warning",
            title=warning.title,
            tool_name=None,
            args_summary=None,
            change_set_id=None,
            document_id=None,
        )
        self.bus.emit(
            "work_item_done",
            work_id=warning.work_id,
            kind="warning",
            status="succeeded",
            title=warning.title,
            detail="该明细未持久化，刷新后不可回看",
            result_summary=None,
        )
        try:
            self._persist_item(warning)
        except Exception:
            logger.debug("工作记录降级 warning 落库仍失败", exc_info=True)

    def note(self, kind: str, title: str) -> str:
        """瞬时事件（warning 等）：start 与 done 连续完成。"""
        work_id = self.start(kind, title)
        self.done(work_id)
        return work_id

    def interrupt_running(self) -> None:
        """可控失败/取消时，运行中的工作项统一以 interrupted 终结落库。"""
        for item in list(self._items.values()):
            if item.status == "running":
                self.done(item.work_id, status="interrupted")

    def finish_task(self, status: str, *, title: str = "任务", detail: str = "") -> None:
        """写任务终态与溢出摘要；终态不受明细上限约束且幂等。"""
        if self._finished:
            return
        self._finished = True
        self.interrupt_running()
        if self._dropped:
            parts = "；".join(
                f"{_KIND_LABELS.get(kind, kind)} {count} 条"
                for kind, count in self._dropped_counts.items()
            )
            summary = f"省略 {self._dropped} 条记录" + (f"（{parts}）" if parts else "")
            self.store.add_project_chat_work_event(
                self.assistant_id,
                self.project_id,
                self.chat_session_id,
                task_id=self.task_id,
                user_message_id=self.user_message_id,
                event_seq=OVERFLOW_SEQ,
                kind="progress",
                status="succeeded",
                title=summary,
                detail="超出单任务 199 条明细上限，仅保留前 199 条",
                created_at=self.started_at,
                completed_at=_now(),
            )
        terminal_seq = max(self._next_seq - 1, OVERFLOW_SEQ if self._dropped else 0) + 1
        self.store.add_project_chat_work_event(
            self.assistant_id,
            self.project_id,
            self.chat_session_id,
            task_id=self.task_id,
            user_message_id=self.user_message_id,
            event_seq=terminal_seq,
            kind="task",
            status=status,
            title=title,
            detail=summarize_detail(detail) or "",
            created_at=self.started_at,
            completed_at=_now(),
        )
