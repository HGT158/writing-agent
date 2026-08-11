"""APScheduler lifecycle bound to the Agent Runtime event loop."""
from __future__ import annotations

import asyncio
from typing import Any

from apscheduler.job import Job
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .jobs import run_scheduled_job


class RuntimeScheduler:
    def __init__(self, runtime: Any, jobs: list[dict[str, Any]]) -> None:
        self.runtime = runtime
        self.jobs = jobs
        self._scheduler: AsyncIOScheduler | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running_tasks: set[asyncio.Task[Any]] = set()

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    def start(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            return
        self._loop = asyncio.get_running_loop()
        self._scheduler = AsyncIOScheduler(event_loop=self._loop)
        for config in self.jobs:
            self._register(config)
        self._scheduler.start()

    def _register(self, config: dict[str, Any]) -> None:
        if self._scheduler is None:
            raise RuntimeError("Scheduler 尚未初始化")
        try:
            job_id = self._required_text(config, "id")
            assistant_id = self._required_text(config, "assistant_id")
            cron = self._required_text(config, "cron")
            task = self._required_text(config, "task")
            self.runtime.assistants.get(assistant_id)
            trigger = CronTrigger.from_crontab(cron, timezone=self._scheduler.timezone)
        except (KeyError, TypeError, ValueError) as exc:
            label = config.get("id", "<unknown>") if isinstance(config, dict) else "<invalid>"
            self.runtime.bus.emit("warning", text=f"定时任务 {label} 未注册：{exc}")
            return

        self._scheduler.add_job(
            self._run_job,
            trigger=trigger,
            args=(self.runtime, job_id, assistant_id, task),
            id=job_id,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
        )

    async def _run_job(self, runtime: Any, job_id: str, assistant_id: str, task: str) -> Any | None:
        current = asyncio.current_task()
        if current is not None:
            self._running_tasks.add(current)
        try:
            return await run_scheduled_job(runtime, job_id, assistant_id, task)
        finally:
            if current is not None:
                self._running_tasks.discard(current)

    @staticmethod
    def _required_text(config: dict[str, Any], key: str) -> str:
        value = config[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} 必须是非空字符串")
        return value.strip()

    def get_job(self, job_id: str) -> Job | None:
        return None if self._scheduler is None else self._scheduler.get_job(job_id)

    def get_jobs(self) -> list[Job]:
        return [] if self._scheduler is None else self._scheduler.get_jobs()

    async def shutdown(self) -> None:
        running = tuple(self._running_tasks)
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        current = asyncio.current_task()
        pending = [task for task in running if task is not current]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
