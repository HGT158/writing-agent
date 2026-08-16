"""TaskBroker resource lifecycle and per-assistant isolation."""
from __future__ import annotations

import asyncio
import json

from agent.events import EventBus
from api.tasks import TaskBroker


def _payload(line: str) -> dict:
    data = next(part for part in line.splitlines() if part.startswith("data: "))
    return json.loads(data.removeprefix("data: "))


def _frame_id(line: str) -> int | None:
    for part in line.splitlines():
        if part.startswith("id: "):
            return int(part.removeprefix("id: "))
    return None


def test_task_broker_broadcasts_to_each_subscriber():
    async def scenario():
        bus = EventBus()
        broker = TaskBroker(bus)
        release = asyncio.Event()

        async def operation():
            await release.wait()
            return {"ok": True}

        task_id = broker.start("writer-a", operation)
        first = broker.stream(task_id, "writer-a")
        second = broker.stream(task_id, "writer-a")
        first_next = asyncio.create_task(anext(first))
        second_next = asyncio.create_task(anext(second))
        await asyncio.sleep(0)
        with bus.task_scope(task_id):
            bus.emit("token", text="同时到达")
        first_event, second_event = await asyncio.gather(first_next, second_next)
        release.set()
        await broker.records[task_id].handle
        await first.aclose()
        await second.aclose()
        return _payload(first_event), _payload(second_event)

    first, second = asyncio.run(scenario())
    assert first["data"]["text"] == "同时到达"
    assert second == first


def test_task_broker_marks_cancelled_tasks_terminal():
    async def scenario():
        broker = TaskBroker(EventBus())

        async def operation():
            await asyncio.Event().wait()
            return {}

        task_id = broker.start("writer-a", operation)
        await asyncio.sleep(0)
        await broker.shutdown()
        return broker.get(task_id, "writer-a")

    record = asyncio.run(scenario())
    assert record.status == "failed"
    assert record.events[-1]["type"] == "task_failed"


def test_task_broker_keeps_terminal_records_bounded():
    async def scenario():
        broker = TaskBroker(EventBus(), max_records=3)
        for index in range(6):
            task_id = broker.start("writer-a", lambda index=index: asyncio.sleep(0, result={"i": index}))
            await broker.records[task_id].handle
        return broker

    broker = asyncio.run(scenario())
    assert len(broker.records) <= 3


def test_task_broker_streams_beyond_event_window():
    """事件窗口被裁剪后活跃订阅者必须继续收流并拿到终态（架构 §5.9）。"""

    async def scenario():
        bus = EventBus()
        broker = TaskBroker(bus, max_events=4)
        release = asyncio.Event()

        async def operation():
            await release.wait()
            return {"ok": True}

        task_id = broker.start("writer-a", operation)
        stream = broker.stream(task_id, "writer-a")
        received: list[dict] = []
        with bus.task_scope(task_id):
            for index in range(12):
                bus.emit("token", text=str(index))
                received.append(_payload(await anext(stream)))
        release.set()
        await broker.records[task_id].handle
        while True:
            event = _payload(await anext(stream))
            received.append(event)
            if event["type"] in {"task_done", "task_failed"}:
                break
        await stream.aclose()
        return received

    events = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    tokens = [item["data"]["text"] for item in events if item["type"] == "token"]
    assert tokens == [str(index) for index in range(12)]
    assert events[-1]["type"] == "task_done"
    assert [item["seq"] for item in events] == list(range(len(events)))


def test_task_broker_reconnect_skips_dropped_events():
    """重连订阅者跳过已裁剪的历史，但不得重复或错位。"""

    async def scenario():
        bus = EventBus()
        broker = TaskBroker(bus, max_events=3)
        release = asyncio.Event()

        async def operation():
            await release.wait()
            return {"ok": True}

        task_id = broker.start("writer-a", operation)
        with bus.task_scope(task_id):
            for index in range(9):
                bus.emit("token", text=str(index))
        release.set()
        await broker.records[task_id].handle
        stream = broker.stream(task_id, "writer-a")
        replay = []
        async for line in stream:
            replay.append(_payload(line))
        return replay

    replay = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    assert [item["seq"] for item in replay] == sorted(item["seq"] for item in replay)
    assert replay[-1]["type"] == "task_done"


