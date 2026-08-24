"""后台任务状态与 SSE 事件归档。"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from agent.events import Event, EventBus


TERMINAL_EVENT_TYPES = {"task_done", "task_failed"}


@dataclass
class TaskRecord:
    task_id: str
    assistant_id: str
    status: str = "running"
    result: dict[str, Any] | None = None
    error: str | None = None
    events: list[Event] = field(default_factory=list)
    subscribers: set[asyncio.Queue[Event]] = field(default_factory=set)
    handle: asyncio.Task | None = None
    completed_at: datetime | None = None
    next_seq: int = 0
    dropped: int = 0


class TaskBroker:
    """事件按任务内单调递增的 seq 寻址（架构 §5.9）。

    `events` 是有界重放窗口，一次流式回复的 token 事件数很容易超过窗口容量，
    因此订阅者不能用列表下标定位：活跃订阅者从自己的队列取事件，窗口裁剪只让
    重连订阅者跳过已丢弃的历史，不会让任何订阅者停流或漏掉终态事件。
    断线重连的订阅者以 `after_seq`（或标准 SSE `Last-Event-ID`）恢复游标：窗口
    仍可覆盖时从游标后精确补发；游标落后于窗口时先发一条不带 seq 的
    `reconnect_gap` 控制事件，再继续活动流，终态事件始终送达。
    """

    def __init__(self, bus: EventBus, *, max_records: int = 128, max_events: int = 4096) -> None:
        self.bus = bus
        self.max_records = max_records
        self.max_events = max_events
        self.records: dict[str, TaskRecord] = {}
        self.bus.subscribe(self._on_event)

    def _record_event(self, record: TaskRecord, event: Event) -> None:
        stamped: Event = {**event, "seq": record.next_seq}
        record.next_seq += 1
        record.events.append(stamped)
        overflow = len(record.events) - self.max_events
        if overflow > 0:
            del record.events[:overflow]
            record.dropped += overflow
        for queue in tuple(record.subscribers):
            queue.put_nowait(stamped)

    def _on_event(self, event: Event) -> None:
        task_id = event.get("task_id")
        record = self.records.get(task_id) if task_id else None
        if record is not None:
            self._record_event(record, event)

    def _trim_records(self) -> None:
        overflow = len(self.records) - self.max_records
        if overflow <= 0:
            return
        removable = [
            task_id
            for task_id, record in self.records.items()
            if record.status in {"done", "failed"} and not record.subscribers
        ]
        for task_id in removable[:overflow]:
            self.records.pop(task_id, None)

    def start(
        self, assistant_id: str, operation: Callable[[], Awaitable[dict[str, Any]]],
        *, task_id: str | None = None,
    ) -> str:
        self._trim_records()
        task_id = task_id or uuid.uuid4().hex[:16]
        record = TaskRecord(task_id=task_id, assistant_id=assistant_id)
        self.records[task_id] = record

        async def runner() -> None:
            try:
                with self.bus.task_scope(task_id):
                    record.result = await operation()
                record.status = "done"
                terminal = {"type": "task_done", "data": {"result": record.result}, "task_id": task_id}
            except asyncio.CancelledError:
                record.status = "failed"
                record.error = "任务已取消"
                terminal = {
                    "type": "task_failed",
                    "data": {"reason": record.error},
                    "task_id": task_id,
                }
            except Exception as exc:
                record.status = "failed"
                record.error = str(exc)
                terminal = {"type": "task_failed", "data": {"reason": str(exc)}, "task_id": task_id}
            record.completed_at = datetime.now(timezone.utc)
            self._record_event(record, terminal)
            self._trim_records()

        record.handle = asyncio.create_task(runner())
        return task_id

    def get(self, task_id: str, assistant_id: str) -> TaskRecord:
        record = self.records.get(task_id)
        if record is None or record.assistant_id != assistant_id:
            raise KeyError(task_id)
        return record

    def is_active(self, task_id: str, assistant_id: str) -> bool:
        """工作记录对账用：任务存在且仍在运行（架构 §5.9 v1.19）。"""
        record = self.records.get(task_id)
        return record is not None and record.assistant_id == assistant_id and record.status == "running"

    async def stream(self, task_id: str, assistant_id: str, after_seq: int | None = None):
        record = self.get(task_id, assistant_id)
        queue: asyncio.Queue[Event] = asyncio.Queue()
        record.subscribers.add(queue)
        cursor = record.dropped  # 已裁剪的历史无法重放，从窗口最早事件开始
        try:
            if after_seq is not None:
                # 客户端声明已消费到 after_seq。超出已记录范围的未来游标回拨到
                # 至多重发末尾事件，保证终态仍可送达；重复由客户端按 seq 去重。
                after = max(-1, min(after_seq, record.next_seq - 2))
                if after + 1 < record.dropped:
                    gap: Event = {
                        "type": "reconnect_gap",
                        "data": {
                            "after_seq": after_seq,
                            "available_from": record.dropped,
                        },
                        "task_id": task_id,
                    }
                    yield f"data: {json.dumps(gap, ensure_ascii=False)}\n\n"
                    cursor = record.dropped
                else:
                    cursor = after + 1
            for event in list(record.events):
                if event["seq"] < cursor:
                    continue
                cursor = event["seq"] + 1
                yield f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event["type"] in TERMINAL_EVENT_TYPES:
                    return
            while True:
                if record.status in {"done", "failed"} and cursor >= record.next_seq:
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    # 命名事件既维持代理/浏览器连接，也让前端空闲看门狗可观察；
                    # 不带业务 data，不进入 TaskEvent/seq 去重链路。
                    yield "event: heartbeat\ndata: {}\n\n"
                    continue
                if event["seq"] < cursor:
                    continue
                cursor = event["seq"] + 1
                yield f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event["type"] in TERMINAL_EVENT_TYPES:
                    break
        finally:
            record.subscribers.discard(queue)
            self._trim_records()

    async def shutdown(self) -> None:
        handles = [record.handle for record in self.records.values() if record.handle and not record.handle.done()]
        for handle in handles:
            handle.cancel()
        if handles:
            await asyncio.gather(*handles, return_exceptions=True)
