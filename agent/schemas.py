"""核心数据结构（架构 §3.1 / §5.1 / §5.2）。"""
from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Any, Awaitable, Callable, Literal, TypedDict

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class ActionPlan(BaseModel):
    """Planner 每轮输出的强类型计划；理由字段用于可观测性（验收关键证据）。"""

    thought: str = ""
    next_action: Literal["call_tool", "activate_skill", "write", "finish"]
    skill: str | None = None
    skill_reason: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_reason: str | None = None
    done_criteria_met: bool = False


class Observation(BaseModel):
    """工具执行结果的结构化摘要——原始输出不进上下文（架构 §3.3）。"""

    tool: str
    success: bool
    summary: str = ""
    error: str | None = None


@dataclass
class ToolContext:
    """Executor 注入的任务上下文；不出现在暴露给 LLM 的 JSON Schema 中（架构 §5.2）。"""

    assistant_id: str
    session_id: str
    data_dir: str


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[str]]


@dataclass
class ToolSpec:
    """统一工具协议：内置工具与 MCP 工具同一张表。"""

    name: str
    description: str
    args_schema: dict[str, Any]
    handler: ToolHandler
    source: str = "builtin"  # "builtin" 或 "mcp:<server>"
    idempotent: bool = True  # 非幂等工具（如 finalize_article/MCP）须显式关闭重试
    captures_source: bool = False  # 结果全文是否入库 sources 表（fetch 类工具显式标记）

    async def call(self, args: dict[str, Any], ctx: ToolContext) -> str:
        return await self.handler(args, ctx)


class AgentState(TypedDict, total=False):
    assistant_id: str               # 当前助手，整个 Loop 不可变
    task: str                       # 用户原始目标
    session_id: str                 # CLI 每次运行生成 uuid4；--resume 续接
    memory_context: str             # 启动时 recall 一次的结果，Planner 每轮从此注入
    observations: Annotated[list[dict[str, Any]], operator.add]
    plan: dict[str, Any] | None
    active_skills: Annotated[list[str], operator.add]
    skill_prompts: Annotated[list[str], operator.add]
    outline: list[str]
    title: str
    draft: str
    step: int                       # 已用步数（防死循环）
    reflect_fails: int              # 连续质检未过次数（>=3 强制 finish，§3.4）
    quality_passed: bool
    status: str                     # running / done / failed
    output_path: str | None
    finish_note: str | None         # 异常终止说明（降级/超步数/质检连败）
