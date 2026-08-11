"""Runtime（架构 §5.4）：进程启动时组装一次，任务按 runtime.run(assistant_id, task) 运行。"""
from __future__ import annotations

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
from .events import EventBus
from .executor import ToolRegistry
from .llm import chat_text
from .loop import RuntimeServices, build_graph
from .project_editing import ProjectChatResult, parse_chat_payload
from .schemas import AgentState
from .skills import load_skills
from .tools import make_builtin_tools


class AgentRuntime:
    def __init__(self, settings: Settings, bus: EventBus | None = None) -> None:
        self.settings = settings
        self.bus = bus or EventBus()
        self.store = MemoryStore(settings.data_dir)
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
        self.tools.register_all(self.mcp.tools)
        builtin_count = len(self.tools.list()) - len(self.mcp.tools)
        self.bus.emit(
            "info",
            text=f"工具表就绪：内置 {builtin_count} + MCP {len(self.mcp.tools)}"
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
        self.store.acquire_lock(assistant_id, task_id, self.settings.run_lock_ttl_hours)
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
        self.store.acquire_lock(assistant_id, task_id, self.settings.run_lock_ttl_hours)
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
            change = self.store.create_change_set(
                assistant_id,
                project_id,
                document_id,
                source="selection",
                start=start,
                end=end,
                original_text=selected_text,
                replacement_text=replacement,
                base_version=document_version,
                session_id=session_id,
            )
            self.bus.emit(
                "change_preview",
                change_set_id=change.change_set_id,
                project_id=project_id,
                document_id=document_id,
                range={"from": start, "to": end},
                original=selected_text,
                replacement=replacement,
                document_version=document_version,
                source="selection",
            )
            return change
        except Exception as exc:
            self.bus.emit("failed", reason=str(exc))
            raise
        finally:
            self.store.release_lock(assistant_id, task_id)

    async def chat_project(
        self,
        assistant_id: str,
        project_id: str,
        message: str,
        *,
        current_document_id: str | None = None,
    ) -> ProjectChatResult:
        """项目 Agent 对话；文件修改只生成待确认 change set。"""
        if not self.settings.openai_api_key:
            raise RuntimeError("未配置 OPENAI_API_KEY：请复制 .env.example 为 .env 并填写后重试")
        assistant = self.assistants.get(assistant_id)
        task_id = uuid.uuid4().hex[:12]
        session_id = uuid.uuid4().hex[:12]
        self.store.acquire_lock(assistant_id, task_id, self.settings.run_lock_ttl_hours)
        try:
            if not message.strip():
                raise ValueError("消息不能为空")
            context = "(未打开文档)"
            if current_document_id is not None:
                document = self.store.get_document(
                    assistant_id, project_id, current_document_id
                )
                context = (
                    f"document_id={document.document_id}\n"
                    f"document_version={document.version}\n"
                    f"relative_path={document.relative_path}\n"
                    f"content:\n{document.content or ''}"
                )
            editing = self.skills.get("editing")
            skill_prompt = editing.body if editing is not None else "帮助用户审校和改写项目文本。"
            prompt = (
                f"{skill_prompt}\n\n"
                "返回严格 JSON："
                '{"reply":"给用户的回答","changes":[{"document_id":"...",'
                '"start":0,"end":1,"original_text":"...","replacement_text":"...",'
                '"document_version":1}]}。不修改文件时 changes 为空。\n\n'
                f"当前项目文档：\n{context}\n\n用户消息：{message.strip()}"
            )
            raw = await chat_text(
                self.llm,
                self.settings.model_name,
                [
                    {"role": "system", "content": assistant.persona},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                json_mode=True,
            )
            payload = parse_chat_payload(raw)
            changes = self.store.create_change_sets(
                assistant_id,
                project_id,
                [
                    {
                        "document_id": item.document_id,
                        "start": item.start,
                        "end": item.end,
                        "original_text": item.original_text,
                        "replacement_text": item.replacement_text,
                        "base_version": item.document_version,
                    }
                    for item in payload.changes
                ],
                source="chat",
                session_id=session_id,
            )
            for change in changes:
                self.bus.emit(
                    "change_preview",
                    change_set_id=change.change_set_id,
                    project_id=project_id,
                    document_id=change.document_id,
                    range={"from": change.start, "to": change.end},
                    original=change.original_text,
                    replacement=change.replacement_text,
                    document_version=change.base_version,
                    source="chat",
                )
            if payload.reply:
                self.bus.emit("token", text=payload.reply)
            return ProjectChatResult(reply=payload.reply, changes=changes)
        except Exception as exc:
            self.bus.emit("failed", reason=str(exc))
            raise
        finally:
            self.store.release_lock(assistant_id, task_id)
