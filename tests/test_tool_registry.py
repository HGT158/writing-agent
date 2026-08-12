"""统一工具表测试：内置工具沙箱、ToolContext 注入、MCP 工具同协议注册。"""
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from agent.executor import ToolRegistry, execute_tool_calls
from agent.events import EventBus
from agent.schemas import ToolCall, ToolContext, ToolSpec
from agent.tools import make_builtin_tools, make_project_edit_tool
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


def test_project_edit_tool_creates_pending_change_without_writing_document(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("tester", "编辑提案")
    document = store.save_document(
        "tester",
        project.project_id,
        project.entry_document_id,
        "第一段原文。第二段原文。",
        expected_version=1,
    )
    spec = make_project_edit_tool(store, project.project_id)

    result = json.loads(asyncio.run(spec.call({
        "changes": [{
            "document_id": document.document_id,
            "old_text": "第一段原文。",
            "new_text": "首段精简。",
            "document_version": document.version,
        }],
    }, _ctx(tmp_path))))

    assert spec.name == "propose_project_edits"
    assert spec.idempotent is False
    assert result["count"] == 1
    assert len(result["change_set_ids"]) == 1
    change = store.get_change_set(
        "tester", project.project_id, result["change_set_ids"][0]
    )
    assert change.status == "pending"
    assert (change.start, change.end) == (0, len("第一段原文。"))
    assert change.original_text == "第一段原文。"
    assert change.replacement_text == "首段精简。"
    current = store.get_document("tester", project.project_id, document.document_id)
    assert current.content == "第一段原文。第二段原文。"
    store.close()


def test_project_edit_tool_inserts_first_draft_into_empty_document(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("tester", "空白首稿")
    document = store.get_document(
        "tester", project.project_id, project.entry_document_id
    )
    spec = make_project_edit_tool(store, project.project_id)

    result = json.loads(asyncio.run(spec.call({
        "changes": [{
            "document_id": document.document_id,
            "old_text": "",
            "new_text": "# 小锅鱼的深圳奇遇\n\n故事正文。",
            "document_version": document.version,
        }],
    }, _ctx(tmp_path))))

    change = store.get_change_set(
        "tester", project.project_id, result["change_set_ids"][0]
    )
    assert (change.start, change.end, change.original_text) == (0, 0, "")
    assert store.get_document(
        "tester", project.project_id, document.document_id
    ).content == ""

    applied, applied_change = store.apply_change_set(
        "tester",
        project.project_id,
        change.change_set_id,
        expected_version=document.version,
    )
    assert applied.content == "# 小锅鱼的深圳奇遇\n\n故事正文。"
    assert applied_change.status == "applied"
    store.close()


def test_project_edit_tool_rejects_empty_old_text_for_nonempty_document(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("tester", "非空文档")
    document = store.save_document(
        "tester",
        project.project_id,
        project.entry_document_id,
        "已有正文。",
        expected_version=1,
    )
    spec = make_project_edit_tool(store, project.project_id)

    with pytest.raises(ResourceConflictError, match="非空文档不能使用空旧文本"):
        asyncio.run(spec.call({
            "changes": [{
                "document_id": document.document_id,
                "old_text": "",
                "new_text": "新增正文。",
                "document_version": document.version,
            }],
        }, _ctx(tmp_path)))

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
        asyncio.run(spec.call({
            "changes": [{
                "document_id": document.document_id,
                "new_text": "新正文。",
                "document_version": document.version,
            }],
        }, _ctx(tmp_path)))

    store.close()


def test_project_edit_tool_rejects_missing_old_text_without_writing_document(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("tester", "缺失旧文本")
    document = store.save_document(
        "tester",
        project.project_id,
        project.entry_document_id,
        "当前正文。",
        expected_version=1,
    )
    spec = make_project_edit_tool(store, project.project_id)

    with pytest.raises(ResourceConflictError, match="旧文本不存在"):
        asyncio.run(spec.call({
            "changes": [{
                "document_id": document.document_id,
                "old_text": "不存在的句子。",
                "new_text": "替换句子。",
                "document_version": document.version,
            }],
        }, _ctx(tmp_path)))

    current = store.get_document("tester", project.project_id, document.document_id)
    assert current.content == "当前正文。"
    store.close()


def test_project_edit_tool_rejects_ambiguous_old_text_without_writing_document(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("tester", "重复旧文本")
    document = store.save_document(
        "tester",
        project.project_id,
        project.entry_document_id,
        "重复句。中间。重复句。",
        expected_version=1,
    )
    spec = make_project_edit_tool(store, project.project_id)

    with pytest.raises(ResourceConflictError, match="旧文本匹配多处"):
        asyncio.run(spec.call({
            "changes": [{
                "document_id": document.document_id,
                "old_text": "重复句。",
                "new_text": "精简句。",
                "document_version": document.version,
            }],
        }, _ctx(tmp_path)))

    current = store.get_document("tester", project.project_id, document.document_id)
    assert current.content == "重复句。中间。重复句。"
    store.close()


def test_project_edit_tool_rejects_duplicate_document_changes_atomically(tmp_path):
    store = MemoryStore(tmp_path)
    project = store.create_project("tester", "重复文档")
    document = store.save_document(
        "tester",
        project.project_id,
        project.entry_document_id,
        "第一句。第二句。",
        expected_version=1,
    )
    spec = make_project_edit_tool(store, project.project_id)

    with pytest.raises(ValueError, match="每个文档只能出现一次"):
        asyncio.run(spec.call({
            "changes": [
                {
                    "document_id": document.document_id,
                    "old_text": "第一句。",
                    "new_text": "首句。",
                    "document_version": document.version,
                },
                {
                    "document_id": document.document_id,
                    "old_text": "第二句。",
                    "new_text": "次句。",
                    "document_version": document.version,
                },
            ],
        }, _ctx(tmp_path)))

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
    document = store.save_document(
        "tester",
        project.project_id,
        project.entry_document_id,
        "当前正文。",
        expected_version=1,
    )
    spec = make_project_edit_tool(store, project.project_id)

    with pytest.raises(ResourceConflictError, match="版本冲突"):
        asyncio.run(spec.call({
            "changes": [{
                "document_id": document.document_id,
                "old_text": "旧版本中已经删除的文字。",
                "new_text": "替换文字。",
                "document_version": document.version - 1,
            }],
        }, _ctx(tmp_path)))

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
