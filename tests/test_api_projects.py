"""阶段 4 FastAPI 项目工作区接口。"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
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
    change = runtime.store.create_change_set(
        "default", project.project_id, project.entry_document_id,
        source="selection", start=0, end=0, original_text="",
        replacement_text="建议正文", base_version=1,
    )

    with TestClient(app) as client:
        response = client.delete(
            f"/api/projects/{project.project_id}", params={"assistant_id": "default"}
        )

    assert change.status == "pending"
    assert response.status_code == 409
    assert "待处理" in response.json()["detail"]


def test_selection_rewrite_task_sse_and_apply(tmp_path):
    settings = _settings(tmp_path)
    runtime = AgentRuntime(settings)
    runtime.llm = FakeLLM(["精简开头。"])
    project = runtime.store.create_project("default", "AI 改写")
    document = runtime.store.save_document(
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

        applied = client.post(
            f"/api/projects/{project.project_id}/change-sets/{change_set_id}/apply",
            json={"assistant_id": "default", "document_version": document.version},
        )
        assert applied.status_code == 200
        assert applied.json()["document"]["content"] == "精简开头。后续内容。"


def test_project_agent_chat_returns_reply_and_change_preview(tmp_path):
    settings = _settings(tmp_path)
    runtime = AgentRuntime(settings)
    project = runtime.store.create_project("default", "聊天修改")
    document = runtime.store.save_document(
        "default", project.project_id, project.entry_document_id,
        "原始段落。", expected_version=1,
    )
    runtime.llm = FakeLLM([json.dumps({
        "reply": "我准备调整语气。",
        "changes": [{
            "document_id": document.document_id,
            "start": 0,
            "end": 5,
            "original_text": "原始段落。",
            "replacement_text": "调整段落。",
            "document_version": document.version,
        }],
    }, ensure_ascii=False)])

    with TestClient(_app(tmp_path, runtime)) as client:
        started = client.post(
            f"/api/projects/{project.project_id}/agent/messages",
            json={
                "assistant_id": "default",
                "message": "调整语气",
                "current_document_id": document.document_id,
            },
        )
        task = _wait_task(client, started.json()["task_id"])
        assert task["status"] == "done"
        assert task["result"]["reply"] == "我准备调整语气。"
        assert len(task["result"]["change_set_ids"]) == 1


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
    assert "document_version" in schemas["ChangeSetAction"]["required"]


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
