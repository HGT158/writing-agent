"""Agent Loop 图级回归测试（审查 P0-1 等核心循环零覆盖区域的补测）。

用 FakeLLM 驱动真实状态机：
- 恒不收敛的 Planner 必须在 max_steps 处优雅终止（不抛 GraphRecursionError、不丢成果）
- 质检连败 3 次必须强制定稿并标注存疑
- 正常路径：write → 质检通过 → 定稿落盘
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from agent.assistant_registry import Assistant
from agent.events import EventBus
from agent.executor import ToolRegistry
from agent.loop import RuntimeServices, build_graph, node_reflect
from agent.schemas import ToolSpec
from agent.skills import load_skills
from agent.tools import make_builtin_tools
from config.settings import Settings
from memory.store import MemoryStore

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"


# ---------------------------------------------------------------- Fake LLM

def _resp(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class _FakeStream:
    def __init__(self, parts: list[str]):
        self._it = iter(parts)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            part = next(self._it)
        except StopIteration:
            raise StopAsyncIteration
        return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=part))])


class _FakeCompletions:
    def __init__(self, plan_of, reflect_passed: bool):
        self._plan_of = plan_of
        self._reflect_passed = reflect_passed

    async def create(self, model, messages, temperature=None, response_format=None, stream=False):
        if stream:
            return _FakeStream(["本节正文第一段。", "本节正文第二段，（来源：https://example.com/a）"])
        user = messages[-1]["content"]
        if "拟定大纲" in user:
            return _resp(json.dumps({"title": "测试文章", "sections": ["第一节", "第二节"]}, ensure_ascii=False))
        if "核查清单" in user:
            return _resp(json.dumps(
                {"passed": self._reflect_passed, "missing": ["来源不足"], "new_preferences": []},
                ensure_ascii=False))
        return _resp(json.dumps(self._plan_of(user), ensure_ascii=False))


class FakeLLM:
    def __init__(self, plan_of, reflect_passed: bool = True):
        self.chat = SimpleNamespace(completions=_FakeCompletions(plan_of, reflect_passed))


# ---------------------------------------------------------------- 工具函数

def _services(tmp_path: Path, plan_of, reflect_passed: bool, max_steps: int) -> RuntimeServices:
    store = MemoryStore(tmp_path)
    tools = ToolRegistry()
    tools.register_all(make_builtin_tools(tmp_path, store))

    async def noop_handler(args, ctx):
        return "noop ok"

    tools.register(ToolSpec(name="noop", description="无操作", args_schema={}, handler=noop_handler))
    settings = Settings(
        project_root=tmp_path, data_dir=tmp_path, skills_dir=SKILLS_DIR,
        mcp_config=tmp_path / "none.json", openai_api_key="fake",
        openai_base_url="", model_name="fake", max_steps=max_steps,
    )
    assistant = Assistant(id="tester", name="测试", description="", skills=None,
                          persona="你是写作助手", directory=tmp_path)
    return RuntimeServices(
        llm=FakeLLM(plan_of, reflect_passed), model="fake", assistant=assistant,
        tools=tools, skills=load_skills(SKILLS_DIR), store=store, bus=EventBus(),
        settings=settings,
    )


def _initial_state() -> dict:
    return {
        "assistant_id": "tester", "task": "写一篇测试文章", "session_id": "s1",
        "memory_context": "", "observations": [], "active_skills": [],
        "skill_prompts": [], "step": 0, "reflect_fails": 0,
        "quality_passed": False, "status": "running",
    }


async def _run_graph(services: RuntimeServices) -> dict:
    graph = build_graph(services).compile()
    return await graph.ainvoke(
        _initial_state(),
        config={"configurable": {"thread_id": "tester:s1"},
                "recursion_limit": services.settings.max_steps * 6 + 20},
    )


_PLAN_NOOP = lambda _u: {"thought": "继续搜", "next_action": "call_tool", "skill": None,
                         "skill_reason": "不需要", "tool_calls": [{"tool": "noop", "args": {}}],
                         "tool_reason": "测试", "done_criteria_met": False}
_PLAN_WRITE = lambda _u: {"thought": "成文", "next_action": "write", "skill": None,
                          "skill_reason": "素材够", "tool_calls": [],
                          "tool_reason": None, "done_criteria_met": False}


def test_never_converging_planner_stops_at_max_steps(tmp_path):
    """P0-1 回归：恒不收敛时 step 必须递增并在 max_steps 优雅终止（不抛 GraphRecursionError）。"""
    services = _services(tmp_path, _PLAN_NOOP, reflect_passed=True, max_steps=3)
    final = asyncio.run(_run_graph(services))
    assert final["step"] == 3                       # step 真的在递增
    assert final["status"] == "failed"              # 无草稿 → failed，但无异常
    assert final["finish_note"]                     # 原因非空
    services.store.close()


def test_reflect_fails_three_times_forces_finalize_with_note(tmp_path):
    """质检连败 3 次 → 强制定稿并标注存疑，草稿不丢失。"""
    services = _services(tmp_path, _PLAN_WRITE, reflect_passed=False, max_steps=25)
    final = asyncio.run(_run_graph(services))
    assert final["status"] == "done"
    assert final["reflect_fails"] == 3
    assert "质检连续 3 次" in final["finish_note"]
    content = Path(final["output_path"]).read_text(encoding="utf-8")
    assert "注意：质检连续 3 次" in content
    services.store.close()


def test_normal_write_pass_finalize(tmp_path):
    """正常路径：成文 → 质检通过 → 定稿落盘，无异常标注。"""
    services = _services(tmp_path, _PLAN_WRITE, reflect_passed=True, max_steps=25)
    final = asyncio.run(_run_graph(services))
    assert final["status"] == "done"
    assert final["finish_note"] is None
    content = Path(final["output_path"]).read_text(encoding="utf-8")
    assert "# 测试文章" in content and "## 第一节" in content
    assert "tester" in str(final["output_path"])  # 文章落在本助手目录（隔离）
    services.store.close()


def test_reflect_invalid_json_counts_as_failure(tmp_path):
    services = _services(tmp_path, _PLAN_WRITE, reflect_passed=True, max_steps=25)

    class InvalidCompletions:
        async def create(self, **_kwargs):
            return _resp("not-json")

    services.llm = SimpleNamespace(
        chat=SimpleNamespace(completions=InvalidCompletions())
    )
    state = {**_initial_state(), "draft": "# 草稿"}

    result = asyncio.run(node_reflect(state, services))

    assert result["quality_passed"] is False
    assert result["reflect_fails"] == 1
    assert "有效 JSON" in result["observations"][0]["error"]
    services.store.close()
