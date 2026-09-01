"""Planner（架构 §5.1）：每轮动态决策，输出强类型 ActionPlan，理由必填。

降级路径必须可路由：连续两次输出非法 JSON 时，强制构造 finish 计划，
绝不产生无法驱动状态机的中间态。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI
from pydantic import ValidationError

from .llm import chat_text, extract_json
from .schemas import ActionPlan, ToolSpec

logger = logging.getLogger(__name__)

_SYSTEM = """你是一名写作 Agent 的规划器（Planner）。你不亲自写作，而是每轮决定下一步动作。

动作定义：
- call_tool：调用工具（搜索、抓取、读写文件等），tool_calls 可并行多个
- activate_skill：激活一个 Skill（注入其工作流程指导后续行动），skill 填 Skill 名
- write：素材已足够，开始/继续撰写正文
- finish：任务完成或无法继续，结束

硬性要求：
1. 只输出一个 JSON 对象，不要输出任何其他文字，字段如下：
   {"thought": "当前局势判断（中文）",
    "next_action": "call_tool|activate_skill|write|finish",
    "skill": "要激活的 Skill 名或 null",
    "skill_reason": "为什么选择/不选择该 Skill（必填）",
    "tool_calls": [{"tool": "工具名", "args": {...}}],
    "tool_reason": "工具选择理由（必填，无工具调用时填 null）",
    "done_criteria_met": false}
2. 只能使用下方列出的 Skill 和工具，禁止发明不存在的名字。
3. 需要外部事实/时效信息时优先激活 research；成文时激活 writing；润色核查时激活 editing。
4. 不要重复调用已得到结果的工具；观察里已有的信息直接利用。
"""


def _tools_catalog(tools: list[ToolSpec]) -> str:
    lines = []
    for t in tools:
        schema = json.dumps(t.args_schema, ensure_ascii=False)
        lines.append(f"- {t.name}：{t.description} 参数 schema：{schema}")
    return "\n".join(lines) or "(无可用工具)"


def observations_text(observations: list[dict[str, Any]], window: int = 8) -> str:
    """观察滑窗（架构 §3.3）：最近 8 条全文，更早的压缩为一行索引。"""
    if not observations:
        return "(暂无观察)"
    older, recent = observations[:-window], observations[-window:]
    lines = [f"[{i + 1}] {o.get('tool')} → {'成功' if o.get('success') else '失败'}"
             for i, o in enumerate(older)]
    offset = len(older)
    for i, o in enumerate(recent):
        body = o.get("summary") if o.get("success") else f"错误：{o.get('error')}"
        lines.append(f"[{offset + i + 1}] {o.get('tool')}：{body}")
    return "\n".join(lines)


class Planner:
    def __init__(
        self, llm: AsyncOpenAI, model: str,
        json_mode: bool = True, temperature: float = 0.3,
    ) -> None:
        self.llm = llm
        self.model = model
        self.json_mode = json_mode
        self.temperature = temperature

    async def make_plan(
        self,
        *,
        task: str,
        persona: str,
        memory_context: str,
        observations: list[dict[str, Any]],
        skills_catalog: str,
        tools: list[ToolSpec],
        skill_prompts: list[str],
        step: int,
        max_steps: int,
    ) -> ActionPlan:
        user = f"""【助手人设】
{persona}

【任务】
{task}

【长期记忆摘要】
{memory_context or "(无)"}

【已激活 Skill 的工作流程】
{chr(10).join(skill_prompts) if skill_prompts else "(尚未激活任何 Skill)"}

【可用 Skill 清单】
{skills_catalog}

【可用工具清单】
{_tools_catalog(tools)}

【历史观察】
{observations_text(observations)}

【进度】第 {step}/{max_steps} 步。请输出本轮计划 JSON。"""

        messages = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ]
        last_error: str = ""
        for _attempt in range(2):  # 首次 + 回喂错误重试一次
            text = await chat_text(self.llm, self.model, messages,
                                   temperature=self.temperature,
                                   json_mode=self.json_mode)
            try:
                return ActionPlan.model_validate_json(extract_json(text))
            except (ValidationError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                messages.append({"role": "assistant", "content": text})
                messages.append({
                    "role": "user",
                    "content": f"上次输出不是合法 JSON：{last_error}。请只输出符合要求的 JSON 对象。",
                })

        logger.warning("Planner 连续输出非法 JSON，强制 finish：%s", last_error)
        return ActionPlan(
            thought=f"规划器连续输出非法 JSON（{last_error[:120]}），终止并保留已有成果",
            next_action="finish",
            done_criteria_met=True,
        )
