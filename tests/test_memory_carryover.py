"""Two real Runtime runs prove same-assistant memory carryover."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from agent.runtime import AgentRuntime
from config.settings import Settings


def _response(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class _FakeStream:
    def __init__(self) -> None:
        self._parts = iter(["先从工程案例开始。", "再解释模型蒸馏原理。"])

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            part = next(self._parts)
        except StopIteration:
            raise StopAsyncIteration
        return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=part))])


class _MemoryFakeCompletions:
    def __init__(self) -> None:
        self.planner_prompts: list[str] = []

    async def create(self, *, messages, stream=False, **_kwargs):
        if stream:
            return _FakeStream()
        user = messages[-1]["content"]
        if "拟定大纲" in user:
            return _response(json.dumps(
                {"title": "模型蒸馏实践", "sections": ["工程案例", "核心原理"]},
                ensure_ascii=False,
            ))
        if "核查清单" in user:
            return _response(json.dumps(
                {
                    "passed": True,
                    "missing": [],
                    "new_preferences": ["正文先讲工程案例，再解释原理"],
                },
                ensure_ascii=False,
            ))
        self.planner_prompts.append(user)
        return _response(json.dumps(
            {
                "thought": "直接成文",
                "next_action": "write",
                "skill": None,
                "skill_reason": "已有足够上下文",
                "tool_calls": [],
                "tool_reason": None,
                "done_criteria_met": False,
            },
            ensure_ascii=False,
        ))


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path,
        data_dir=tmp_path,
        skills_dir=Path(__file__).resolve().parents[1] / "skills",
        mcp_config=tmp_path / "empty.json",
        openai_api_key="fake",
        openai_base_url="",
        model_name="fake",
    )


def test_second_runtime_run_receives_first_preference_and_article(tmp_path: Path):
    async def scenario():
        runtime = AgentRuntime(_settings(tmp_path))
        completions = _MemoryFakeCompletions()
        runtime.llm = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        first_task = "写一篇模型蒸馏实践"
        second_task = "再次写模型蒸馏实践"
        try:
            first = await runtime.run("default", first_task)
            second = await runtime.run("default", second_task)
        finally:
            await runtime.close()

        assert first["status"] == "done"
        assert second["status"] == "done"
        assert "正文先讲工程案例，再解释原理" in completions.planner_prompts[1]
        assert "《模型蒸馏实践》" in completions.planner_prompts[1]
        assert second_task not in second["memory_context"]

    asyncio.run(scenario())
