"""统一工具表测试：内置工具沙箱、ToolContext 注入、MCP 工具同协议注册。"""
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

import agent.runtime as runtime_module
from agent.executor import ToolRegistry, execute_tool_calls
from agent.events import EventBus
from agent.runtime import AgentRuntime
from agent.schemas import ToolCall, ToolContext, ToolSpec
from agent.tools import make_builtin_tools, make_project_edit_tool
from config.settings import Settings
from memory.errors import ResourceConflictError
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


def _hunk(old: str, new: str) -> dict:
    return {"old_text": old, "new_text": new}


def _documents(document, hunks: list[dict]) -> list[dict]:
    return [{
        "document_id": document.document_id,
        "document_version": document.version,
        "hunks": hunks,
    }]


def test_project_edit_tool_creates_pending_change_without_writing_document(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("tester", "编辑提案")
    document, _staled = store.save_document(
        "tester",
        project.project_id,
        project.entry_document_id,
        "第一段原文。第二段原文。",
        expected_version=1,
    )
    spec = make_project_edit_tool(store, project.project_id)

    result = json.loads(asyncio.run(spec.call(
        {"documents": _documents(document, [_hunk("第一段原文。", "首段精简。")])},
        _ctx(tmp_path),
    )))

    assert spec.name == "propose_project_edits"
    assert spec.idempotent is False
    assert result["count"] == 1
    assert len(result["change_set_ids"]) == 1
    change = store.get_change_set(
        "tester", project.project_id, result["change_set_ids"][0]
    )
    assert change.status == "pending"
    assert [(h.start, h.end) for h in change.hunks] == [(0, len("第一段原文。"))]
    assert change.hunks[0].original_text == "第一段原文。"
    assert change.hunks[0].new_text == "首段精简。"
    current = store.get_document("tester", project.project_id, document.document_id)
    assert current.content == "第一段原文。第二段原文。"
    store.close()


def test_project_edit_tool_allows_multiple_hunks_for_same_document(tmp_path):
    """v1.20 修复：同一次调用对同一文档提交多处修改不再整批失败。"""
    store = MemoryStore(tmp_path)
    project = store.create_project("tester", "多处修改")
    document, _staled = store.save_document(
        "tester",
        project.project_id,
        project.entry_document_id,
        "第一段。中间段。最后段。",
        expected_version=1,
    )
    spec = make_project_edit_tool(store, project.project_id)

    result = json.loads(asyncio.run(spec.call(
        {"documents": _documents(document, [
            _hunk("第一段。", "【一】。"),
            _hunk("中间段。", "【中】。"),
            _hunk("最后段。", "【末】。"),
        ])},
        _ctx(tmp_path),
    )))

    assert result["count"] == 1
    change = store.get_change_set(
        "tester", project.project_id, result["change_set_ids"][0]
    )
    assert len(change.hunks) == 3
    assert [h.display_order for h in change.hunks] == [0, 1, 2]
    starts = [h.start for h in change.hunks]
    assert starts == sorted(starts)
    applied = store.accept_all_change_hunks(
        "tester", project.project_id, change.change_set_id
    )
    assert applied["stopped"] is None
    assert applied["document"].content == "【一】。【中】。【末】。"
    store.close()


def test_project_edit_tool_rejects_resubmission_for_same_task(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("tester", "同任务重复")
    document, _staled = store.save_document(
        "tester", project.project_id, project.entry_document_id,
        "第一句。第二句。", expected_version=1,
    )
    spec = make_project_edit_tool(store, project.project_id)
    bus = EventBus()

    with bus.task_scope("broker-task-77"):
        asyncio.run(spec.call(
            {"documents": _documents(document, [_hunk("第一句。", "改。")])},
            _ctx(tmp_path),
        ))
        with pytest.raises(ResourceConflictError, match="该任务已提交"):
            asyncio.run(spec.call(
                {"documents": _documents(document, [_hunk("第二句。", "改。")])},
                _ctx(tmp_path),
            ))

    current = store.get_document("tester", project.project_id, document.document_id)
    assert current.content == "第一句。第二句。"
    with sqlite3.connect(tmp_path / "app.db") as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM change_sets WHERE assistant_id = ? AND project_id = ?",
            ("tester", project.project_id),
        ).fetchone()[0]
    assert pending == 1
    store.close()


def test_project_edit_tool_inserts_first_draft_into_empty_document(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("tester", "空白首稿")
    document = store.get_document(
        "tester", project.project_id, project.entry_document_id
    )
    spec = make_project_edit_tool(store, project.project_id)

    result = json.loads(asyncio.run(spec.call(
        {"documents": _documents(document, [
            _hunk("", "# 小锅鱼的深圳奇遇\n\n故事正文。"),
        ])},
        _ctx(tmp_path),
    )))

    change = store.get_change_set(
        "tester", project.project_id, result["change_set_ids"][0]
    )
    assert (change.hunks[0].start, change.hunks[0].end, change.hunks[0].original_text) == (0, 0, "")
    assert store.get_document(
        "tester", project.project_id, document.document_id
    ).content == ""

    applied = store.accept_all_change_hunks(
        "tester", project.project_id, change.change_set_id
    )
    assert applied["document"].content == "# 小锅鱼的深圳奇遇\n\n故事正文。"
    assert applied["change_set"].status == "applied"
    store.close()


def test_project_edit_tool_rejects_empty_old_text_for_nonempty_document(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("tester", "非空文档")
    document, _staled = store.save_document(
        "tester",
        project.project_id,
        project.entry_document_id,
        "已有正文。",
        expected_version=1,
    )
    spec = make_project_edit_tool(store, project.project_id)

    with pytest.raises(ResourceConflictError, match="非空文档不能使用空旧文本"):
        asyncio.run(spec.call(
            {"documents": _documents(document, [_hunk("", "新增正文。")])},
            _ctx(tmp_path),
        ))

    assert store.get_document(
        "tester", project.project_id, document.document_id
    ).content == "已有正文。"
    store.close()


def test_project_edit_tool_reports_readable_schema_error(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("tester", "参数错误")
    document = store.get_document(
        "tester", project.project_id, project.entry_document_id
    )
    spec = make_project_edit_tool(store, project.project_id)

    with pytest.raises(ValueError, match="修改建议参数无效"):
        asyncio.run(spec.call(
            {"documents": [{
                "document_id": document.document_id,
                "document_version": document.version,
                "hunks": [{"new_text": "新正文。"}],
            }]},
            _ctx(tmp_path),
        ))

    store.close()


def test_project_edit_tool_rejects_missing_old_text_without_writing_document(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("tester", "缺失旧文本")
    document, _staled = store.save_document(
        "tester",
        project.project_id,
        project.entry_document_id,
        "当前正文。",
        expected_version=1,
    )
    spec = make_project_edit_tool(store, project.project_id)

    with pytest.raises(ResourceConflictError, match="旧文本不存在"):
        asyncio.run(spec.call(
            {"documents": _documents(document, [_hunk("不存在的句子。", "替换句子。")])},
            _ctx(tmp_path),
        ))

    current = store.get_document("tester", project.project_id, document.document_id)
    assert current.content == "当前正文。"
    store.close()


def test_project_edit_tool_rejects_ambiguous_old_text_without_writing_document(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("tester", "重复旧文本")
    document, _staled = store.save_document(
        "tester",
        project.project_id,
        project.entry_document_id,
        "重复句。中间。重复句。",
        expected_version=1,
    )
    spec = make_project_edit_tool(store, project.project_id)

    with pytest.raises(ResourceConflictError, match="旧文本匹配多处"):
        asyncio.run(spec.call(
            {"documents": _documents(document, [_hunk("重复句。", "精简句。")])},
            _ctx(tmp_path),
        ))

    current = store.get_document("tester", project.project_id, document.document_id)
    assert current.content == "重复句。中间。重复句。"
    store.close()


def test_project_edit_tool_rejects_duplicate_document_entries_atomically(tmp_path):
    """documents 列表内同一文档出现两次：整批拒绝，不创建半成品。"""
    store = MemoryStore(tmp_path)
    project = store.create_project("tester", "重复文档")
    document, _staled = store.save_document(
        "tester",
        project.project_id,
        project.entry_document_id,
        "第一句。第二句。",
        expected_version=1,
    )
    spec = make_project_edit_tool(store, project.project_id)

    with pytest.raises(ValueError, match="文档重复"):
        asyncio.run(spec.call(
            {"documents": [
                *_documents(document, [_hunk("第一句。", "首句。")]),
                *_documents(document, [_hunk("第二句。", "次句。")]),
            ]},
            _ctx(tmp_path),
        ))

    current = store.get_document("tester", project.project_id, document.document_id)
    assert current.content == "第一句。第二句。"
    with sqlite3.connect(tmp_path / "app.db") as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM change_sets WHERE assistant_id = ? AND project_id = ?",
            ("tester", project.project_id),
        ).fetchone()[0]
    assert pending == 0
    store.close()


def test_project_edit_tool_checks_version_before_matching_old_text(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("tester", "版本优先")
    document, _staled = store.save_document(
        "tester",
        project.project_id,
        project.entry_document_id,
        "当前正文。",
        expected_version=1,
    )
    spec = make_project_edit_tool(store, project.project_id)

    with pytest.raises(ResourceConflictError, match="版本冲突"):
        asyncio.run(spec.call(
            {"documents": [{
                "document_id": document.document_id,
                "document_version": document.version - 1,
                "hunks": [_hunk("旧版本中已经删除的文字。", "替换文字。")],
            }]},
            _ctx(tmp_path),
        ))

    current = store.get_document("tester", project.project_id, document.document_id)
    assert current.content == "当前正文。"
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


def test_registry_rejects_duplicate_name_and_counts_only_new_tools():
    async def original_handler(args, ctx):
        return "builtin"

    async def replacement_handler(args, ctx):
        return "mcp"

    original = ToolSpec(
        name="read_file", description="builtin", args_schema={},
        handler=original_handler, source="builtin",
    )
    replacement = ToolSpec(
        name="read_file", description="mcp", args_schema={},
        handler=replacement_handler, source="mcp:filesystem",
    )
    unique = ToolSpec(
        name="search", description="mcp", args_schema={},
        handler=replacement_handler, source="mcp:search",
    )
    registry = ToolRegistry()

    assert registry.register(original) is True
    assert registry.register_all([replacement, unique]) == 1
    assert registry.get("read_file") is original
    assert registry.names() == {"read_file", "search"}


def test_runtime_skips_conflicting_mcp_tool_and_reports_actual_count(tmp_path, monkeypatch):
    async def mcp_handler(args, ctx):
        return "mcp"

    mcp_tools = [
        ToolSpec(
            name="read_file", description="collision", args_schema={},
            handler=mcp_handler, source="mcp:filesystem",
        ),
        ToolSpec(
            name="search", description="unique", args_schema={},
            handler=mcp_handler, source="mcp:search",
        ),
    ]

    class FakeMCPManager:
        def __init__(self, configs):
            self.tools = mcp_tools
            self.failed_servers = []

        async def start(self, warn=None):
            return None

        async def close(self):
            return None

    empty = tmp_path / "empty.json"
    empty.write_text('{"mcpServers": {}}', encoding="utf-8")
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path,
        skills_dir=Path(__file__).resolve().parents[1] / "skills",
        mcp_config=empty,
        openai_api_key="fake",
        openai_base_url="",
        model_name="fake",
    )
    monkeypatch.setattr(runtime_module, "MCPManager", FakeMCPManager)
    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    runtime = AgentRuntime(settings, bus)

    asyncio.run(runtime.start())

    assert runtime.tools.get("read_file").source == "builtin"
    assert runtime.tools.get("search").source == "mcp:search"
    warnings = [event["data"]["text"] for event in events if event["type"] == "warning"]
    infos = [event["data"]["text"] for event in events if event["type"] == "info"]
    assert any("read_file" in text and "已跳过" in text for text in warnings)
    assert any("内置 3 + MCP 1" in text for text in infos)
    asyncio.run(runtime.close())


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
