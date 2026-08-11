"""TaskBroker resource lifecycle and per-assistant isolation."""
from __future__ import annotations

import asyncio
import json

from agent.events import EventBus
from api.tasks import TaskBroker


def _payload(line: str) -> dict:
    return json.loads(line.removeprefix("data: "))


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
