"""Stage 3 APScheduler integration tests."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import inspect
from types import SimpleNamespace

from agent.events import EventBus
from memory.store import AssistantBusyError, MemoryStore


class _Assistants:
    def __init__(self, available: set[str]) -> None:
        self.available = available

    def get(self, assistant_id: str):
        if assistant_id not in self.available:
            raise KeyError(assistant_id)
        return SimpleNamespace(id=assistant_id)


class _FakeRuntime:
    def __init__(self, *, busy: bool = False) -> None:
        self.bus = EventBus()
        self.assistants = _Assistants({"tech-writer", "marketing"})
        self.busy = busy
        self.calls: list[tuple[str, str]] = []
        self.run_loop = None

    async def run(self, assistant_id: str, task: str):
        self.calls.append((assistant_id, task))
        self.run_loop = asyncio.get_running_loop()
        if self.busy:
            raise AssistantBusyError(assistant_id, "manual-task 运行中")
        return {"status": "done"}


class _BlockingRuntime(_FakeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cleaned_up = asyncio.Event()

    async def run(self, assistant_id: str, task: str):
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cleaned_up.set()


class _LockingRuntime(_BlockingRuntime):
    def __init__(self, data_dir) -> None:
        super().__init__()
        self.store = MemoryStore(data_dir)

    async def run(self, assistant_id: str, task: str):
        self.store.acquire_lock(assistant_id, "scheduled-task")
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.store.release_lock(assistant_id, "scheduled-task")
            self.cleaned_up.set()


def test_registers_valid_jobs_and_skips_invalid_entries():
    from scheduler.scheduler import RuntimeScheduler

    async def scenario():
        runtime = _FakeRuntime()
        events = []
        runtime.bus.subscribe(events.append)
        manager = RuntimeScheduler(
            runtime,
            [
                {
                    "id": "daily-ai-news",
                    "assistant_id": "tech-writer",
                    "cron": "0 8 * * *",
                    "task": "生成 AI 日报",
                },
                {
                    "id": "missing-assistant",
                    "assistant_id": "unknown",
                    "cron": "0 9 * * *",
                    "task": "不应注册",
                },
                {
                    "id": "bad-cron",
                    "assistant_id": "marketing",
                    "cron": "not-a-cron",
                    "task": "不应注册",
                },
            ],
        )
        manager.start()
        try:
            assert [job.id for job in manager.get_jobs()] == ["daily-ai-news"]
            assert manager.get_job("daily-ai-news").args[1:] == (
                "daily-ai-news",
                "tech-writer",
                "生成 AI 日报",
            )
            warnings = [event for event in events if event["type"] == "warning"]
            assert len(warnings) == 2
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_scheduled_job_runs_on_the_runtime_event_loop():
    from scheduler.scheduler import RuntimeScheduler

    async def scenario():
        runtime = _FakeRuntime()
        manager = RuntimeScheduler(
            runtime,
            [{
                "id": "daily-ai-news",
                "assistant_id": "tech-writer",
                "cron": "0 8 * * *",
                "task": "生成 AI 日报",
            }],
        )
        manager.start()
        try:
            job = manager.get_job("daily-ai-news")
            await job.func(*job.args, **job.kwargs)
            assert manager.loop is asyncio.get_running_loop()
            assert runtime.run_loop is manager.loop
            assert runtime.calls == [("tech-writer", "生成 AI 日报")]
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_scheduler_dispatches_a_due_job():
    from scheduler.scheduler import RuntimeScheduler

    async def scenario():
        runtime = _FakeRuntime()
        manager = RuntimeScheduler(
            runtime,
            [{
                "id": "daily-ai-news",
                "assistant_id": "tech-writer",
                "cron": "0 8 * * *",
                "task": "生成 AI 日报",
            }],
        )
        manager.start()
        try:
            job = manager.get_job("daily-ai-news")
            job.modify(next_run_time=datetime.now().astimezone() + timedelta(milliseconds=50))
            for _ in range(20):
                if runtime.calls:
                    break
                await asyncio.sleep(0.05)
            assert runtime.calls == [("tech-writer", "生成 AI 日报")]
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_busy_assistant_skips_job_and_emits_warning():
    from scheduler.jobs import run_scheduled_job

    async def scenario():
        runtime = _FakeRuntime(busy=True)
        events = []
        runtime.bus.subscribe(events.append)

        result = await run_scheduled_job(
            runtime,
            "daily-ai-news",
            "tech-writer",
            "生成 AI 日报",
        )

        assert result is None
        warning = next(event for event in events if event["type"] == "warning")
        assert "daily-ai-news" in warning["data"]["text"]
        assert "tech-writer" in warning["data"]["text"]
        assert "跳过" in warning["data"]["text"]

    asyncio.run(scenario())


def test_shutdown_waits_for_running_job_cleanup():
    from scheduler.scheduler import RuntimeScheduler

    async def scenario():
        runtime = _BlockingRuntime()
        manager = RuntimeScheduler(
            runtime,
            [{
                "id": "daily-ai-news",
                "assistant_id": "tech-writer",
                "cron": "0 8 * * *",
                "task": "生成 AI 日报",
            }],
        )
        manager.start()
        job = manager.get_job("daily-ai-news")
        job.modify(next_run_time=datetime.now().astimezone() + timedelta(milliseconds=10))
        await asyncio.wait_for(runtime.started.wait(), timeout=1)

        shutdown_result = manager.shutdown()
        assert inspect.isawaitable(shutdown_result)
        await shutdown_result
        assert runtime.cleaned_up.is_set()

    asyncio.run(scenario())


def test_shutdown_releases_active_job_run_lock_before_store_close(tmp_path):
    from scheduler.scheduler import RuntimeScheduler

    async def scenario():
        runtime = _LockingRuntime(tmp_path)
        manager = RuntimeScheduler(
            runtime,
            [{
                "id": "daily-ai-news",
                "assistant_id": "tech-writer",
                "cron": "0 8 * * *",
                "task": "生成 AI 日报",
            }],
        )
        manager.start()
        job = manager.get_job("daily-ai-news")
        job.modify(next_run_time=datetime.now().astimezone() + timedelta(milliseconds=10))
        await asyncio.wait_for(runtime.started.wait(), timeout=1)
        assert runtime.store.is_locked("tech-writer")

        await manager.shutdown()

        assert not runtime.store.is_locked("tech-writer")
        runtime.store.close()

    asyncio.run(scenario())
