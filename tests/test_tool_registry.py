"""统一工具表测试：内置工具沙箱、ToolContext 注入、MCP 工具同协议注册。"""
import asyncio
from pathlib import Path

import pytest

from agent.executor import ToolRegistry, execute_tool_calls
from agent.events import EventBus
from agent.schemas import ToolCall, ToolContext, ToolSpec
from agent.tools import make_builtin_tools
from memory.store import MemoryStore


def _ctx(data_dir: Path, assistant_id: str = "tester") -> ToolContext:
    return ToolContext(assistant_id=assistant_id, session_id="s1", data_dir=str(data_dir))


def test_sandbox_rejects_escape(tmp_path):
    store = MemoryStore(tmp_path)
    tools = {t.name: t for t in make_builtin_tools(tmp_path, store)}
    with pytest.raises(ValueError, match="沙箱"):
        asyncio.run(tools["save_markdown"].call({"path": "../evil.md", "content": "x"}, _ctx(tmp_path)))
    store.close()


def test_save_markdown_rejects_managed_project_paths(tmp_path):
    store = MemoryStore(tmp_path)
    tools = {tool.name: tool for tool in make_builtin_tools(tmp_path, store)}

    with pytest.raises(ValueError, match="受管项目"):
        asyncio.run(tools["save_markdown"].call({
            "path": "assistants/tester/projects/project-1/article.md",
            "content": "绕过版本控制",
        }, _ctx(tmp_path)))
    store.close()


def test_save_markdown_rejects_managed_assistant_profile(tmp_path):
    store = MemoryStore(tmp_path)
    tools = {tool.name: tool for tool in make_builtin_tools(tmp_path, store)}

    with pytest.raises(ValueError, match="受管助手"):
        asyncio.run(tools["save_markdown"].call({
            "path": "assistants/tester/memory/profile.md",
            "content": "覆盖长期画像",
        }, _ctx(tmp_path)))
    store.close()


def test_finalize_article_uses_tool_context(tmp_path):
    """finalize_article 不写助手 id 参数，靠 ToolContext 落到正确目录并登记索引。"""
    store = MemoryStore(tmp_path)
    tools = {t.name: t for t in make_builtin_tools(tmp_path, store)}
    result = asyncio.run(
        tools["finalize_article"].call({"title": "模型蒸馏", "content": "# 正文"}, _ctx(tmp_path, "tech-writer"))
    )
    written = list((tmp_path / "articles" / "tech-writer").glob("模型蒸馏-*.md"))
    assert len(written) == 1 and written[0].read_text(encoding="utf-8") == "# 正文"
    assert "模型蒸馏" in result
    assert "模型蒸馏" in store.recall("tech-writer", "模型蒸馏")
    store.close()


def test_registry_mixes_builtin_and_mcp_specs(tmp_path):
    """内置与 MCP 工具同一张表、同一调用协议。"""
    store = MemoryStore(tmp_path)
    registry = ToolRegistry()
    registry.register_all(make_builtin_tools(tmp_path, store))

    async def fake_mcp_handler(args, ctx):
        return f"echo:{args.get('q')}"

    registry.register(ToolSpec(name="tavily_search", description="fake", args_schema={}, handler=fake_mcp_handler, source="mcp:tavily"))
    assert "save_markdown" in registry.names() and "tavily_search" in registry.names()

    obs = asyncio.run(execute_tool_calls(
        [ToolCall(tool="tavily_search", args={"q": "模型蒸馏"})],
        registry, _ctx(tmp_path), EventBus(),
    ))
    assert obs[0].success and obs[0].summary == "echo:模型蒸馏"
    store.close()


def test_error_becomes_observation(tmp_path):
    """错误即观察：工具异常不打断执行，返回 success=False。"""
    store = MemoryStore(tmp_path)
    registry = ToolRegistry()
    registry.register_all(make_builtin_tools(tmp_path, store))
    obs = asyncio.run(execute_tool_calls(
        [ToolCall(tool="read_file", args={"path": "notes/missing.md"})],
        registry, _ctx(tmp_path), EventBus(),
    ))
    assert not obs[0].success and "不存在" in obs[0].error
    store.close()
