"""FastAPI 应用工厂：本地单用户接口、SSE 与静态前端托管。"""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
import anyio

from agent.runtime import AgentRuntime
from config.settings import Settings, load_settings
from memory.errors import ChangeSetStateError, ResourceConflictError
from memory.store import AssistantBusyError

from .models import (
    AgentTaskRequest,
    AssistantCreate,
    AssistantUpdate,
    ChangeSetHunkAction,
    DocumentRename,
    DocumentSave,
    ProjectChatRequest,
    ProjectCreate,
    ProjectRename,
    SelectionRewriteRequest,
)
from .middleware import RequestBodyLimitMiddleware
from .tasks import TaskBroker

logger = logging.getLogger(__name__)


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, (KeyError, FileNotFoundError)):
        raise HTTPException(status_code=404, detail="资源不存在") from exc
    if isinstance(exc, ChangeSetStateError):
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
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
        "hunks": [{
            "hunk_id": hunk.hunk_id,
            "range": {"from": hunk.start, "to": hunk.end},
            "original": hunk.original_text,
            "replacement": hunk.new_text,
            "status": hunk.status,
        } for hunk in change.hunks],
        "document_version": change.base_version,
        "chat_session_id": change.session_id,
        "source": change.source,
        "status": change.status,
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
            logger.info("Web 服务模式未启用 Scheduler；定时任务请使用 python -m agent schedule")
        try:
            yield
        finally:
            await broker.shutdown()
            await runtime.close()

    app = FastAPI(title="个人写作 Agent", version="1.0", lifespan=lifespan)
    trusted_hosts = ["127.0.0.1", "localhost"]
    if not start_runtime:
        trusted_hosts.append("testserver")
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=trusted_hosts,
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=settings.api_max_request_body_mb * 1024 * 1024,
    )
    app.state.runtime = runtime
    app.state.tasks = broker

    def reserve_task_submission(assistant_id: str) -> str:
        runtime.assistants.get(assistant_id)
        task_id = uuid.uuid4().hex[:16]
        runtime.store.acquire_lock(assistant_id, task_id)
        return task_id

    def start_reserved_task(assistant_id: str, task_id: str, operation) -> str:
        try:
            return broker.start(assistant_id, operation, task_id=task_id)
        except Exception:
            runtime.store.release_lock(assistant_id, task_id)
            raise

    @app.get("/api/assistants")
    async def list_assistants():
        return [
            {"id": item.id, "name": item.name, "description": item.description}
            for item in runtime.assistants.list()
        ]

    @app.post("/api/assistants", status_code=status.HTTP_201_CREATED)
    async def create_assistant(body: AssistantCreate):
        try:
            item = await asyncio.to_thread(
                runtime.assistants.create, body.id, body.name, body.description, body.persona
            )
            return {"id": item.id, "name": item.name, "description": item.description}
        except Exception as exc:
            _raise_http(exc)

    @app.get("/api/assistants/{assistant_id}")
    async def get_assistant(assistant_id: str):
        try:
            item = runtime.assistants.get(assistant_id)
            return {
                "id": item.id,
                "name": item.name,
                "description": item.description,
                "persona": item.persona,
            }
        except Exception as exc:
            _raise_http(exc)

    @app.patch("/api/assistants/{assistant_id}")
    async def update_assistant(assistant_id: str, body: AssistantUpdate):
        fields = body.model_dump(exclude_unset=True)
        try:
            item = await asyncio.to_thread(
                runtime.assistants.update,
                assistant_id,
                name=fields.get("name"),
                description=fields.get("description"),
                persona=fields.get("persona"),
            )
            return {
                "id": item.id,
                "name": item.name,
                "description": item.description,
                "persona": item.persona,
            }
        except Exception as exc:
            _raise_http(exc)

    @app.delete("/api/assistants/{assistant_id}")
    async def delete_assistant(assistant_id: str, purge: bool = Query(False)):
        try:
            archived = await asyncio.to_thread(
                runtime.assistants.delete, assistant_id, purge
            )
            return {"archived_path": str(archived), "purged": purge}
        except Exception as exc:
            _raise_http(exc)

    @app.post("/api/tasks", status_code=status.HTTP_202_ACCEPTED)
    async def start_task(body: AgentTaskRequest):
        try:
            task_id = reserve_task_submission(body.assistant_id)
        except Exception as exc:
            _raise_http(exc)

        async def operation():
            try:
                result = await runtime.run(
                    body.assistant_id, body.task, body.session_id,
                    lock_task_id=task_id, lock_already_held=True,
                )
                return dict(result)
            finally:
                runtime.store.release_lock(body.assistant_id, task_id)

        return {"task_id": start_reserved_task(body.assistant_id, task_id, operation)}

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
            project = await anyio.to_thread.run_sync(
                runtime.store.create_project, body.assistant_id, body.name
            )
            return asdict(project)
        except Exception as exc:
            _raise_http(exc)

    @app.patch("/api/projects/{project_id}")
    async def rename_project(project_id: str, body: ProjectRename):
        try:
            project = await anyio.to_thread.run_sync(
                runtime.store.rename_project, body.assistant_id, project_id, body.name
            )
            return asdict(project)
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
                await anyio.to_thread.run_sync(
                    runtime.store.purge_project, assistant_id, project_id
                )
                return {"purged": True}
            archived = await anyio.to_thread.run_sync(
                runtime.store.archive_project, assistant_id, project_id
            )
            return {"archived_path": str(archived)}
        except Exception as exc:
            _raise_http(exc)

    @app.post("/api/projects/import-file", status_code=status.HTTP_201_CREATED)
    async def import_file(assistant_id: str = Form(...), file: UploadFile = File(...)):
        try:
            runtime.assistants.get(assistant_id)
            project = await anyio.to_thread.run_sync(lambda: runtime.store.import_text_project(
                assistant_id, file.filename or "", file.file,
                max_files=settings.project_import_max_files,
                max_total_bytes=settings.project_import_max_total_mb * 1024 * 1024,
                max_file_bytes=settings.project_import_max_file_mb * 1024 * 1024,
            ))
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
            project = await anyio.to_thread.run_sync(lambda: runtime.store.import_folder_project(
                assistant_id,
                name,
                [(relative, upload.file) for relative, upload in zip(paths, files, strict=True)],
                max_files=settings.project_import_max_files,
                max_total_bytes=settings.project_import_max_total_mb * 1024 * 1024,
                max_file_bytes=settings.project_import_max_file_mb * 1024 * 1024,
            ))
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
            document, staled = await anyio.to_thread.run_sync(lambda: runtime.store.save_document(
                body.assistant_id, project_id, document_id, body.content,
                expected_version=body.document_version,
            ))
            return {**asdict(document), "staled_change_set_ids": staled}
        except Exception as exc:
            _raise_http(exc)

    @app.patch("/api/projects/{project_id}/documents/{document_id}")
    async def rename_document(project_id: str, document_id: str, body: DocumentRename):
        try:
            return asdict(await anyio.to_thread.run_sync(lambda: runtime.store.rename_document(
                body.assistant_id, project_id, document_id, body.relative_path
            )))
        except Exception as exc:
            _raise_http(exc)

    @app.delete("/api/projects/{project_id}/documents/{document_id}")
    async def delete_document(
        project_id: str,
        document_id: str,
        assistant_id: str = Query(...),
    ):
        try:
            return await anyio.to_thread.run_sync(
                runtime.store.delete_document, assistant_id, project_id, document_id
            )
        except Exception as exc:
            _raise_http(exc)

    @app.post(
        "/api/projects/{project_id}/documents/{document_id}/selection-rewrites",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def selection_rewrite(project_id: str, document_id: str, body: SelectionRewriteRequest):
        try:
            task_id = reserve_task_submission(body.assistant_id)
        except Exception as exc:
            _raise_http(exc)

        async def operation():
            try:
                change = await runtime.rewrite_selection(
                    body.assistant_id, project_id, document_id,
                    start=body.start, end=body.end, selected_text=body.selected_text,
                    instruction=body.instruction, document_version=body.document_version,
                    lock_task_id=task_id, lock_already_held=True,
                )
                return {"change_set_id": change.change_set_id}
            finally:
                runtime.store.release_lock(body.assistant_id, task_id)

        return {"task_id": start_reserved_task(body.assistant_id, task_id, operation)}

    @app.post(
        "/api/projects/{project_id}/change-sets/{change_set_id}/hunks/{hunk_id}/accept"
    )
    async def accept_hunk(project_id: str, change_set_id: str, hunk_id: str, body: ChangeSetHunkAction):
        try:
            document, change, hunk, staled = await anyio.to_thread.run_sync(
                runtime.store.accept_change_hunk,
                body.assistant_id, project_id, change_set_id, hunk_id,
            )
            return {
                "document": asdict(document),
                "change_set": asdict(change),
                "hunk": asdict(hunk),
                "staled_change_set_ids": staled,
            }
        except Exception as exc:
            _raise_http(exc)

    @app.post(
        "/api/projects/{project_id}/change-sets/{change_set_id}/hunks/{hunk_id}/reject"
    )
    async def reject_hunk(project_id: str, change_set_id: str, hunk_id: str, body: ChangeSetHunkAction):
        try:
            change = await anyio.to_thread.run_sync(
                runtime.store.reject_change_hunk,
                body.assistant_id, project_id, change_set_id, hunk_id,
            )
            return {"change_set": asdict(change)}
        except Exception as exc:
            _raise_http(exc)

    @app.post("/api/projects/{project_id}/change-sets/{change_set_id}/accept-all")
    async def accept_all_hunks(project_id: str, change_set_id: str, body: ChangeSetHunkAction):
        try:
            result = await anyio.to_thread.run_sync(
                runtime.store.accept_all_change_hunks,
                body.assistant_id, project_id, change_set_id,
            )
            return {
                "document": asdict(result["document"]),
                "change_set": asdict(result["change_set"]),
                "applied_hunk_ids": result["applied_hunk_ids"],
                "stopped": result["stopped"],
                "staled_change_set_ids": result["staled_change_set_ids"],
            }
        except Exception as exc:
            _raise_http(exc)

    @app.get("/api/projects/{project_id}/change-sets")
    async def list_change_sets(
        project_id: str,
        assistant_id: str = Query(...),
        document_id: str = Query(...),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
    ):
        try:
            result = runtime.store.list_change_sets_for_document(
                assistant_id, project_id, document_id, page=page, page_size=page_size
            )
            return {
                "items": [_change_preview(item) for item in result["items"]],
                "total": result["total"],
                "page": result["page"],
                "page_size": result["page_size"],
            }
        except Exception as exc:
            _raise_http(exc)

    @app.post("/api/projects/{project_id}/agent/messages", status_code=status.HTTP_202_ACCEPTED)
    async def project_chat(project_id: str, body: ProjectChatRequest):
        task_id: str | None = None
        created_chat_session = False
        chat_session_id: str | None = None
        try:
            if not body.message.strip():
                raise ValueError("消息不能为空")
            task_id = reserve_task_submission(body.assistant_id)
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
                runtime.store.get_project_chat_session(
                    body.assistant_id, project_id, body.chat_session_id
                )
                chat_session_id = body.chat_session_id
        except Exception as exc:
            if task_id is not None:
                runtime.store.release_lock(body.assistant_id, task_id)
            if created_chat_session and chat_session_id is not None:
                try:
                    runtime.store.delete_empty_project_chat_session(
                        body.assistant_id, project_id, chat_session_id
                    )
                except Exception:
                    logger.warning("清理未受理的空项目聊天会话失败", exc_info=True)
            _raise_http(exc)

        async def operation():
            if chat_session_id is None or task_id is None:
                raise RuntimeError("任务占位或聊天会话未初始化")
            try:
                result = await runtime.chat_project(
                    body.assistant_id, project_id, body.message,
                    chat_session_id=chat_session_id,
                    current_document_id=body.current_document_id,
                    lock_task_id=task_id,
                    lock_already_held=True,
                )
                return {
                    "reply": result.reply,
                    "change_set_ids": [item.change_set_id for item in result.changes],
                }
            finally:
                runtime.store.release_lock(body.assistant_id, task_id)
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
            "task_id": start_reserved_task(body.assistant_id, task_id, operation),
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
            work_events = runtime.store.list_project_chat_work_events(
                assistant_id, project_id, chat_session_id
            )
            return {
                "session": asdict(session),
                "messages": [asdict(item) for item in messages],
                "pending_changes": [_change_preview(item) for item in pending],
                "work_events": [asdict(item) for item in work_events],
            }
        except Exception as exc:
            _raise_http(exc)

    @app.post(
        "/api/projects/{project_id}/agent/sessions/{chat_session_id}/reconcile"
    )
    async def reconcile_project_chat_session(
        project_id: str,
        chat_session_id: str,
        assistant_id: str = Query(...),
    ):
        try:
            runtime.store.get_project_chat_session(
                assistant_id, project_id, chat_session_id
            )
            live_lock_task_id = runtime.store.current_lock_task_id(assistant_id)
            reconciled: list[str] = []
            for work_task_id in runtime.store.list_unfinished_project_chat_work_task_ids(
                assistant_id, project_id, chat_session_id
            ):
                if broker.is_active(work_task_id, assistant_id):
                    continue
                if work_task_id == live_lock_task_id:
                    continue
                await anyio.to_thread.run_sync(
                    runtime.store.interrupt_project_chat_work_task,
                    assistant_id, project_id, chat_session_id, work_task_id,
                )
                reconciled.append(work_task_id)
            return {"reconciled_task_ids": reconciled}
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
    async def task_stream(
        task_id: str,
        assistant_id: str = Query(...),
        after_seq: int | None = Query(None),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ):
        try:
            broker.get(task_id, assistant_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        cursor = after_seq
        if cursor is None and last_event_id is not None:
            try:
                cursor = int(last_event_id)
            except ValueError:
                cursor = None  # 非法头按全新订阅处理，不猜游标
        return StreamingResponse(
            broker.stream(task_id, assistant_id, after_seq=cursor),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    dist = settings.project_root / "web" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    return app
