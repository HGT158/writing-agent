"""Agent Loop（架构 §3）：observe → plan → act/write → reflect 循环，条件边路由。

路由规则（与架构图文一致）：
- Plan 之后按 plan.next_action 四路分发（call_tool/activate_skill → act；write → write；finish → done）
- Reflect 之后二路分发（质检通过/连败/超步数 → done；否则 → observe 回到循环入口计数）
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from langgraph.graph import END, START, StateGraph
from openai import AsyncOpenAI

from config.settings import Settings
from memory.store import MemoryStore

from .assistant_registry import Assistant
from .events import EventBus
from .executor import ToolRegistry, execute_tool_calls
from .llm import chat_text, extract_json
from .planner import Planner, observations_text
from .schemas import ActionPlan, AgentState, Observation, ToolContext
from .skills import Skill, missing_dependencies
from .tools import finalize_article_impl

logger = logging.getLogger(__name__)


@dataclass
class RuntimeServices:
    llm: AsyncOpenAI
    model: str
    assistant: Assistant
    tools: ToolRegistry
    skills: dict[str, Skill]
    store: MemoryStore
    bus: EventBus
    settings: Settings


# ---------------------------------------------------------------- 节点

async def node_observe(state: AgentState) -> dict[str, Any]:
    return {"step": state.get("step", 0) + 1}


async def node_plan(state: AgentState, services: RuntimeServices) -> dict[str, Any]:
    planner = Planner(services.llm, services.model, json_mode=services.settings.json_mode)
    from .skills import catalog_text

    plan = await planner.make_plan(
        task=state["task"],
        persona=services.assistant.persona,
        memory_context=state.get("memory_context", ""),
        observations=state.get("observations", []),
        skills_catalog=catalog_text(services.skills, services.assistant.skills),
        tools=services.tools.list(),
        skill_prompts=state.get("skill_prompts", []),
        step=state.get("step", 0),
        max_steps=services.settings.max_steps,
    )
    services.bus.emit(
        "thought",
        text=plan.thought,
        skill=plan.skill,
        skill_reason=plan.skill_reason,
        tool_reason=plan.tool_reason,
    )
    return {"plan": plan.model_dump()}


async def node_act(state: AgentState, services: RuntimeServices) -> dict[str, Any]:
    plan = ActionPlan(**state["plan"])
    ctx = ToolContext(
        assistant_id=state["assistant_id"],
        session_id=state["session_id"],
        data_dir=str(services.settings.data_dir),
    )

    if plan.next_action == "activate_skill":
        skill = services.skills.get(plan.skill or "")
        allowed = services.assistant.skills
        if skill is None or (allowed is not None and skill.name not in allowed):
            obs = Observation(tool=f"skill:{plan.skill}", success=False,
                              error=f"Skill 不存在或不在本助手技能子集内：{plan.skill}")
        elif skill.name in state.get("active_skills", []):
            # 已激活过：跳过重复注入，防 prompt 重复累积（审查 P1-5）
            return {"observations": [Observation(tool=f"skill:{skill.name}", success=True,
                                                 summary=f"skill「{skill.name}」已在激活状态，跳过重复注入").model_dump()]}
        else:
            missing = missing_dependencies(skill, services.tools.names())
            if missing:
                obs = Observation(tool=f"skill:{skill.name}", success=False,
                                  error=f"skill '{skill.name}' 缺少依赖工具: {', '.join(missing)}")
            else:
                services.bus.emit("skill", text=f"已激活「{skill.name}」：{plan.skill_reason or ''}")
                return {
                    "active_skills": [skill.name],
                    "skill_prompts": [skill.body],
                    "observations": [Observation(tool=f"skill:{skill.name}", success=True,
                                                 summary=f"已激活 skill「{skill.name}」").model_dump()],
                }
        services.bus.emit("tool_result", tool=obs.tool, ok=False, error=obs.error)
        return {"observations": [obs.model_dump()]}

    observations = await execute_tool_calls(plan.tool_calls, services.tools, ctx, services.bus, services.store)
    return {"observations": [o.model_dump() for o in observations]}


async def node_write(state: AgentState, services: RuntimeServices) -> dict[str, Any]:
    """分段成文（架构 §9 防截断）：先大纲，每节独立 LLM 调用（token 流式），最后合并。"""
    task = state["task"]
    outline = state.get("outline") or []
    title = state.get("title") or ""
    material = observations_text(state.get("observations", []))

    # sources 全文回查（审查 P1-4）：URL 与正文素材注入，来源标注才有依据
    sources = services.store.get_sources(state["assistant_id"], state["session_id"], limit=5)
    if sources:
        blocks = [f"来源 {i + 1}：{url}\n{fulltext[:1500]}" for i, (url, _t, fulltext) in enumerate(sources)]
        material += "\n\n【抓取全文素材】\n" + "\n\n".join(blocks)

    if not outline:
        text = await chat_text(
            services.llm, services.model,
            messages=[
                {"role": "system", "content": services.assistant.persona},
                {"role": "user", "content": (
                    f"为文章拟定大纲。任务：{task}\n\n素材摘要：\n{material}\n\n"
                    '只输出 JSON：{"title": "文章标题", "sections": ["第一节标题", "第二节标题", ...]}，3-6 节。'
                )},
            ],
            temperature=0.5,
            json_mode=services.settings.json_mode,
        )
        try:
            parsed = json.loads(extract_json(text))
            title = parsed.get("title") or task[:30]
            outline = [str(s) for s in parsed.get("sections", [])][:8] or ["正文"]
        except json.JSONDecodeError:
            title, outline = task[:30], ["正文"]
        services.bus.emit("thought", text=f"大纲：《{title}》 {' / '.join(outline)}")

    writing_guide = "\n".join(state.get("skill_prompts", []))
    parts: list[str] = []
    for section in outline:
        services.bus.emit("section", title=section)
        stream = await services.llm.chat.completions.create(
            model=services.model,
            messages=[
                {"role": "system", "content": f"{services.assistant.persona}\n\n{writing_guide}"},
                {"role": "user", "content": (
                    f"撰写文章《{title}》的其中一节：「{section}」。\n"
                    f"文章大纲：{' / '.join(outline)}\n"
                    f"全文任务：{task}\n\n可用素材：\n{material}\n\n"
                    "要求：300-600 字，只输出本节正文（不要重复节标题），事实须来自素材，来源在句末以（来源：URL）标注。"
                )},
            ],
            stream=True,
            temperature=0.6,
        )
        buf: list[str] = []
        async for chunk in stream:
            if not chunk.choices:  # 部分服务的尾包仅带 usage，无 choices（审查 P2-15）
                continue
            delta = chunk.choices[0].delta.content or ""
            if delta:
                services.bus.emit("token", text=delta)
                buf.append(delta)
        parts.append(f"## {section}\n\n{''.join(buf).strip()}")

    draft = f"# {title}\n\n" + "\n\n".join(parts)
    return {"draft": draft, "outline": outline, "title": title}


async def node_reflect(state: AgentState, services: RuntimeServices) -> dict[str, Any]:
    """按 checklist 质检（架构 §3.4）；无草稿时直接放行回 Plan。"""
    draft = state.get("draft", "")
    if not draft:
        return {}

    text = await chat_text(
        services.llm, services.model,
        messages=[
            {"role": "system", "content": "你是质检员，按清单逐项核查文章，只输出 JSON。"},
            {"role": "user", "content": (
                f"任务：{state['task']}\n\n文章草稿：\n{draft[:6000]}\n\n"
                "核查清单：1) 引用来源≥3且可追溯 2) 大纲各节均有实质内容 3) 正文含来源标注 "
                "4) 无明显臆造数字/人名 5) 篇幅达到任务要求。\n"
                '输出 JSON：{"passed": true/false, "missing": ["未达标项说明"], '
                '"new_preferences": ["从本次任务发现的值得长期记住的用户偏好，可为空数组"]}'
            )},
        ],
        temperature=0.2,
        json_mode=services.settings.json_mode,
    )
    try:
        result = json.loads(extract_json(text))
        passed = bool(result.get("passed", True))
        missing = [str(m) for m in result.get("missing", [])]
        preferences = [str(p) for p in result.get("new_preferences", [])]
    except json.JSONDecodeError:
        passed, missing, preferences = True, [], []

    for pref in preferences[:3]:
        services.store.memorize(state["assistant_id"], "preference", pref, session_id=state["session_id"])

    reflect_fails = 0 if passed else state.get("reflect_fails", 0) + 1
    if passed:
        services.bus.emit("thought", text="质检通过")
    else:
        services.bus.emit("thought", text=f"质检未过（第 {reflect_fails} 次）：{'；'.join(missing)}")
    obs = Observation(tool="reflect", success=passed,
                      summary="质检通过" if passed else "质检未过：" + "；".join(missing),
                      error=None if passed else "；".join(missing))
    return {"quality_passed": passed, "reflect_fails": reflect_fails, "observations": [obs.model_dump()]}


async def node_done(state: AgentState, services: RuntimeServices) -> dict[str, Any]:
    draft = state.get("draft", "")
    if not draft:
        reason = "未能产出草稿（可能工具不可用或规划异常终止）"
        services.bus.emit("failed", reason=reason)
        return {"status": "failed", "finish_note": reason}

    notes: list[str] = []
    if state.get("step", 0) >= services.settings.max_steps:
        notes.append("达到最大步数，提前收敛")
    if not state.get("quality_passed") and state.get("reflect_fails", 0) >= 3:
        notes.append("质检连续 3 次未通过，内容可能存疑")
    note = "；".join(notes)
    if note:
        draft += f"\n\n> 注意：{note}。"

    ctx = ToolContext(
        assistant_id=state["assistant_id"],
        session_id=state["session_id"],
        data_dir=str(services.settings.data_dir),
    )
    title = state.get("title") or state["task"][:30]
    # 直接调用结构化实现（审查 P1-6：不再字符串反解路径、去掉 assert、不绕过异常处理）
    try:
        path = finalize_article_impl(services.store, ctx, title, draft)
    except Exception as exc:
        services.bus.emit("failed", reason=f"定稿失败：{exc}")
        return {"status": "failed", "finish_note": f"定稿失败：{exc}"}
    services.store.add_message(state["assistant_id"], state["session_id"], "assistant",
                               f"完成文章：《{title}》 {path}")
    services.bus.emit("done", path=str(path))
    return {"status": "done", "output_path": str(path), "finish_note": note or None}


# ---------------------------------------------------------------- 路由

def route_after_plan(state: AgentState) -> str:
    plan = state.get("plan") or {}
    return {
        "call_tool": "act",
        "activate_skill": "act",
        "write": "write",
        "finish": "done",
    }.get(plan.get("next_action", "finish"), "done")


def route_after_reflect(state: AgentState, services: RuntimeServices) -> str:
    # 回边指向 observe（而非 plan）：每轮循环都经过计数节点，max_steps 才能生效（审查 P0-1）
    if state.get("step", 0) >= services.settings.max_steps:
        return "done"
    if state.get("draft"):
        if state.get("quality_passed") or state.get("reflect_fails", 0) >= 3:
            return "done"
    return "observe"


# ---------------------------------------------------------------- 装配

def build_graph(services: RuntimeServices) -> StateGraph:
    async def observe(state: AgentState) -> dict[str, Any]:
        return await node_observe(state)

    async def plan(state: AgentState) -> dict[str, Any]:
        return await node_plan(state, services)

    async def act(state: AgentState) -> dict[str, Any]:
        return await node_act(state, services)

    async def write(state: AgentState) -> dict[str, Any]:
        return await node_write(state, services)

    async def reflect(state: AgentState) -> dict[str, Any]:
        return await node_reflect(state, services)

    async def done(state: AgentState) -> dict[str, Any]:
        return await node_done(state, services)

    builder = StateGraph(AgentState)
    builder.add_node("observe", observe)
    builder.add_node("plan", plan)
    builder.add_node("act", act)
    builder.add_node("write", write)
    builder.add_node("reflect", reflect)
    builder.add_node("done", done)

    builder.add_edge(START, "observe")
    builder.add_edge("observe", "plan")
    builder.add_conditional_edges("plan", route_after_plan,
                                  {"act": "act", "write": "write", "done": "done"})
    builder.add_edge("act", "reflect")
    builder.add_edge("write", "reflect")
    builder.add_conditional_edges("reflect",
                                  lambda s: route_after_reflect(s, services),
                                  {"observe": "observe", "done": "done"})
    builder.add_edge("done", END)
    return builder
