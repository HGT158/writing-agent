"""FastAPI 应用工厂：本地单用户接口、SSE 与静态前端托管。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent.runtime import AgentRuntime
from config.settings import Settings, load_settings
from memory.errors import ResourceConflictError
from memory.store import AssistantBusyError

from .models import (
    AgentTaskRequest,
    AssistantCreate,
    ChangeSetAction,
    ChangeSetReject,
    DocumentSave,
    ProjectChatRequest,
    ProjectCreate,
    ProjectRename,
    SelectionRewriteRequest,
)
from .tasks import TaskBroker

logger = logging.getLogger(__name__)


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, (KeyError, FileNotFoundError)):
        raise HTTPException(status_code=404, detail="资源不存在") from exc
    if isinstance(exc, (AssistantBusyError, ResourceConflictError)):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, (ValueError, RuntimeError)):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


def _change_preview(change) -> dict:
    return {
        "change_set_id": change.change_set_id,
        "project_id": change.project_id,
        "document_id": change.document_id,
        "range": {"from": change.start, "to": change.end},
        "original": change.original_text,
        "replacement": change.replacement_text,
        "document_version": change.base_version,
        "source": change.source,
    }


def create_app(
    settings: Settings | None = None,
    runtime: AgentRuntime | None = None,
    *,
    start_runtime: bool = True,
) -> FastAPI:
    settings = settings or load_settings()
    runtime = runtime or AgentRuntime(settings)
    broker = TaskBroker(runtime.bus)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_runtime:
            await runtime.start()
        try:
            yield
        finally:
            await broker.shutdown()
            await runtime.close()

    app = FastAPI(title="个人写作 Agent", version="1.0", lifespan=lifespan)
    app.state.runtime = runtime
    app.state.tasks = broker

    def validate_task_submission(assistant_id: str) -> None:
        runtime.assistants.get(assistant_id)
        if runtime.store.is_locked(assistant_id):
            raise AssistantBusyError(assistant_id, "已有任务运行中")

    @app.get("/api/assistants")
    async def list_assistants():
        return [
            {"id": item.id, "name": item.name, "description": item.description}
            for item in runtime.assistants.list()
        ]

    @app.post("/api/assistants", status_code=status.HTTP_201_CREATED)
    async def create_assistant(body: AssistantCreate):
        try:
            item = runtime.assistants.create(body.id, body.name, body.description)
            return {"id": item.id, "name": item.name, "description": item.description}
        except Exception as exc:
            _raise_http(exc)

    @app.delete("/api/assistants/{assistant_id}")
    async def delete_assistant(assistant_id: str, purge: bool = Query(False)):
        try:
            archived = runtime.assistants.delete(assistant_id, purge=purge)
            return {"archived_path": str(archived), "purged": purge}
        except Exception as exc:
            _raise_http(exc)

    @app.post("/api/tasks", status_code=status.HTTP_202_ACCEPTED)
    async def start_task(body: AgentTaskRequest):
        try:
            validate_task_submission(body.assistant_id)
        except Exception as exc:
            _raise_http(exc)

        async def operation():
            result = await runtime.run(body.assistant_id, body.task, body.session_id)
            return dict(result)

        return {"task_id": broker.start(body.assistant_id, operation)}

    @app.get("/api/articles")
    async def list_articles(assistant_id: str = Query(...)):
        try:
            runtime.assistants.get(assistant_id)
        except Exception as exc:
            _raise_http(exc)
        return [asdict(item) for item in runtime.store.list_articles(assistant_id)]

    @app.get("/api/articles/{article_id}")
    async def get_article(article_id: int, assistant_id: str = Query(...)):
        try:
            record, content = runtime.store.get_article(assistant_id, article_id)
            return {**asdict(record), "content": content}
        except Exception as exc:
            _raise_http(exc)

    @app.get("/api/projects")
    async def list_projects(assistant_id: str = Query(...)):
        try:
            runtime.assistants.get(assistant_id)
        except Exception as exc:
            _raise_http(exc)
        return [asdict(item) for item in runtime.store.list_projects(assistant_id)]

    @app.post("/api/projects", status_code=status.HTTP_201_CREATED)
    async def create_project(body: ProjectCreate):
        try:
            runtime.assistants.get(body.assistant_id)
            return asdict(runtime.store.create_project(body.assistant_id, body.name))
        except Exception as exc:
            _raise_http(exc)

    @app.patch("/api/projects/{project_id}")
    async def rename_project(project_id: str, body: ProjectRename):
        try:
            return asdict(runtime.store.rename_project(body.assistant_id, project_id, body.name))
        except Exception as exc:
            _raise_http(exc)

    @app.delete("/api/projects/{project_id}")
    async def archive_project(
        project_id: str,
        assistant_id: str = Query(...),
        purge: bool = Query(False),
    ):
        try:
            if purge:
                runtime.store.purge_project(assistant_id, project_id)
                return {"purged": True}
            archived = runtime.store.archive_project(assistant_id, project_id)
            return {"archived_path": str(archived)}
        except Exception as exc:
            _raise_http(exc)

    @app.post("/api/projects/import-file", status_code=status.HTTP_201_CREATED)
    async def import_file(assistant_id: str = Form(...), file: UploadFile = File(...)):
        try:
            runtime.assistants.get(assistant_id)
            project = runtime.store.import_text_project(
                assistant_id,
                file.filename or "",
                file.file,
                max_files=settings.project_import_max_files,
                max_total_bytes=settings.project_import_max_total_mb * 1024 * 1024,
                max_file_bytes=settings.project_import_max_file_mb * 1024 * 1024,
            )
            return asdict(project)
        except Exception as exc:
            _raise_http(exc)

    @app.post("/api/projects/import-folder", status_code=status.HTTP_201_CREATED)
    async def import_folder(
        assistant_id: str = Form(...),
        name: str = Form(...),
        paths: list[str] = Form(...),
        files: list[UploadFile] = File(...),
    ):
        if len(paths) != len(files):
            raise HTTPException(status_code=400, detail="paths 与 files 数量不一致")
        try:
            runtime.assistants.get(assistant_id)
            project = runtime.store.import_folder_project(
                assistant_id,
                name,
                [(relative, upload.file) for relative, upload in zip(paths, files, strict=True)],
                max_files=settings.project_import_max_files,
                max_total_bytes=settings.project_import_max_total_mb * 1024 * 1024,
                max_file_bytes=settings.project_import_max_file_mb * 1024 * 1024,
            )
            return asdict(project)
        except Exception as exc:
            _raise_http(exc)

    @app.get("/api/projects/{project_id}/tree")
    async def project_tree(project_id: str, assistant_id: str = Query(...)):
        try:
            return [asdict(item) for item in runtime.store.get_project_tree(assistant_id, project_id)]
        except Exception as exc:
            _raise_http(exc)

    @app.get("/api/projects/{project_id}/documents/{document_id}")
    async def get_document(project_id: str, document_id: str, assistant_id: str = Query(...)):
        try:
            return asdict(runtime.store.get_document(assistant_id, project_id, document_id))
        except Exception as exc:
            _raise_http(exc)

    @app.put("/api/projects/{project_id}/documents/{document_id}")
    async def save_document(project_id: str, document_id: str, body: DocumentSave):
        try:
            document = runtime.store.save_document(
                body.assistant_id, project_id, document_id, body.content,
                expected_version=body.document_version,
            )
            return asdict(document)
        except Exception as exc:
            _raise_http(exc)

    @app.post(
        "/api/projects/{project_id}/documents/{document_id}/selection-rewrites",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def selection_rewrite(project_id: str, document_id: str, body: SelectionRewriteRequest):
        try:
            validate_task_submission(body.assistant_id)
        except Exception as exc:
            _raise_http(exc)

        async def operation():
            change = await runtime.rewrite_selection(
                body.assistant_id, project_id, document_id,
                start=body.start, end=body.end, selected_text=body.selected_text,
                instruction=body.instruction, document_version=body.document_version,
            )
            return {"change_set_id": change.change_set_id}

        return {"task_id": broker.start(body.assistant_id, operation)}

    @app.post("/api/projects/{project_id}/change-sets/{change_set_id}/apply")
    async def apply_change(project_id: str, change_set_id: str, body: ChangeSetAction):
        try:
            document, change = runtime.store.apply_change_set(
                body.assistant_id, project_id, change_set_id,
                expected_version=body.document_version,
            )
            return {"document": asdict(document), "change_set": asdict(change)}
        except Exception as exc:
            _raise_http(exc)

    @app.post("/api/projects/{project_id}/change-sets/{change_set_id}/reject")
    async def reject_change(project_id: str, change_set_id: str, body: ChangeSetReject):
        try:
            return asdict(runtime.store.reject_change_set(body.assistant_id, project_id, change_set_id))
        except Exception as exc:
            _raise_http(exc)

    @app.post("/api/projects/{project_id}/agent/messages", status_code=status.HTTP_202_ACCEPTED)
    async def project_chat(project_id: str, body: ProjectChatRequest):
        try:
            if not body.message.strip():
                raise ValueError("消息不能为空")
            validate_task_submission(body.assistant_id)
            runtime.store.get_project_tree(body.assistant_id, project_id)
            if body.current_document_id is not None:
                runtime.store.get_document(
                    body.assistant_id, project_id, body.current_document_id
                )
            if body.chat_session_id is None:
                created_chat_session = True
                chat_session_id = runtime.store.create_project_chat_session(
                    body.assistant_id, project_id
                ).chat_session_id
            else:
                created_chat_session = False
                runtime.store.get_project_chat_session(
                    body.assistant_id, project_id, body.chat_session_id
                )
                chat_session_id = body.chat_session_id
        except Exception as exc:
            _raise_http(exc)

        async def operation():
            try:
                result = await runtime.chat_project(
                    body.assistant_id, project_id, body.message,
                    chat_session_id=chat_session_id,
                    current_document_id=body.current_document_id,
                )
                return {
                    "reply": result.reply,
                    "change_set_ids": [item.change_set_id for item in result.changes],
                }
            finally:
                if created_chat_session:
                    try:
                        runtime.store.delete_empty_project_chat_session(
                            body.assistant_id, project_id, chat_session_id
                        )
                    except Exception:
                        logger.warning(
                            "清理空项目聊天会话失败（assistant=%s project=%s session=%s）",
                            body.assistant_id,
                            project_id,
                            chat_session_id,
                            exc_info=True,
                        )

        return {
            "task_id": broker.start(body.assistant_id, operation),
            "chat_session_id": chat_session_id,
        }

    @app.get("/api/projects/{project_id}/agent/sessions")
    async def list_project_chat_sessions(
        project_id: str, assistant_id: str = Query(...)
    ):
        try:
            return [
                asdict(item)
                for item in runtime.store.list_project_chat_sessions(
                    assistant_id, project_id
                )
            ]
        except Exception as exc:
            _raise_http(exc)

    @app.get("/api/projects/{project_id}/agent/sessions/{chat_session_id}")
    async def get_project_chat_session(
        project_id: str,
        chat_session_id: str,
        assistant_id: str = Query(...),
    ):
        try:
            session = runtime.store.get_project_chat_session(
                assistant_id, project_id, chat_session_id
            )
            messages = runtime.store.list_project_chat_messages(
                assistant_id, project_id, chat_session_id
            )
            pending = runtime.store.list_pending_chat_changes(
                assistant_id, project_id, chat_session_id
            )
            return {
                "session": asdict(session),
                "messages": [asdict(item) for item in messages],
                "pending_changes": [_change_preview(item) for item in pending],
            }
        except Exception as exc:
            _raise_http(exc)

    @app.delete("/api/projects/{project_id}/agent/sessions/{chat_session_id}")
    async def delete_project_chat_session(
        project_id: str,
        chat_session_id: str,
        assistant_id: str = Query(...),
    ):
        try:
            runtime.store.delete_project_chat_session(
                assistant_id, project_id, chat_session_id
            )
            return {"deleted": True}
        except Exception as exc:
            _raise_http(exc)

    @app.get("/api/tasks/{task_id}")
    async def task_status(task_id: str, assistant_id: str = Query(...)):
        try:
            record = broker.get(task_id, assistant_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        return {
            "task_id": record.task_id,
            "status": record.status,
            "result": record.result,
            "error": record.error,
        }

    @app.get("/api/tasks/{task_id}/stream")
    async def task_stream(task_id: str, assistant_id: str = Query(...)):
        try:
            broker.get(task_id, assistant_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        return StreamingResponse(
            broker.stream(task_id, assistant_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    dist = settings.project_root / "web" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    return app
