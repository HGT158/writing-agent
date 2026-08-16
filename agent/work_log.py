"""项目聊天工作记录记录器（架构 §5.4/§5.7 v1.19）。

SSE 三类事件：work_item_start / work_item_delta / work_item_done。
delta 与 start 只走流；明细事件仅在 done 时落库，单任务明细上限 199 条，
第 200 位固定为溢出摘要，任务终态不受限、幂等写入。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent.events import EventBus

REDACT_KEYS = ("api_key", "token", "authorization", "cookie", "secret", "password")
REDACTED = "***"
ARGS_MAX_CHARS = 4_000
RESULT_MAX_CHARS = 8_000
RESULT_HEAD_CHARS = 6_000
RESULT_TAIL_CHARS = 2_000
DETAIL_EVENT_LIMIT = 199
OVERFLOW_SEQ = 200

_KIND_LABELS = {"progress": "进度", "tool": "工具", "warning": "警告", "changes": "修改建议"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(value):
    """递归脱敏：名称匹配敏感词的字段值替换为 ***（架构 §5.7）。"""
    if isinstance(value, dict):
        return {
            key: (REDACTED if any(word in str(key).lower() for word in REDACT_KEYS)
                  else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def summarize_args(args) -> str | None:
    if args is None:
        return None
    if isinstance(args, (dict, list)):
        text = json.dumps(redact(args), ensure_ascii=False)
    else:
        text = str(args)
    if len(text) > ARGS_MAX_CHARS:
        return text[:ARGS_MAX_CHARS] + f"…[参数已截断：原始 {len(text)} 字符]"
    return text


def summarize_result(result) -> str | None:
    if result is None:
        return None
    text = result if isinstance(result, str) else json.dumps(redact(result), ensure_ascii=False)
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
        self.bus.emit(
            "work_item_done",
            work_id=work_id,
            kind=item.kind,
            status=status,
            title=item.title,
            detail=detail,
            result_summary=item.result_summary,
        )
        if item.seq <= DETAIL_EVENT_LIMIT:
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
                detail=detail,
                tool_name=item.tool_name,
                args_summary=item.args_summary,
                result_summary=item.result_summary,
                change_set_id=item.change_set_id,
                document_id=item.document_id,
                created_at=item.created_at,
                completed_at=item.completed_at,
            )
        else:
            self._dropped += 1
            self._dropped_counts[item.kind] = self._dropped_counts.get(item.kind, 0) + 1

    def note(self, kind: str, title: str) -> str:
        """瞬时事件（warning 等）：start 与 done 连续完成。"""
        work_id = self.start(kind, title)
        self.done(work_id)
        return work_id

    def interrupt_running(self) -> None:
        """可控失败/取消时，运行中的工作项统一以 interrupted 终结落库。"""
        for item in self._items.values():
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
            detail=detail,
            created_at=self.started_at,
            completed_at=_now(),
        )
