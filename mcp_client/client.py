"""MCP Client（架构 §5.6）：官方 mcp SDK + stdio 传输。

启动时连接全部已配置 server 并 list_tools() 发现工具，包装成 ToolSpec
纳入统一工具表；任一 server 启动失败只记 warning，不阻断启动（架构 §9）。
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any, Callable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.schemas import ToolContext, ToolSpec

logger = logging.getLogger(__name__)
MCP_STARTUP_STEP_TIMEOUT_SECONDS = 10.0


class MCPManager:
    def __init__(self, configs: dict[str, dict[str, Any]]) -> None:
        self._configs = configs
        self._stack: AsyncExitStack | None = None
        self.tools: list[ToolSpec] = []
        self.failed_servers: list[str] = []

    async def start(self, warn: Callable[[str], None] | None = None) -> None:
        warn = warn or (lambda msg: logger.warning(msg))
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        for name, cfg in self._configs.items():
            try:
                await self._connect(name, cfg)
            except Exception as exc:
                self.failed_servers.append(name)
                warn(f"MCP server {name} 启动失败（其工具不注册，不影响启动）：{exc}")

    async def _connect(self, name: str, cfg: dict[str, Any]) -> None:
        if self._stack is None:
            raise RuntimeError("MCP manager 尚未启动")
        params = StdioServerParameters(command=cfg["command"], args=cfg["args"], env=cfg["env"])
        server_stack = AsyncExitStack()
        await server_stack.__aenter__()
        try:
            async def open_session() -> ClientSession:
                read, write = await server_stack.enter_async_context(stdio_client(params))
                return await server_stack.enter_async_context(ClientSession(read, write))

            session = await asyncio.wait_for(
                open_session(), timeout=MCP_STARTUP_STEP_TIMEOUT_SECONDS
            )
            await asyncio.wait_for(
                session.initialize(), timeout=MCP_STARTUP_STEP_TIMEOUT_SECONDS
            )
            listed = await asyncio.wait_for(
                session.list_tools(), timeout=MCP_STARTUP_STEP_TIMEOUT_SECONDS
            )
            wrapped_tools = [self._wrap(name, session, tool) for tool in listed.tools]

            # 只有完整启动成功的 server 才把清理责任晋升给 manager 长命栈；
            # 失败时 server_stack 仍持有全部回调，会在 finally 即时关闭子进程。
            promoted = server_stack.pop_all()
            self._stack.push_async_callback(promoted.aclose)
            self.tools.extend(wrapped_tools)
        finally:
            await server_stack.aclose()

    def _wrap(self, server_name: str, session: ClientSession, tool: Any) -> ToolSpec:
        async def handler(args: dict[str, Any], ctx: ToolContext) -> str:
            # MCP 工具不需要 ctx（ToolContext 为内置工具归属信息），适配层直接丢弃
            result = await session.call_tool(tool.name, args)
            parts: list[str] = []
            for content in result.content:
                text = getattr(content, "text", None)
                parts.append(text if text is not None else str(content))
            return "\n".join(parts)

        return ToolSpec(
            name=tool.name,
            description=tool.description or "",
            args_schema=tool.inputSchema or {"type": "object", "properties": {}},
            handler=handler,
            source=f"mcp:{server_name}",
            idempotent=False,
            captures_source=False,
        )

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
