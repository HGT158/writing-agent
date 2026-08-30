"""阶段 4 助手、普通任务与完成态文章 API。"""
from __future__ import annotations

import asyncio
import json
import logging
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


def test_task_submission_reserves_lock_before_returning_202(tmp_path):
    runtime = AgentRuntime(_settings(tmp_path))

    async def slow_run(*_args, **_kwargs):
        await asyncio.sleep(0.2)
        return {"status": "done"}

    runtime.run = AsyncMock(side_effect=slow_run)
    with TestClient(_app(tmp_path, runtime)) as client:
        first = client.post("/api/tasks", json={"assistant_id": "default", "task": "一"})
        second = client.post("/api/tasks", json={"assistant_id": "default", "task": "二"})
        assert first.status_code == 202
        assert second.status_code == 409
        _wait_task(client, first.json()["task_id"])


def test_request_body_limit_rejects_before_pydantic_parsing(tmp_path):
    settings = _settings(tmp_path)
    settings.api_max_request_body_mb = 0
    from api.main import create_app

    with TestClient(create_app(settings=settings, start_runtime=False)) as client:
        response = client.post(
            "/api/tasks", json={"assistant_id": "default", "task": "x"}
        )

    assert response.status_code == 413


def test_web_lifespan_logs_that_scheduler_is_disabled(tmp_path, caplog):
    runtime = AgentRuntime(_settings(tmp_path))
    runtime.start = AsyncMock()
    runtime.close = AsyncMock()
    from api.main import create_app
    app = create_app(settings=_settings(tmp_path), runtime=runtime, start_runtime=True)
    caplog.set_level(logging.INFO, logger="api.main")

    with TestClient(app):
        pass

    assert "未启用 Scheduler" in caplog.text


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


def test_assistant_persona_roundtrip_and_lightweight_list(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        created = client.post(
            "/api/assistants",
            json={
                "id": "editor", "name": "编辑助手", "description": "润色",
                "persona": "你是一名严谨的编辑。",
            },
        )
        assert created.status_code == 201

        detail = client.get("/api/assistants/editor")
        assert detail.status_code == 200
        assert detail.json()["persona"] == "你是一名严谨的编辑。"

        listing = client.get("/api/assistants").json()
        assert all("persona" not in item for item in listing)


def test_assistant_create_blank_persona_falls_back_to_default(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        created = client.post(
            "/api/assistants",
            json={"id": "editor", "name": "编辑助手", "persona": "   "},
        )
        assert created.status_code == 201
        detail = client.get("/api/assistants/editor").json()
        assert detail["persona"].strip()

        unknown = client.get("/api/assistants/ghost")
        assert unknown.status_code == 404


def test_assistant_patch_updates_fields_and_validates(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        client.post("/api/assistants", json={"id": "editor", "name": "编辑助手"})

        patched = client.patch(
            "/api/assistants/editor", json={"name": "新名字", "persona": "新人设"}
        )
        assert patched.status_code == 200
        body = patched.json()
        assert body["name"] == "新名字"
        assert body["persona"] == "新人设"
        assert body["description"] == ""

        partial = client.patch("/api/assistants/editor", json={"description": "只改描述"})
        assert partial.status_code == 200
        assert partial.json()["name"] == "新名字"  # 部分更新：未提供的字段不变
        assert partial.json()["description"] == "只改描述"

        empty = client.patch("/api/assistants/editor", json={})
        assert empty.status_code == 400

        unknown = client.patch("/api/assistants/ghost", json={"name": "x"})
        assert unknown.status_code == 404


def test_assistant_patch_rejected_while_task_running(tmp_path):
    runtime = AgentRuntime(_settings(tmp_path))
    with TestClient(_app(tmp_path, runtime)) as client:
        client.post("/api/assistants", json={"id": "editor", "name": "编辑助手"})
        runtime.store.acquire_lock("editor", "task-1")
        busy = client.patch("/api/assistants/editor", json={"name": "新名字"})
        assert busy.status_code == 409
        runtime.store.release_lock("editor", "task-1")
        ok = client.patch("/api/assistants/editor", json={"name": "新名字"})
        assert ok.status_code == 200


def test_assistant_persona_has_bounded_size(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        response = client.post(
            "/api/assistants",
            json={"id": "editor", "name": "编辑助手", "persona": "x" * 50_001},
        )
        patch_response = client.patch(
            "/api/assistants/default", json={"persona": "x" * 50_001}
        )
    assert response.status_code == 422
    assert patch_response.status_code == 422


def test_importing_api_main_does_not_construct_a_default_runtime():
    import api.main as api_main

    assert not hasattr(api_main, "app")


def test_api_rejects_untrusted_host_and_allows_vite_local_host(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        rejected = client.get(
            "/api/assistants", headers={"host": "attacker.example"}
        )
        allowed = client.get(
            "/api/assistants", headers={"host": "127.0.0.1:5173"}
        )

    assert rejected.status_code == 400
    assert allowed.status_code == 200


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
    runtime.run.assert_awaited_once()
    args, kwargs = runtime.run.await_args
    assert args == ("default", "写一篇短文", "session-1")
    assert kwargs["lock_already_held"] is True
    assert kwargs["lock_task_id"] == started.json()["task_id"]


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


def _sse_frames(text: str) -> list[tuple[int | None, dict]]:
    frames = []
    for block in (part for part in text.split("\n\n") if part.strip()):
        frame_id = None
        data = None
        for line in block.splitlines():
            if line.startswith("id: "):
                frame_id = int(line.removeprefix("id: "))
            if line.startswith("data: "):
                data = json.loads(line.removeprefix("data: "))
        if data is not None:
            frames.append((frame_id, data))
    return frames


def test_task_stream_resumes_from_explicit_or_header_cursor(tmp_path):
    runtime = AgentRuntime(_settings(tmp_path))

    async def emit_and_finish(assistant_id, task, session_id=None, **_kwargs):
        for index in range(3):
            runtime.bus.emit("token", text=f"t{index}")
        return {"status": "done"}

    runtime.run = AsyncMock(side_effect=emit_and_finish)

    with TestClient(_app(tmp_path, runtime)) as client:
        started = client.post(
            "/api/tasks", json={"assistant_id": "default", "task": "测试"}
        )
        task_id = started.json()["task_id"]
        _wait_task(client, task_id)

        full = client.get(f"/api/tasks/{task_id}/stream", params={"assistant_id": "default"})
        explicit = client.get(
            f"/api/tasks/{task_id}/stream",
            params={"assistant_id": "default", "after_seq": 1},
        )
        header = client.get(
            f"/api/tasks/{task_id}/stream",
            params={"assistant_id": "default"},
            headers={"Last-Event-ID": "1"},
        )
        precedence = client.get(
            f"/api/tasks/{task_id}/stream",
            params={"assistant_id": "default", "after_seq": 2},
            headers={"Last-Event-ID": "1"},
        )
        invalid_header = client.get(
            f"/api/tasks/{task_id}/stream",
            params={"assistant_id": "default"},
            headers={"Last-Event-ID": "not-a-number"},
        )

    full_frames = _sse_frames(full.text)
    assert [seq for seq, _ in full_frames] == [0, 1, 2, 3]
    assert full_frames[-1][1]["type"] == "task_done"
    assert [seq for seq, _ in _sse_frames(explicit.text)] == [2, 3]
    assert [seq for seq, _ in _sse_frames(header.text)] == [2, 3]
    assert [seq for seq, _ in _sse_frames(precedence.text)] == [3]
    assert [seq for seq, _ in _sse_frames(invalid_header.text)] == [0, 1, 2, 3]


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