def test_task_broker_frames_events_with_sse_id():
    """每个数据帧携带标准 SSE id 行，重连客户端据此报告 Last-Event-ID（架构 §5.9）。"""

    async def scenario():
        bus = EventBus()
        broker = TaskBroker(bus)
        task_id = broker.start("writer-a", lambda: asyncio.sleep(0, result={"ok": True}))
        with bus.task_scope(task_id):
            bus.emit("token", text="帧")
        await broker.records[task_id].handle
        stream = broker.stream(task_id, "writer-a")
        frames = [line async for line in stream]
        await stream.aclose()
        return frames

    frames = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    assert frames[0].splitlines()[0] == "id: 0"
    assert _frame_id(frames[-1]) == 1
    assert _payload(frames[-1])["type"] == "task_done"


def test_task_broker_resumes_stream_from_cursor():
    """after_seq 表示客户端已消费到该序号：重连只补发其后事件，不重复不遗漏。"""

    async def scenario():
        bus = EventBus()
        broker = TaskBroker(bus, max_events=8)
        release = asyncio.Event()

        async def operation():
            await release.wait()
            return {"ok": True}

        task_id = broker.start("writer-a", operation)
        first = broker.stream(task_id, "writer-a")
        with bus.task_scope(task_id):
            for index in range(5):
                bus.emit("token", text=str(index))
            # 活动连接只消费到 seq=2 后断开
            for _ in range(3):
                await anext(first)
            await first.aclose()
            release.set()
        await broker.records[task_id].handle
        resumed = broker.stream(task_id, "writer-a", after_seq=2)
        frames = [line async for line in resumed]
        await resumed.aclose()
        return frames

    frames = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    events = [_payload(frame) for frame in frames]
    assert [event["seq"] for event in events] == [3, 4, 5]
    assert [_frame_id(frame) for frame in frames] == [3, 4, 5]
    assert [event["data"]["text"] for event in events[:2]] == ["3", "4"]
    assert events[-1]["type"] == "task_done"


def test_task_broker_signals_gap_when_cursor_behind_window():
    """游标落后于重放窗口时先发可识别的缺口事件，不得静默拼接残缺回复。"""

    async def scenario():
        bus = EventBus()
        broker = TaskBroker(bus, max_events=3)

        async def operation():
            return {"ok": True}

        task_id = broker.start("writer-a", operation)
        with bus.task_scope(task_id):
            for index in range(9):
                bus.emit("token", text=str(index))
        await broker.records[task_id].handle
        resumed = broker.stream(task_id, "writer-a", after_seq=2)
        frames = [line async for line in resumed]
        await resumed.aclose()
        return frames

    frames = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    gap = _payload(frames[0])
    assert gap["type"] == "reconnect_gap"
    assert gap["data"]["after_seq"] == 2
    assert gap["data"]["available_from"] == 7
    assert _frame_id(frames[0]) is None  # 控制事件不属于事件序号流
    events = [_payload(frame) for frame in frames[1:]]
    assert [event["seq"] for event in events] == [7, 8, 9]
    assert events[-1]["type"] == "task_done"


def test_task_broker_delivers_terminal_for_future_cursor():
    """游标超出已记录序号时回拨重发末尾事件，终态必须送达而不是空流。"""

    async def scenario():
        bus = EventBus()
        broker = TaskBroker(bus)

        async def operation():
            return {"ok": True}

        task_id = broker.start("writer-a", operation)
        with bus.task_scope(task_id):
            bus.emit("token", text="a")
            bus.emit("token", text="b")
        await broker.records[task_id].handle
        resumed = broker.stream(task_id, "writer-a", after_seq=10_000)
        frames = [line async for line in resumed]
        await resumed.aclose()
        return frames

    frames = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    events = [_payload(frame) for frame in frames]
    assert [event["seq"] for event in events] == [2]
    assert events[0]["type"] == "task_done"
