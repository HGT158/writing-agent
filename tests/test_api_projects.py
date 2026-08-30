"""阶段 4 FastAPI 项目工作区接口。"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent.runtime import AgentRuntime
from config.settings import Settings
from memory import projects as project_storage


def _settings(tmp_path: Path) -> Settings:
    empty = tmp_path / "empty.json"
    empty.write_text('{"mcpServers": {}}', encoding="utf-8")
    return Settings(
        project_root=tmp_path,
        data_dir=tmp_path,
        skills_dir=Path(__file__).resolve().parents[1] / "skills",
        mcp_config=empty,
        openai_api_key="fake",
        openai_base_url="",
        model_name="fake",
    )


def _app(tmp_path: Path, runtime: AgentRuntime | None = None):
    from api.main import create_app

    return create_app(settings=_settings(tmp_path), runtime=runtime, start_runtime=False)


def _response(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class FakeLLM:
    def __init__(self, outputs: list[str]):
        self.outputs = iter(outputs)
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        return _response(next(self.outputs))


class FakeAsyncStream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        self._iterator = iter(self.chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class StreamingFakeLLM:
    def __init__(self, turns):
        self.turns = iter(turns)
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        return FakeAsyncStream(next(self.turns))


def _stream_chunk(*, content=None, tool_calls=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _tool_delta(index, *, call_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _wait_task(client: TestClient, task_id: str, assistant_id: str = "default") -> dict:
    for _ in range(100):
        payload = client.get(
            f"/api/tasks/{task_id}", params={"assistant_id": assistant_id}
        ).json()
        if payload["status"] in {"done", "failed"}:
            return payload
        time.sleep(0.01)
    raise AssertionError("task did not finish")


def test_project_crud_document_save_and_isolation(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        assistants = client.get("/api/assistants").json()
        assert any(item["id"] == "default" for item in assistants)

        created = client.post("/api/projects", json={"assistant_id": "default", "name": "测试项目"})
        assert created.status_code == 201
        project = created.json()

        projects = client.get("/api/projects", params={"assistant_id": "default"}).json()
        assert [item["project_id"] for item in projects] == [project["project_id"]]

        tree = client.get(
            f"/api/projects/{project['project_id']}/tree", params={"assistant_id": "default"}
        ).json()
        document_id = tree[0]["document_id"]
        opened = client.get(
            f"/api/projects/{project['project_id']}/documents/{document_id}",
            params={"assistant_id": "default"},
        ).json()
        saved = client.put(
            f"/api/projects/{project['project_id']}/documents/{document_id}",
            json={"assistant_id": "default", "content": "新的正文", "document_version": opened["version"]},
        )
        assert saved.status_code == 200
        assert saved.json()["content"] == "新的正文"
        assert saved.json()["version"] == 2

        stale = client.put(
            f"/api/projects/{project['project_id']}/documents/{document_id}",
            json={"assistant_id": "default", "content": "覆盖", "document_version": 1},
        )
        assert stale.status_code == 409

        hidden = client.get(
            f"/api/projects/{project['project_id']}/documents/{document_id}",
            params={"assistant_id": "other"},
        )
        assert hidden.status_code == 404


def test_project_list_rejects_unknown_assistant(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        response = client.get("/api/projects", params={"assistant_id": "ghost"})
    assert response.status_code == 404


def test_file_and_folder_import_copy_into_managed_projects(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        imported_file = client.post(
            "/api/projects/import-file",
            data={"assistant_id": "default"},
            files={"file": ("draft.md", "导入正文", "text/markdown")},
        )
        assert imported_file.status_code == 201
        file_project = imported_file.json()
        file_tree = client.get(
            f"/api/projects/{file_project['project_id']}/tree", params={"assistant_id": "default"}
        ).json()
        assert file_tree[0]["relative_path"] == "draft.md"

        imported_folder = client.post(
            "/api/projects/import-folder",
            data={"assistant_id": "default", "name": "资料项目", "paths": ["article.md", "notes/source.txt"]},
            files=[
                ("files", ("article.md", "文章", "text/markdown")),
                ("files", ("source.txt", "来源", "text/plain")),
            ],
        )
        assert imported_folder.status_code == 201
        folder_project = imported_folder.json()
        folder_tree = client.get(
            f"/api/projects/{folder_project['project_id']}/tree", params={"assistant_id": "default"}
        ).json()
        assert [item["relative_path"] for item in folder_tree] == ["article.md", "notes/source.txt"]


def test_project_delete_supports_explicit_purge(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        project = client.post(
            "/api/projects", json={"assistant_id": "default", "name": "临时项目"}
        ).json()

        deleted = client.delete(
            f"/api/projects/{project['project_id']}",
            params={"assistant_id": "default", "purge": "true"},
        )

        assert deleted.status_code == 200
        assert deleted.json() == {"purged": True}
        assert client.get("/api/projects", params={"assistant_id": "default"}).json() == []


def test_project_archive_returns_conflict_for_pending_change_set(tmp_path):
    app = _app(tmp_path)
    runtime = app.state.runtime
    project = runtime.store.create_project("default", "待确认项目")
    change = runtime.store.create_selection_change_set(
        "default", project.project_id, project.entry_document_id,
        task_id="task-archive", start=0, end=0, original_text="",
        replacement_text="建议正文", base_version=1, source="selection",
    )

    with TestClient(app) as client:
        response = client.delete(
            f"/api/projects/{project.project_id}", params={"assistant_id": "default"}
        )

    assert change.status == "pending"
    assert response.status_code == 409
    assert "待处理" in response.json()["detail"]


def test_document_rename_and_delete(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        created = client.post("/api/projects", json={"assistant_id": "default", "name": "改名项目"})
        assert created.status_code == 201
        project = created.json()
        tree = client.get(
            f"/api/projects/{project['project_id']}/tree", params={"assistant_id": "default"}
        ).json()
        document_id = tree[0]["document_id"]
        original = client.get(
            f"/api/projects/{project['project_id']}/documents/{document_id}",
            params={"assistant_id": "default"},
        ).json()

        renamed = client.patch(
            f"/api/projects/{project['project_id']}/documents/{document_id}",
            json={"assistant_id": "default", "relative_path": "drafts/renamed.md"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["relative_path"] == "drafts/renamed.md"
        tree_after = client.get(
            f"/api/projects/{project['project_id']}/tree", params={"assistant_id": "default"}
        ).json()
        assert [item["relative_path"] for item in tree_after] == ["drafts/renamed.md"]
        reopened = client.get(
            f"/api/projects/{project['project_id']}/documents/{document_id}",
            params={"assistant_id": "default"},
        ).json()
        assert reopened["content"] == original["content"]

        illegal = client.patch(
            f"/api/projects/{project['project_id']}/documents/{document_id}",
            json={"assistant_id": "default", "relative_path": "../escape.md"},
        )
        assert illegal.status_code == 400
        bad_extension = client.patch(
            f"/api/projects/{project['project_id']}/documents/{document_id}",
            json={"assistant_id": "default", "relative_path": "cover.png"},
        )
        assert bad_extension.status_code == 400
        wrong_assistant = client.patch(
            f"/api/projects/{project['project_id']}/documents/{document_id}",
            json={"assistant_id": "other", "relative_path": "x.md"},
        )
        assert wrong_assistant.status_code == 404

        deleted = client.delete(
            f"/api/projects/{project['project_id']}/documents/{document_id}",
            params={"assistant_id": "default"},
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert deleted.json()["entry_document_id"] is None
        tree_empty = client.get(
            f"/api/projects/{project['project_id']}/tree", params={"assistant_id": "default"}
        ).json()
        assert tree_empty == []
        assert client.get(
            f"/api/projects/{project['project_id']}/documents/{document_id}",
            params={"assistant_id": "default"},
        ).status_code == 404
        assert client.delete(
            f"/api/projects/{project['project_id']}/documents/{document_id}",
            params={"assistant_id": "default"},
        ).status_code == 404


def test_document_rename_delete_reject_conflicts(tmp_path):
    app = _app(tmp_path)
    runtime = app.state.runtime
    imported = runtime.store.import_folder_project(
        "default", "冲突项目导入",
        [("article.md", BytesIO("主文".encode())),
         ("notes/source.txt", BytesIO("来源".encode()))],
    )
    entry_id = imported.entry_document_id
    notes_id = next(
        item.document_id for item in runtime.store.get_project_tree("default", imported.project_id)
        if item.relative_path == "notes/source.txt"
    )

    with TestClient(app) as client:
        # 重命名撞既有路径 → 409，磁盘与元数据不变
        collision = client.patch(
            f"/api/projects/{imported.project_id}/documents/{notes_id}",
            json={"assistant_id": "default", "relative_path": "article.md"},
        )
        assert collision.status_code == 409
        tree = client.get(
            f"/api/projects/{imported.project_id}/tree", params={"assistant_id": "default"}
        ).json()
        assert sorted(item["relative_path"] for item in tree) == ["article.md", "notes/source.txt"]

        # 存在待处理 change set → 重命名与删除都 409
        change = runtime.store.create_selection_change_set(
            "default", imported.project_id, notes_id,
            task_id="task-conflict", start=0, end=0, original_text="",
            replacement_text="建议", base_version=1, source="selection",
        )
        busy_rename = client.patch(
            f"/api/projects/{imported.project_id}/documents/{notes_id}",
            json={"assistant_id": "default", "relative_path": "moved.md"},
        )
        assert busy_rename.status_code == 409
        busy_delete = client.delete(
            f"/api/projects/{imported.project_id}/documents/{notes_id}",
            params={"assistant_id": "default"},
        )
        assert busy_delete.status_code == 409

        # 放弃建议后删除入口文档：入口改指向其余可编辑文档（按路径序）
        rejected = client.post(
            f"/api/projects/{imported.project_id}/change-sets/{change.change_set_id}"
            f"/hunks/{change.hunks[0].hunk_id}/reject",
            json={"assistant_id": "default"},
        )
        assert rejected.status_code == 200
        deleted = client.delete(
            f"/api/projects/{imported.project_id}/documents/{entry_id}",
            params={"assistant_id": "default"},
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert deleted.json()["entry_document_id"] == notes_id
        tree_after = client.get(
            f"/api/projects/{imported.project_id}/tree", params={"assistant_id": "default"}
        ).json()
        assert [item["relative_path"] for item in tree_after] == ["notes/source.txt"]


def test_change_set_endpoints_return_404_for_cross_assistant_access(tmp_path):
    """跨助手持 change set id 访问（phase7 P3-11）：已注册的其他助手访问
    hunk 级端点一律 404，不泄漏资源存在性，change set 状态不受影响。"""
    runtime = AgentRuntime(_settings(tmp_path))
    project = runtime.store.create_project("default", "跨助手项目")
    document, _ = runtime.store.save_document(
        "default", project.project_id, project.entry_document_id,
        "这是原文。后续内容。", expected_version=1,
    )
    change = runtime.store.create_selection_change_set(
        "default", project.project_id, document.document_id,
        task_id="task-isolation", start=0, end=5,
        original_text="这是原文。", replacement_text="这是改写。",
        base_version=document.version, source="selection",
    )

    with TestClient(_app(tmp_path, runtime)) as client:
        client.post(
            "/api/assistants",
            json={"id": "writer-b", "name": "第二助手", "description": "", "persona": ""},
        )
        base = (
            f"/api/projects/{project.project_id}/change-sets/{change.change_set_id}"
            f"/hunks/{change.hunks[0].hunk_id}"
        )
        accept = client.post(f"{base}/accept", json={"assistant_id": "writer-b"})
        reject = client.post(f"{base}/reject", json={"assistant_id": "writer-b"})
        assert accept.status_code == 404
        assert reject.status_code == 404

        untouched = runtime.store.get_change_set(
            "default", project.project_id, change.change_set_id
        )
        assert [hunk.status for hunk in untouched.hunks] == ["pending"]
    asyncio.run(runtime.close())


def test_selection_rewrite_task_sse_and_apply(tmp_path):
    settings = _settings(tmp_path)
    runtime = AgentRuntime(settings)
    runtime.llm = FakeLLM(["精简开头。"])
    project = runtime.store.create_project("default", "AI 改写")
    document, _staled = runtime.store.save_document(
        "default", project.project_id, project.entry_document_id,
        "这是原文。后续内容。", expected_version=1,
    )

    with TestClient(_app(tmp_path, runtime)) as client:
        started = client.post(
            f"/api/projects/{project.project_id}/documents/{document.document_id}/selection-rewrites",
            json={
                "assistant_id": "default",
                "start": 0,
                "end": 5,
                "selected_text": "这是原文。",
                "instruction": "精简",
                "document_version": document.version,
            },
        )
        assert started.status_code == 202
        task = _wait_task(client, started.json()["task_id"])
        assert task["status"] == "done"
        change_set_id = task["result"]["change_set_id"]

        stream = client.get(
            f"/api/tasks/{task['task_id']}/stream",
            params={"assistant_id": "default"},
        )
        assert stream.status_code == 200
        assert "change_preview" in stream.text

        query = client.get(
            f"/api/projects/{project.project_id}/change-sets",
            params={
                "assistant_id": "default", "document_id": document.document_id,
            },
        )
        assert query.status_code == 200
        assert query.json()["total"] == 1
        hunk_id = query.json()["items"][0]["hunks"][0]["hunk_id"]
        applied = client.post(
            f"/api/projects/{project.project_id}/change-sets/{change_set_id}/hunks/{hunk_id}/accept",
            json={"assistant_id": "default"},
        )
        assert applied.status_code == 200
        assert applied.json()["document"]["content"] == "精简开头。后续内容。"
        assert applied.json()["hunk"]["status"] == "applied"
        assert applied.json()["staled_change_set_ids"] == []


def test_project_agent_chat_returns_streamed_reply_and_change_preview(tmp_path):
    settings = _settings(tmp_path)
    runtime = AgentRuntime(settings)
    project = runtime.store.create_project("default", "聊天修改")
    document, _staled = runtime.store.save_document(
        "default", project.project_id, project.entry_document_id,
        "原始段落。", expected_version=1,
    )
    arguments = json.dumps({
        "documents": [{
            "document_id": document.document_id,
            "document_version": document.version,
            "hunks": [{"old_text": "原始段落。", "new_text": "调整段落。"}],
        }],
    }, ensure_ascii=False)
    runtime.llm = StreamingFakeLLM([
        [_stream_chunk(tool_calls=[_tool_delta(
            0,
            call_id="call-api-edit",
            name="propose_project_edits",
            arguments=arguments,
        )])],
        [
            _stream_chunk(content="我准备"),
            _stream_chunk(content="调整语气。"),
        ],
    ])

    with TestClient(_app(tmp_path, runtime)) as client:
        started = client.post(
            f"/api/projects/{project.project_id}/agent/messages",
            json={
                "assistant_id": "default",
                "message": "调整语气",
                "current_document_id": document.document_id,
            },
        )
        assert started.status_code == 202
        assert started.json()["chat_session_id"]
        task = _wait_task(client, started.json()["task_id"])
        assert task["status"] == "done"
        assert task["result"]["reply"] == "我准备调整语气。"
        assert len(task["result"]["change_set_ids"]) == 1
        stream = client.get(
            f"/api/tasks/{task['task_id']}/stream",
            params={"assistant_id": "default"},
        )
        assert stream.status_code == 200
        assert stream.text.count('"type": "token"') == 2
        assert stream.text.count('"type": "tool_call"') == 1
        assert stream.text.count('"type": "tool_result"') == 1
        assert stream.text.count('"type": "change_preview"') == 1
        assert stream.text.count('"type": "task_done"') == 1
        detail = client.get(
            f"/api/projects/{project.project_id}/agent/sessions/"
            f"{started.json()['chat_session_id']}",
            params={"assistant_id": "default"},
        )
        assert detail.status_code == 200
        assert [item["content"] for item in detail.json()["messages"]] == [
            "调整语气", "我准备调整语气。",
        ]
        assert len(detail.json()["pending_changes"]) == 1


def test_project_chat_session_list_detail_delete_and_scope(tmp_path):
    runtime = AgentRuntime(_settings(tmp_path))
    project = runtime.store.create_project("default", "历史项目")
    other_project = runtime.store.create_project("default", "其他项目")
    session = runtime.store.create_project_chat_session("default", project.project_id)
    runtime.store.add_project_chat_message(
        "default", project.project_id, session.chat_session_id,
        "user", "历史问题",
    )
    document = runtime.store.get_document(
        "default", project.project_id, project.entry_document_id
    )
    change = runtime.store.create_selection_change_set(
        "default", project.project_id, document.document_id,
        task_id="task-history", start=0, end=0, original_text="",
        replacement_text="建议正文", base_version=document.version,
        source="chat", session_id=session.chat_session_id,
    )

    with TestClient(_app(tmp_path, runtime)) as client:
        listed = client.get(
            f"/api/projects/{project.project_id}/agent/sessions",
            params={"assistant_id": "default"},
        )
        assert listed.status_code == 200
        assert listed.json()[0]["chat_session_id"] == session.chat_session_id
        assert listed.json()[0]["title"] == "历史问题"

        detail = client.get(
            f"/api/projects/{project.project_id}/agent/sessions/{session.chat_session_id}",
            params={"assistant_id": "default"},
        )
        assert detail.status_code == 200
        assert detail.json()["messages"][0]["content"] == "历史问题"
        assert detail.json()["pending_changes"][0]["change_set_id"] == change.change_set_id

        cross_project = client.get(
            f"/api/projects/{other_project.project_id}/agent/sessions/{session.chat_session_id}",
            params={"assistant_id": "default"},
        )
        cross_assistant = client.get(
            f"/api/projects/{project.project_id}/agent/sessions/{session.chat_session_id}",
            params={"assistant_id": "ghost"},
        )
        assert cross_project.status_code == 404
        assert cross_assistant.status_code == 404

        blocked = client.delete(
            f"/api/projects/{project.project_id}/agent/sessions/{session.chat_session_id}",
            params={"assistant_id": "default"},
        )
        assert blocked.status_code == 409
        runtime.store.reject_change_hunk(
            "default", project.project_id, change.change_set_id,
            change.hunks[0].hunk_id,
        )
        deleted = client.delete(
            f"/api/projects/{project.project_id}/agent/sessions/{session.chat_session_id}",
            params={"assistant_id": "default"},
        )
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True}


def test_project_chat_rejects_invalid_session_or_document_before_enqueue(tmp_path):
    app = _app(tmp_path)
    runtime = app.state.runtime
    project = runtime.store.create_project("default", "会话预检")
    session = runtime.store.create_project_chat_session("default", project.project_id)

    with TestClient(app) as client:
        missing_session = client.post(
            f"/api/projects/{project.project_id}/agent/messages",
            json={
                "assistant_id": "default",
                "message": "继续",
                "chat_session_id": "missing-session",
            },
        )
        missing_document = client.post(
            f"/api/projects/{project.project_id}/agent/messages",
            json={
                "assistant_id": "default",
                "message": "分析",
                "chat_session_id": session.chat_session_id,
                "current_document_id": "missing-document",
            },
        )
        blank_message = client.post(
            f"/api/projects/{project.project_id}/agent/messages",
            json={"assistant_id": "default", "message": "   \n  "},
        )

        assert missing_session.status_code == 404
        assert missing_document.status_code == 404
        assert blank_message.status_code == 400
        assert len(runtime.store.list_project_chat_sessions(
            "default", project.project_id
        )) == 1
        assert app.state.tasks.records == {}


def test_failed_new_project_chat_does_not_leave_empty_session(tmp_path):
    settings = _settings(tmp_path)
    settings.openai_api_key = ""
    runtime = AgentRuntime(settings)
    project = runtime.store.create_project("default", "失败清理")

    with TestClient(_app(tmp_path, runtime)) as client:
        started = client.post(
            f"/api/projects/{project.project_id}/agent/messages",
            json={"assistant_id": "default", "message": "分析正文"},
        )
        assert started.status_code == 202
        task = _wait_task(client, started.json()["task_id"])
        assert task["status"] == "failed"
        assert runtime.store.list_project_chat_sessions(
            "default", project.project_id
        ) == []


def test_cancelled_new_project_chat_does_not_leave_empty_session(tmp_path):
    app = _app(tmp_path)
    runtime = app.state.runtime
    project = runtime.store.create_project("default", "取消清理")

    async def cancel_chat(*_args, **_kwargs):
        raise asyncio.CancelledError

    runtime.chat_project = cancel_chat
    with TestClient(app) as client:
        started = client.post(
            f"/api/projects/{project.project_id}/agent/messages",
            json={"assistant_id": "default", "message": "分析正文"},
        )
        assert started.status_code == 202
        task = _wait_task(client, started.json()["task_id"])
        assert task["status"] == "failed"
        assert runtime.store.list_project_chat_sessions(
            "default", project.project_id
        ) == []


def test_new_chat_cleanup_failure_does_not_mask_task_error(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.openai_api_key = ""
    runtime = AgentRuntime(settings)
    project = runtime.store.create_project("default", "清理异常")
    monkeypatch.setattr(
        runtime.store,
        "delete_empty_project_chat_session",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )

    with TestClient(_app(tmp_path, runtime)) as client:
        started = client.post(
            f"/api/projects/{project.project_id}/agent/messages",
            json={"assistant_id": "default", "message": "分析正文"},
        )
        task = _wait_task(client, started.json()["task_id"])

    assert task["status"] == "failed"
    assert "未配置 OPENAI_API_KEY" in task["error"]


def test_project_chat_message_has_same_length_limit_as_agent_task(tmp_path):
    app = _app(tmp_path)
    runtime = app.state.runtime
    project = runtime.store.create_project("default", "消息上限")

    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project.project_id}/agent/messages",
            json={"assistant_id": "default", "message": "x" * 100_001},
        )
        assert response.status_code == 422
        assert runtime.store.list_project_chat_sessions(
            "default", project.project_id
        ) == []


def test_editing_task_endpoints_reject_unknown_or_busy_assistant_before_enqueue(tmp_path):
    app = _app(tmp_path)
    runtime = app.state.runtime
    project = runtime.store.create_project("default", "预检")
    document_id = project.entry_document_id
    selection_url = (
        f"/api/projects/{project.project_id}/documents/{document_id}/selection-rewrites"
    )
    selection_body = {
        "assistant_id": "ghost",
        "start": 0,
        "end": 0,
        "selected_text": "",
        "instruction": "改写",
        "document_version": 1,
    }

    with TestClient(app) as client:
        unknown_selection = client.post(selection_url, json=selection_body)
        unknown_chat = client.post(
            f"/api/projects/{project.project_id}/agent/messages",
            json={"assistant_id": "ghost", "message": "修改"},
        )
        runtime.store.acquire_lock("default", "existing-task")
        busy_selection = client.post(
            selection_url, json={**selection_body, "assistant_id": "default"}
        )
        busy_chat = client.post(
            f"/api/projects/{project.project_id}/agent/messages",
            json={"assistant_id": "default", "message": "修改"},
        )
        runtime.store.release_lock("default", "existing-task")

    assert unknown_selection.status_code == 404
    assert unknown_chat.status_code == 404
    assert busy_selection.status_code == 409
    assert busy_chat.status_code == 409
    assert app.state.tasks.records == {}


def test_openapi_declares_code_point_offsets_and_required_apply_version(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        schemas = client.get("/openapi.json").json()["components"]["schemas"]

    selection = schemas["SelectionRewriteRequest"]
    assert "Unicode code point" in selection["properties"]["start"]["description"]
    assert "Unicode code point" in selection["properties"]["end"]["description"]
    assert schemas["ChangeSetHunkAction"]["required"] == ["assistant_id"]


def test_live_document_write_conflict_maps_to_http_409(tmp_path, monkeypatch):
    app = _app(tmp_path)
    runtime = app.state.runtime
    project = runtime.store.create_project("default", "写入冲突")
    document_id = project.entry_document_id
    runtime.store._conn.execute(
        "INSERT INTO document_write_intents "
        "(intent_id, assistant_id, project_id, document_id, change_set_id, expected_version, "
        "target_version, relative_path, content, owner_pid, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "live-intent", "default", project.project_id, document_id, None, 1, 2,
            "article.md", "其他进程正文", 424242, datetime.now(timezone.utc).isoformat(),
        ),
    )
    runtime.store._conn.commit()
    monkeypatch.setattr(project_storage.psutil, "pid_exists", lambda _pid: True)

    with TestClient(app) as client:
        response = client.put(
            f"/api/projects/{project.project_id}/documents/{document_id}",
            json={
                "assistant_id": "default",
                "content": "当前请求正文",
                "document_version": 1,
            },
        )

    assert response.status_code == 409
