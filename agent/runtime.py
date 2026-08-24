"""Runtime（架构 §5.4）：进程启动时组装一次，任务按 runtime.run(assistant_id, task) 运行。"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from openai import AsyncOpenAI

from config.settings import Settings
from mcp_client import MCPManager, load_server_configs
from memory.store import MemoryStore
from scheduler import RuntimeScheduler

from .assistant_registry import AssistantRegistry
from .context import build_chat_context, clip_document_content, estimate_tokens
from .events import EventBus, current_task_id
from .executor import ToolRegistry
from .llm import chat_text, stream_chat_turn
from .loop import RuntimeServices, build_graph
from .project_editing import ProjectChatResult, ProjectEditBatch, hunk_count
from .schemas import AgentState, ToolContext
from .skills import load_skills
from .tools import make_builtin_tools, make_project_edit_tool
from .work_log import WorkLogRecorder

logger = logging.getLogger(__name__)


class AgentRuntime:
    def __init__(self, settings: Settings, bus: EventBus | None = None) -> None:
        self.settings = settings
        self.bus = bus or EventBus()
        self.store = MemoryStore(
            settings.data_dir,
            run_lock_ttl_hours=settings.run_lock_ttl_hours,
        )
        self.assistants = AssistantRegistry(settings.data_dir, self.store)
        for warning in self.assistants.warnings:
            self.bus.emit("warning", text=warning)
        self.skills = load_skills(settings.skills_dir)
        self.tools = ToolRegistry()
        self.tools.register_all(make_builtin_tools(settings.data_dir, self.store))
        self.mcp: MCPManager | None = None
        self.scheduler: RuntimeScheduler | None = None
        self.llm = AsyncOpenAI(
            api_key=settings.openai_api_key or "missing",
            base_url=settings.openai_base_url,
        )

    async def start(self, *, enable_scheduler: bool = False) -> None:
        """连接 MCP servers 并把发现的工具纳入统一工具表；失败不阻断启动。"""
        warn = lambda msg: self.bus.emit("warning", text=msg)  # noqa: E731
        configs = load_server_configs(self.settings.mcp_config, self.settings.project_root, warn=warn)
        self.mcp = MCPManager(configs)
        await self.mcp.start(warn=warn)
        builtin_count = len(self.tools.list())
        mcp_count = 0
        for spec in self.mcp.tools:
            if self.tools.register(spec):
                mcp_count += 1
                continue
            registered = self.tools.get(spec.name)
            warn(
                f"MCP 工具 {spec.name} 与已注册工具同名，已跳过"
                f"（保留 {registered.source if registered else '既有实现'}）"
            )
        self.bus.emit(
            "info",
            text=f"工具表就绪：内置 {builtin_count} + MCP {mcp_count}"
                 + (f"（失败 server：{', '.join(self.mcp.failed_servers)}）" if self.mcp.failed_servers else ""),
        )
        if enable_scheduler:
            self.scheduler = RuntimeScheduler(self, self.settings.jobs)
            self.scheduler.start()
            self.bus.emit("info", text=f"Scheduler 已启动：注册 {len(self.scheduler.get_jobs())} 个 job")

    async def close(self) -> None:
        if self.scheduler is not None:
            await self.scheduler.shutdown()
            self.scheduler = None
        if self.mcp is not None:
            await self.mcp.close()
        self.store.close()

    async def run(self, assistant_id: str, task: str, session_id: str | None = None) -> AgentState:
        if not self.settings.openai_api_key:
            raise RuntimeError("未配置 OPENAI_API_KEY：请复制 .env.example 为 .env 并填写后重试")

        assistant = self.assistants.get(assistant_id)  # KeyError 内含可用助手列表
        session_id = session_id or uuid.uuid4().hex[:12]
        task_id = uuid.uuid4().hex[:12]

        # 跨进程运行锁（架构 §4.6）：冲突抛 AssistantBusyError
        self.store.acquire_lock(assistant_id, task_id)
        try:
            self.store.create_session(assistant_id, session_id, task)
            memory_context = self.store.recall(assistant_id, task)
            self.store.add_message(assistant_id, session_id, "user", task)

            services = RuntimeServices(
                llm=self.llm,
                model=self.settings.model_name,
                assistant=assistant,
                tools=self.tools,
                skills=self.skills,
                store=self.store,
                bus=self.bus,
                settings=self.settings,
            )
            initial: AgentState = {
                "assistant_id": assistant_id,
                "task": task,
                "session_id": session_id,
                "memory_context": memory_context,
                "observations": [],
                "active_skills": [],
                "skill_prompts": [],
                "step": 0,
                "reflect_fails": 0,
                "quality_passed": False,
                "status": "running",
            }
            self.bus.emit("info", text=f"助手「{assistant.name}」开始任务（session {session_id}）")

            db_path = str(self.settings.data_dir / "checkpoints.db")
            async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
                await saver.setup()
                graph = build_graph(services).compile(checkpointer=saver)
                final = await graph.ainvoke(
                    initial,
                    config={
                        "configurable": {"thread_id": f"{assistant_id}:{session_id}"},
                        "recursion_limit": self.settings.max_steps * 6 + 20,
                    },
                )
            return final
        finally:
            self.store.release_lock(assistant_id, task_id)  # 只释放自己持有的锁（审查 P1-3）

    async def rewrite_selection(
        self,
        assistant_id: str,
        project_id: str,
        document_id: str,
        *,
        start: int,
        end: int,
        selected_text: str,
        instruction: str,
        document_version: int,
    ):
        """生成待确认的选区 change set，不直接修改项目文件。"""
        if not self.settings.openai_api_key:
            raise RuntimeError("未配置 OPENAI_API_KEY：请复制 .env.example 为 .env 并填写后重试")
        assistant = self.assistants.get(assistant_id)
        task_id = uuid.uuid4().hex[:12]
        session_id = uuid.uuid4().hex[:12]
        self.store.acquire_lock(assistant_id, task_id)
        try:
            document = self.store.get_document(assistant_id, project_id, document_id)
            if document.version != document_version:
                raise RuntimeError("版本冲突")
            if document.content is None or start < 0 or end < start or end > len(document.content):
                raise RuntimeError("选区范围非法")
            if document.content[start:end] != selected_text:
                raise RuntimeError("选区文本与当前文档不匹配")
            if not instruction.strip():
                raise ValueError("改写指令不能为空")
            editing = self.skills.get("editing")
            if assistant.skills is not None and "editing" not in assistant.skills:
                raise RuntimeError(f"助手 {assistant_id} 未启用 editing Skill")
            skill_prompt = editing.body if editing is not None else "只改写用户选中的文本，保持事实和 Markdown 结构。"
            prompt = (
                f"{skill_prompt}\n\n"
                "你正在执行局部改写。只返回替换文本，不要解释，不要 Markdown 围栏。\n"
                f"用户指令：{instruction.strip()}\n"
                f"选中文本：\n{selected_text}"
            )
            replacement = (await chat_text(
                self.llm,
                self.settings.model_name,
                [
                    {"role": "system", "content": assistant.persona},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                json_mode=False,
            )).strip()
            if not replacement:
                raise RuntimeError("AI 改写结果为空")
            change = self.store.create_selection_change_set(
                assistant_id,
                project_id,
                document_id,
                task_id=current_task_id() or task_id,
                start=start,
                end=end,
                original_text=selected_text,
                replacement_text=replacement,
                base_version=document_version,
                source="selection",
                session_id=session_id,
            )
            hunk = change.hunks[0]
            self.bus.emit(
                "change_preview",
                change_set_id=change.change_set_id,
                project_id=project_id,
                document_id=document_id,
                hunks=[{
                    "hunk_id": hunk.hunk_id,
                    "range": {"from": hunk.start, "to": hunk.end},
                    "original": hunk.original_text,
                    "replacement": hunk.new_text,
                    "status": hunk.status,
                }],
                document_version=document_version,
                source="selection",
            )
            return change
        except Exception as exc:
            self.bus.emit("failed", reason=str(exc))
            raise
        finally:
            self.store.release_lock(assistant_id, task_id)

    async def _summarize_chat_history(self, transcript: str) -> str:
        """压缩项目聊天的早期历史；不发 token 事件，不污染可见回复（架构 §3.3）。"""
        return await chat_text(
            self.llm,
            self.settings.model_name,
            [
                {
                    "role": "system",
                    "content": "你在压缩一段写作助手与用户的对话历史，供后续轮次做背景参考。",
                },
                {
                    "role": "user",
                    "content": (
                        "用中文把下面的对话压缩成一段不超过 400 字的摘要。"
                        "保留用户的写作目标、已确认的风格与结构决定、已完成和待办的修改，"
                        "以及任何具体的事实、数字与命名约定；丢弃寒暄和重复内容。"
                        "只输出摘要正文。\n\n"
                        f"{transcript}"
                    ),
                },
            ],
            temperature=0.2,
            json_mode=False,
        )

    async def chat_project(
        self,
        assistant_id: str,
        project_id: str,
        message: str,
        *,
        chat_session_id: str,
        current_document_id: str | None = None,
    ) -> ProjectChatResult:
        """项目 Agent 对话；文件修改只生成待确认 change set。"""
        if not self.settings.openai_api_key:
            raise RuntimeError("未配置 OPENAI_API_KEY：请复制 .env.example 为 .env 并填写后重试")
        assistant = self.assistants.get(assistant_id)
        lock_task_id = uuid.uuid4().hex[:12]
        self.store.acquire_lock(assistant_id, lock_task_id)
        recorder: WorkLogRecorder | None = None
        try:
            if not message.strip():
                raise ValueError("消息不能为空")
            self.store.get_project_tree(assistant_id, project_id)
            self.store.get_project_chat_session(
                assistant_id, project_id, chat_session_id
            )
            context = "(未打开文档)"
            if current_document_id is not None:
                document = self.store.get_document(
                    assistant_id, project_id, current_document_id
                )
                body, clipped = clip_document_content(
                    document.content or "", self.settings.chat_context_doc_max_chars
                )
                context = (
                    f"document_id={document.document_id}\n"
                    f"document_version={document.version}\n"
                    f"relative_path={document.relative_path}\n"
                    + ("content（已按上下文预算截断）:\n" if clipped else "content:\n")
                    + body
                )
            editing = self.skills.get("editing")
            skill_prompt = editing.body if editing is not None else "帮助用户审校和改写项目文本。"
            system_prompt = (
                f"{assistant.persona}\n\n{skill_prompt}\n\n"
                "你正在项目 Agent 面板中回答用户。回答应简洁、具体。"
                "用户要求改写、增删或替换正文时，必须调用 propose_project_edits，"
                "不要声称已经修改文件，也不要在工具调用前输出解释。"
                "同一文档的多处修改必须放进同一次调用的 hunks 列表，不要分多次调用。"
                "普通问答不调用工具。\n\n"
                f"当前项目文档：\n{context}"
            )
            user_record = self.store.add_project_chat_message(
                assistant_id,
                project_id,
                chat_session_id,
                "user",
                message,
            )
            recorder = WorkLogRecorder(
                self.store,
                self.bus,
                assistant_id=assistant_id,
                project_id=project_id,
                chat_session_id=chat_session_id,
                task_id=current_task_id() or lock_task_id,
                user_message_id=user_record.message_id,
            )
            context_work = recorder.start("progress", "正在读取当前文档与历史上下文")
            history = self.store.list_project_chat_messages(
                assistant_id, project_id, chat_session_id
            )
            existing = self.store.get_project_chat_summary(
                assistant_id, project_id, chat_session_id
            )
            chat_context = await build_chat_context(
                history,
                system_tokens=estimate_tokens(system_prompt),
                token_budget=self.settings.chat_context_token_budget,
                keep_recent=self.settings.chat_context_keep_recent,
                existing_summary=existing.summary if existing else None,
                existing_summary_through=existing.covered_through_message_id if existing else None,
                summarize=self._summarize_chat_history,
            )
            for warning in chat_context.warnings:
                self.bus.emit("warning", text=warning)
                recorder.note("warning", warning)
            if chat_context.summary_changed:
                # 显式校验代替 assert：python -O 剥离断言后不应把类型收窄交给运行时崩溃。
                if (
                    chat_context.summary is None
                    or chat_context.summary_through_message_id is None
                ):
                    raise RuntimeError("上下文压缩结果不完整，本轮任务终止")
                self.store.save_project_chat_summary(
                    assistant_id,
                    project_id,
                    chat_session_id,
                    chat_context.summary,
                    chat_context.summary_through_message_id,
                )
                recorder.delta(
                    context_work,
                    f"已把较早的 {chat_context.compacted_message_count} 条对话压缩为摘要以控制上下文",
                )
                self.bus.emit(
                    "info",
                    text=f"已把较早的 {chat_context.compacted_message_count} 条对话压缩为摘要以控制上下文",
                )
            recorder.done(context_work)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                *chat_context.messages,
            ]
            visible: list[str] = []

            def emit_text(text: str) -> None:
                visible.append(text)
                self.bus.emit("token", text=text)

            chat_tools = ToolRegistry()
            chat_tools.register(make_project_edit_tool(self.store, project_id))
            edit_tool = chat_tools.get("propose_project_edits")
            if edit_tool is None:
                raise RuntimeError("项目编辑工具注册失败")
            tool_schema = [{
                "type": "function",
                "function": {
                    "name": edit_tool.name,
                    "description": edit_tool.description,
                    "parameters": edit_tool.args_schema,
                },
            }]
            first = await stream_chat_turn(
                self.llm,
                self.settings.model_name,
                messages,
                tools=tool_schema,
                on_text=emit_text,
            )
            if not first.tool_calls:
                reply = "".join(visible)
                if not reply.strip():
                    reply = "模型未返回可见内容，请重试。"
                    emit_text(reply)
                self.store.add_project_chat_message(
                    assistant_id,
                    project_id,
                    chat_session_id,
                    "assistant",
                    reply,
                )
                recorder.finish_task(
                    "succeeded", title=message.strip()[:80], detail="无工具调用"
                )
                return ProjectChatResult(reply=reply, changes=[])
            if len(first.tool_calls) != 1:
                raise RuntimeError("项目聊天每轮只允许一个编辑提案工具调用")
            call = first.tool_calls[0]
            if call.name != edit_tool.name:
                raise RuntimeError(f"项目聊天不允许调用工具：{call.name}")
            ctx = ToolContext(
                assistant_id=assistant_id,
                session_id=chat_session_id,
                data_dir=str(self.settings.data_dir),
            )
            tool_work = recorder.start(
                "tool",
                "正在准备修改",
                tool_name=call.name,
                args=call.arguments,
            )
            try:
                args = json.loads(call.arguments)
                validated = ProjectEditBatch.model_validate(args)
            except Exception as exc:
                error = "修改建议参数无效，请重试"
                recorder.done(tool_work, status="failed", detail=error)
                self.bus.emit("tool_result", tool=call.name, ok=False, error=error)
                raise ValueError(error) from exc
            self.bus.emit(
                "tool_call",
                tool=call.name,
                args={
                    "documents": len(validated.documents),
                    "hunks": hunk_count(validated),
                },
            )
            try:
                output = await edit_tool.call(args, ctx)
            except Exception as exc:
                recorder.done(tool_work, status="failed", detail=str(exc))
                self.bus.emit("tool_result", tool=call.name, ok=False, error=str(exc))
                raise
            recorder.done(tool_work, result=output)
            result_data = json.loads(output)
            changes = [
                self.store.get_change_set(assistant_id, project_id, change_set_id)
                for change_set_id in result_data["change_set_ids"]
            ]
            total_hunks = sum(len(change.hunks) for change in changes)
            self.bus.emit(
                "tool_result",
                tool=call.name,
                ok=True,
                summary=f"已生成 {total_hunks} 处修改建议",
            )
            for change in changes:
                document = self.store.get_document(
                    assistant_id, project_id, change.document_id
                )
                work = recorder.start(
                    "changes",
                    f"为 {document.relative_path} 生成 {len(change.hunks)} 处修改建议",
                    change_set_id=change.change_set_id,
                    document_id=change.document_id,
                )
                recorder.done(
                    work, detail=f"基于正文版本 {change.base_version}，逐处确认后写入"
                )
                self.bus.emit(
                    "change_preview",
                    change_set_id=change.change_set_id,
                    project_id=project_id,
                    document_id=change.document_id,
                    hunks=[{
                        "hunk_id": hunk.hunk_id,
                        "range": {"from": hunk.start, "to": hunk.end},
                        "original": hunk.original_text,
                        "replacement": hunk.new_text,
                        "status": hunk.status,
                    } for hunk in change.hunks],
                    document_version=change.base_version,
                    source="chat",
                )
            messages.extend([
                {
                    "role": "assistant",
                    "content": first.text or None,
                    "tool_calls": [{
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }],
                },
                {"role": "tool", "tool_call_id": call.id, "content": output},
            ])
            followup_started = False

            def emit_followup(text: str) -> None:
                nonlocal followup_started
                if not followup_started and first.text:
                    emit_text("\n\n")
                followup_started = True
                emit_text(text)

            await stream_chat_turn(
                self.llm,
                self.settings.model_name,
                messages,
                on_text=emit_followup,
            )
            reply = "".join(visible)
            if not reply.strip():
                reply = "修改建议已生成，请审核。"
                emit_text(reply)
            self.store.add_project_chat_message(
                assistant_id,
                project_id,
                chat_session_id,
                "assistant",
                reply,
            )
            recorder.finish_task(
                "succeeded",
                title=message.strip()[:80],
                detail=(
                    f"工具 1 次；修改建议 {sum(len(item.hunks) for item in changes)} 条"
                    if changes else "无工具调用"
                ),
            )
            return ProjectChatResult(reply=reply, changes=changes)
        except asyncio.CancelledError:
            if recorder is not None:
                try:
                    recorder.finish_task("interrupted", title=message.strip()[:80])
                except Exception:
                    logger.warning(
                        "工作记录中断终态写入失败（assistant=%s session=%s）",
                        assistant_id, chat_session_id, exc_info=True,
                    )
            raise
        except Exception as exc:
            if recorder is not None:
                try:
                    recorder.finish_task("failed", title=message.strip()[:80], detail=str(exc))
                except Exception:
                    # 补偿不得覆盖原始任务错误（对齐 v1.16 API 补偿原则）。
                    logger.warning(
                        "工作记录失败终态写入失败（assistant=%s session=%s）",
                        assistant_id, chat_session_id, exc_info=True,
                    )
            self.bus.emit("failed", reason=str(exc))
            raise
        finally:
            self.store.release_lock(assistant_id, lock_task_id)
