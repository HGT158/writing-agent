"""MCP Client（架构 §5.6）：官方 mcp SDK + stdio 传输。

启动时连接全部已配置 server 并 list_tools() 发现工具，包装成 ToolSpec
纳入统一工具表；任一 server 启动失败只记 warning，不阻断启动（架构 §9）。
"""
from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any, Callable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.schemas import ToolContext, ToolSpec

logger = logging.getLogger(__name__)


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
        assert self._stack is not None
        params = StdioServerParameters(command=cfg["command"], args=cfg["args"], env=cfg["env"])
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        listed = await session.list_tools()
        for tool in listed.tools:
            self.tools.append(self._wrap(name, session, tool))

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
            # 抓取类工具的结果全文入库 sources 表（显式标记，不靠调用点子串匹配）
            captures_source="fetch" in tool.name,
        )

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
