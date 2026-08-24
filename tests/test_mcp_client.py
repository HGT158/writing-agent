"""MCP 启动超时与 server 级资源生命周期。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from mcp_client import client as client_module


class _Transport:
    def __init__(self, events: list[str], *, hang: bool = False) -> None:
        self.events = events
        self.hang = hang

    async def __aenter__(self):
        self.events.append("transport-enter")
        if self.hang:
            try:
                await asyncio.Event().wait()
            finally:
                self.events.append("transport-cancel-cleanup")
        return object(), object()

    async def __aexit__(self, *_args):
        self.events.append("transport-exit")


class _Session:
    def __init__(self, events: list[str], *, hang_at: str | None = None) -> None:
        self.events = events
        self.hang_at = hang_at

    async def __aenter__(self):
        self.events.append("session-enter")
        return self

    async def __aexit__(self, *_args):
        self.events.append("session-exit")

    async def initialize(self):
        self.events.append("initialize")
        if self.hang_at == "initialize":
            await asyncio.Event().wait()

    async def list_tools(self):
        self.events.append("list-tools")
        if self.hang_at == "list_tools":
            await asyncio.Event().wait()
        tool = SimpleNamespace(
            name="remote_tool",
            description="remote",
            inputSchema={"type": "object", "properties": {}},
        )
        return SimpleNamespace(tools=[tool])


def _config():
    return {"server": {"command": "fake", "args": [], "env": {}}}


@pytest.mark.parametrize("hang_at", ["connect", "initialize", "list_tools"])
def test_mcp_startup_timeout_closes_failed_server_stack(monkeypatch, hang_at):
    events: list[str] = []
    session = _Session(events, hang_at=hang_at)
    monkeypatch.setattr(
        client_module,
        "stdio_client",
        lambda _params: _Transport(events, hang=hang_at == "connect"),
    )
    monkeypatch.setattr(client_module, "ClientSession", lambda _read, _write: session)
    monkeypatch.setattr(client_module, "MCP_STARTUP_STEP_TIMEOUT_SECONDS", 0.01)
    manager = client_module.MCPManager(_config())
    warnings: list[str] = []

    asyncio.run(manager.start(warnings.append))

    assert manager.failed_servers == ["server"]
    assert manager.tools == []
    assert warnings and "不影响启动" in warnings[0]
    if hang_at == "connect":
        assert events[-1] == "transport-cancel-cleanup"
    else:
        assert events[-2:] == ["session-exit", "transport-exit"]
    asyncio.run(manager.close())


def test_successful_mcp_session_is_promoted_until_manager_close(monkeypatch):
    events: list[str] = []
    session = _Session(events)
    monkeypatch.setattr(client_module, "stdio_client", lambda _params: _Transport(events))
    monkeypatch.setattr(client_module, "ClientSession", lambda _read, _write: session)
    manager = client_module.MCPManager(_config())

    async def run():
        await manager.start()
        assert [tool.name for tool in manager.tools] == ["remote_tool"]
        assert "session-exit" not in events
        await manager.close()

    asyncio.run(run())

    assert events[-2:] == ["session-exit", "transport-exit"]
