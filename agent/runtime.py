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
from . import chat_memory
from .context import (
    build_chat_context,
    clip_content_to_token_budget,
    clip_document_content,
    estimate_tokens,
)
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
            timeout=settings.llm_timeout_seconds,
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

    async def run(
        self, assistant_id: str, task: str, session_id: str | None = None, *,
        lock_task_id: str | None = None, lock_already_held: bool = False,
    ) -> AgentState:
        if not self.settings.openai_api_key:
            raise RuntimeError("未配置 OPENAI_API_KEY：请复制 .env.example 为 .env 并填写后重试")

        assistant = self.assistants.get(assistant_id)  # KeyError 内含可用助手列表
        if session_id is not None:
            self.store.validate_session_owner(assistant_id, session_id)
        session_id = session_id or uuid.uuid4().hex[:12]
        task_id = lock_task_id or uuid.uuid4().hex[:12]

        # 跨进程运行锁（架构 §4.6）：冲突抛 AssistantBusyError
        if not lock_already_held:
            self.store.acquire_lock(assistant_id, task_id)
        try:
            self.store.create_session(assistant_id, session_id, task)
            trace = self.store.recall_trace(assistant_id, task)
            memory_context = trace.text
            self.store.add_message(assistant_id, session_id, "user", task)
            self.bus.emit("info", text=f"已注入助手记忆：{self._recall_summary_text(trace)}")

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
            if final.get("status") == "failed":
                raise RuntimeError(final.get("finish_note") or "任务失败")
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
        lock_task_id: str | None = None,
        lock_already_held: bool = False,
    ):
        """生成待确认的选区 change set，不直接修改项目文件。"""
        if not self.settings.openai_api_key:
            raise RuntimeError("未配置 OPENAI_API_KEY：请复制 .env.example 为 .env 并填写后重试")
        assistant = self.assistants.get(assistant_id)
        task_id = lock_task_id or uuid.uuid4().hex[:12]
        session_id = uuid.uuid4().hex[:12]
        if not lock_already_held:
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
                chat_session_id=None,
            )
            return change
        except Exception:
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

    @staticmethod
    def _recall_summary_text(trace) -> str:
        """recall 命中摘要（v1.30）：画像条数、文章命中、对话片段与分路降级标记。"""
        summary = (
            f"画像 {trace.profile_entries} 条；文章命中 {len(trace.article_hits)} 篇；"
            f"对话片段 {len(trace.message_hits)} 段"
        )
        if trace.degraded:
            summary += f"（部分降级：{'、'.join(trace.degraded)}）"
        return summary

    async def _consolidate_chat_memory(
        self,
        recorder: WorkLogRecorder,
        *,
        assistant_id: str,
        chat_session_id: str,
        user_message: str,
        assistant_reply: str,
    ) -> None:
        """轮次终态选择性沉淀（架构 §5.4 v1.30）。

        确定性门槛 → 显式指令直达 / 一次启发式提取；任何失败降级 warning，
        不得影响本轮已交付的聊天回复（对齐 §3.3 压缩失败降级语义）。
        """
        if not self.settings.chat_memory_consolidation:
            return
        try:
            if not chat_memory.has_consolidation_signal(user_message):
                return
            direct = chat_memory.extract_explicit_command(user_message)
            if direct is not None:
                self.store.memorize(
                    assistant_id, "preference", direct, session_id=chat_session_id
                )
                work = recorder.start("progress", "已沉淀助手记忆")
                recorder.done(work, detail=f"- [偏好] {direct}")
                return
            items = await chat_memory.extract_preferences(
                self.llm,
                self.settings.model_name,
                user_message=user_message,
                assistant_reply=assistant_reply,
                profile_text=self.store.get_assistant_profile(assistant_id),
                json_mode=self.settings.json_mode,
            )
            if not items:
                return
            lines: list[str] = []
            for kind, content in items:
                self.store.memorize(
                    assistant_id, kind, content, session_id=chat_session_id
                )
                lines.append(f"- [{chat_memory.kind_label(kind)}] {content}")
            work = recorder.start("progress", "已沉淀助手记忆")
            recorder.done(work, detail="\n".join(lines))
        except Exception:
            logger.warning(
                "聊天记忆沉淀失败（assistant=%s session=%s）",
                assistant_id, chat_session_id, exc_info=True,
            )
            try:
                recorder.note("warning", "本轮记忆沉淀失败，已跳过（不影响回复）")
            except Exception:
                logger.debug("沉淀降级警告工作项写入失败", exc_info=True)

    async def chat_project(
        self,
        assistant_id: str,
        project_id: str,
        message: str,
        *,
        chat_session_id: str,
        current_document_id: str | None = None,
        lock_task_id: str | None = None,
        lock_already_held: bool = False,
    ) -> ProjectChatResult:
        """项目 Agent 对话；文件修改只生成待确认 change set。"""
        if not self.settings.openai_api_key:
            raise RuntimeError("未配置 OPENAI_API_KEY：请复制 .env.example 为 .env 并填写后重试")
        assistant = self.assistants.get(assistant_id)
        lock_task_id = lock_task_id or uuid.uuid4().hex[:12]
        if not lock_already_held:
            self.store.acquire_lock(assistant_id, lock_task_id)
        recorder: WorkLogRecorder | None = None
        user_record = None
        assistant_persisted = False
        try:
            if not message.strip():
                raise ValueError("消息不能为空")
            self.store.get_project_tree(assistant_id, project_id)
            self.store.get_project_chat_session(
                assistant_id, project_id, chat_session_id
            )
            context = "(未打开文档)"
            document_content = ""
            document_prefix = ""
            document_clipped = False
            if current_document_id is not None:
                document = self.store.get_document(
                    assistant_id, project_id, current_document_id
                )
                body, clipped = clip_document_content(
                    document.content or "", self.settings.chat_context_doc_max_chars
                )
                document_prefix = (
                    f"document_id={document.document_id}\n"
                    f"document_version={document.version}\n"
                    f"relative_path={document.relative_path}\n"
                )
                document_content = body
                document_clipped = clipped
            editing = self.skills.get("editing")
            skill_prompt = editing.body if editing is not None else "帮助用户审校和改写项目文本。"
            # 聊天注入本助手记忆（v1.30，§4.7 既有声明补齐实现）：在编辑指导之前注入
            # recall 结果；注入内容属于 system prompt，参与既有 token 预算与兜底计算。
            memory_trace = self.store.recall_trace(assistant_id, message)
            memory_block = (
                f"本助手长期记忆（跨会话共享，供参考）：\n{memory_trace.text}\n\n"
                if memory_trace.text
                else ""
            )
            system_prefix = (
                f"{assistant.persona}\n\n{skill_prompt}\n\n{memory_block}"
                "你正在项目 Agent 面板中回答用户。回答应简洁、具体。"
                "用户要求改写、增删或替换正文时，必须调用 propose_project_edits，"
                "不要声称已经修改文件，也不要在工具调用前输出解释。"
                "同一文档的多处修改必须放进同一次调用的 hunks 列表，不要分多次调用。"
                "普通问答不调用工具。\n\n"
                "当前项目文档：\n"
            )
            if current_document_id is not None:
                content_label = "content（已按上下文预算截断）:\n" if document_clipped else "content:\n"
                fixed_system = (
                    f"{system_prefix}{document_prefix}content（已按上下文预算截断）:\n"
                )
                max_document_tokens = max(
                    self.settings.chat_context_token_budget
                    - estimate_tokens(fixed_system)
                    - 5,  # 至少给一条历史消息保留 role 开销与一个 token。
                    0,
                ) if self.settings.chat_context_token_budget > 0 else estimate_tokens(document_content)
                document_content, token_clipped = clip_content_to_token_budget(
                    document_content, max_document_tokens
                )
                if token_clipped and not document_clipped:
                    content_label = "content（已按上下文预算截断）:\n"
                context = f"{document_prefix}{content_label}{document_content}"
            system_prompt = f"{system_prefix}{context}"
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
            if memory_trace.text or memory_trace.degraded:
                injection_work = recorder.start("progress", "已注入助手记忆")
                recorder.done(injection_work, detail=self._recall_summary_text(memory_trace))
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
                total_timeout_seconds=self.settings.llm_stream_timeout_seconds,
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
                assistant_persisted = True
                await self._consolidate_chat_memory(
                    recorder,
                    assistant_id=assistant_id,
                    chat_session_id=chat_session_id,
                    user_message=message,
                    assistant_reply=reply,
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
                    chat_session_id=chat_session_id,
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
                total_timeout_seconds=self.settings.llm_stream_timeout_seconds,
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
            assistant_persisted = True
            await self._consolidate_chat_memory(
                recorder,
                assistant_id=assistant_id,
                chat_session_id=chat_session_id,
                user_message=message,
                assistant_reply=reply,
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
            if user_record is not None and not assistant_persisted:
                try:
                    self.store.add_project_chat_message(
                        assistant_id, project_id, chat_session_id,
                        "assistant", "[interrupted] 本轮任务已取消。",
                    )
                except Exception:
                    logger.warning("写入取消轮次占位失败", exc_info=True)
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
            if user_record is not None and not assistant_persisted:
                try:
                    self.store.add_project_chat_message(
                        assistant_id, project_id, chat_session_id,
                        "assistant", "[interrupted] 本轮处理失败，请重试。",
                    )
                except Exception:
                    logger.warning("写入失败轮次占位失败", exc_info=True)
            raise
        finally:
            self.store.release_lock(assistant_id, lock_task_id)
