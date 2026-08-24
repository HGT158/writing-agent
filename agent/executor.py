"""Executor（架构 §5.2）：并行执行工具调用，错误即观察。

- 每次调用注入 ToolContext（assistant_id/session_id/data_dir），LLM 不可见
- 单工具超时 30s，失败重试 1 次
- fetch 类工具的原始全文落 SQLite sources 表，只有摘要进上下文（§3.3）
"""
from __future__ import annotations

import asyncio
from typing import Any

from memory.store import MemoryStore

from .events import EventBus
from .schemas import Observation, ToolCall, ToolContext, ToolSpec

_SUMMARY_LIMIT = 500


def _summarize(result: str) -> str:
    text = " ".join(result.split())
    return text if len(text) <= _SUMMARY_LIMIT else text[: _SUMMARY_LIMIT - 1] + "…"


class ToolRegistry:
    """统一工具表：内置工具与 MCP 工具同一协议（架构 §1）。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> bool:
        if spec.name in self._tools:
            return False
        self._tools[spec.name] = spec
        return True

    def register_all(self, specs: list[ToolSpec]) -> int:
        registered = 0
        for spec in specs:
            registered += self.register(spec)
        return registered

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> set[str]:
        return set(self._tools)

    def list(self) -> list[ToolSpec]:
        return list(self._tools.values())


async def execute_tool_calls(
    tool_calls: list[ToolCall],
    registry: ToolRegistry,
    ctx: ToolContext,
    bus: EventBus,
    store: MemoryStore | None = None,
    *,
    timeout_seconds: float = 30.0,
) -> list[Observation]:
    async def run_one(tc: ToolCall) -> Observation:
        bus.emit("tool_call", tool=tc.tool, args=tc.args)
        spec = registry.get(tc.tool)
        if spec is None:
            obs = Observation(tool=tc.tool, success=False, error=f"工具不存在：{tc.tool}")
            bus.emit("tool_result", tool=tc.tool, ok=False, error=obs.error)
            return obs

        last_error: Exception | None = None
        attempts = 2 if spec.idempotent else 1  # 非幂等写工具失败不重试（防重复写文件/重复登记）
        for _attempt in range(attempts):
            try:
                result = await asyncio.wait_for(
                    spec.call(tc.args, ctx), timeout=timeout_seconds
                )
                if store is not None and spec.captures_source and tc.args.get("url"):
                    store.save_source(ctx.assistant_id, ctx.session_id, tc.args["url"], tc.args["url"], result)
                obs = Observation(tool=tc.tool, success=True, summary=_summarize(result))
                bus.emit("tool_result", tool=tc.tool, ok=True, summary=obs.summary)
                return obs
            except Exception as exc:
                last_error = exc

        obs = Observation(tool=tc.tool, success=False, error=str(last_error)[:300])
        bus.emit("tool_result", tool=tc.tool, ok=False, error=obs.error)
        return obs

    return list(await asyncio.gather(*[run_one(tc) for tc in tool_calls]))
