"""Scheduled job entry points."""
from __future__ import annotations

import logging
from typing import Any

from memory.store import AssistantBusyError

logger = logging.getLogger(__name__)


async def run_scheduled_job(
    runtime: Any,
    job_id: str,
    assistant_id: str,
    task: str,
) -> Any | None:
    """Run one complete Agent task; a busy assistant skips this occurrence."""
    try:
        return await runtime.run(assistant_id, task)
    except AssistantBusyError as exc:
        runtime.bus.emit(
            "warning",
            text=f"定时任务 {job_id} 跳过：助手 {assistant_id} 正忙（{exc}）",
        )
        return None
    except Exception as exc:
        logger.exception("定时任务 %s 执行失败", job_id)
        runtime.bus.emit(
            "warning",
            text=f"定时任务 {job_id} 执行失败（助手 {assistant_id}）：{exc}",
        )
        return None
