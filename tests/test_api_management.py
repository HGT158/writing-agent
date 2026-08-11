"""阶段 4 助手、普通任务与完成态文章 API。"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from agent.runtime import AgentRuntime
from agent.tools import finalize_article_impl
from config.settings import Settings
from agent.schemas import ToolContext


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


def _wait_task(client: TestClient, task_id: str, assistant_id: str = "default") -> dict:
    for _ in range(100):
        payload = client.get(
            f"/api/tasks/{task_id}", params={"assistant_id": assistant_id}
        ).json()
        if payload["status"] in {"done", "failed"}:
            return payload
        time.sleep(0.01)
    raise AssertionError("task did not finish")


def test_assistant_create_and_archive_api(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        created = client.post(
            "/api/assistants",
            json={"id": "editor", "name": "编辑助手", "description": "负责润色"},
        )
        assert created.status_code == 201
        assert created.json()["id"] == "editor"

        archived = client.delete("/api/assistants/editor")
        assert archived.status_code == 200
        assert "archive" in archived.json()["archived_path"]
        assert all(item["id"] != "editor" for item in client.get("/api/assistants").json())


def test_importing_api_main_does_not_construct_a_default_runtime():
    import api.main as api_main

    assert not hasattr(api_main, "app")


def test_general_agent_task_api_uses_runtime_and_task_broker(tmp_path):
    runtime = AgentRuntime(_settings(tmp_path))
    runtime.run = AsyncMock(return_value={
        "assistant_id": "default",
        "session_id": "session-1",
        "status": "done",
        "output_path": "article.md",
    })

    with TestClient(_app(tmp_path, runtime)) as client:
        started = client.post(
            "/api/tasks",
            json={"assistant_id": "default", "task": "写一篇短文", "session_id": "session-1"},
        )
        assert started.status_code == 202
        finished = _wait_task(client, started.json()["task_id"])

    assert finished["status"] == "done"
    assert finished["result"]["output_path"] == "article.md"
    runtime.run.assert_awaited_once_with("default", "写一篇短文", "session-1")


def test_general_task_rejects_unknown_or_busy_assistant_before_enqueue(tmp_path):
    runtime = AgentRuntime(_settings(tmp_path))
    runtime.run = AsyncMock()
    with TestClient(_app(tmp_path, runtime)) as client:
        unknown = client.post(
            "/api/tasks",
            json={"assistant_id": "ghost", "task": "测试"},
        )
        runtime.store.acquire_lock("default", "existing-task")
        busy = client.post(
            "/api/tasks",
            json={"assistant_id": "default", "task": "测试"},
        )
        runtime.store.release_lock("default", "existing-task")

    assert unknown.status_code == 404
    assert busy.status_code == 409
    runtime.run.assert_not_awaited()


def test_task_input_has_a_bounded_size(tmp_path):
    runtime = AgentRuntime(_settings(tmp_path))
    runtime.run = AsyncMock(return_value={"status": "done"})
    with TestClient(_app(tmp_path, runtime)) as client:
        response = client.post(
            "/api/tasks",
            json={"assistant_id": "default", "task": "x" * 100_001},
        )
    assert response.status_code == 422


def test_task_status_and_stream_are_assistant_scoped(tmp_path):
    runtime = AgentRuntime(_settings(tmp_path))
    runtime.assistants.create("other", "其他助手")
    runtime.run = AsyncMock(return_value={"status": "done"})

    with TestClient(_app(tmp_path, runtime)) as client:
        started = client.post(
            "/api/tasks",
            json={"assistant_id": "default", "task": "测试"},
        )
        task_id = started.json()["task_id"]
        _wait_task(client, task_id)
        hidden_status = client.get(
            f"/api/tasks/{task_id}", params={"assistant_id": "other"}
        )
        hidden_stream = client.get(
            f"/api/tasks/{task_id}/stream", params={"assistant_id": "other"}
        )

    assert hidden_status.status_code == 404
    assert hidden_stream.status_code == 404


def test_completed_articles_are_read_only_and_assistant_isolated(tmp_path):
    runtime = AgentRuntime(_settings(tmp_path))
    path = finalize_article_impl(
        runtime.store,
        ToolContext(assistant_id="default", session_id="s1", data_dir=str(tmp_path)),
        "已完成文章",
        "# 正文\n",
    )

    with TestClient(_app(tmp_path, runtime)) as client:
        listed = client.get("/api/articles", params={"assistant_id": "default"})
        assert listed.status_code == 200
        article = listed.json()[0]
        assert article["title"] == "已完成文章"
        assert "content" not in article

        opened = client.get(
            f"/api/articles/{article['article_id']}", params={"assistant_id": "default"}
        )
        assert opened.status_code == 200
        assert opened.json()["content"] == "# 正文\n"
        assert Path(opened.json()["path"]) == path

        hidden = client.get(
            f"/api/articles/{article['article_id']}", params={"assistant_id": "other"}
        )
        assert hidden.status_code == 404
