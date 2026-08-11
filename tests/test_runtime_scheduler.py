"""Runtime ownership and CLI lifecycle for the scheduler."""
from __future__ import annotations

import asyncio
from argparse import Namespace
from pathlib import Path

import agent.__main__ as cli
from agent.__main__ import _build_parser
from agent.events import EventBus
from agent.runtime import AgentRuntime
from config.settings import JOBS, Settings, load_settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path,
        data_dir=tmp_path,
        skills_dir=Path(__file__).resolve().parents[1] / "skills",
        mcp_config=tmp_path / "empty.json",
        openai_api_key="fake",
        openai_base_url="",
        model_name="fake",
        jobs=[
            {
                "id": "daily-ai-news",
                "assistant_id": "default",
                "cron": "0 8 * * *",
                "task": "生成日报",
            }
        ],
    )


def test_runtime_starts_and_closes_scheduler_on_same_loop(tmp_path):
    async def scenario():
        runtime = AgentRuntime(_settings(tmp_path), EventBus())
        await runtime.start(enable_scheduler=True)
        try:
            assert runtime.scheduler is not None
            assert runtime.scheduler.loop is asyncio.get_running_loop()
            assert runtime.scheduler.get_job("daily-ai-news") is not None
        finally:
            await runtime.close()
        assert runtime.scheduler is None

    asyncio.run(scenario())


def test_cli_exposes_long_running_schedule_command():
    args = _build_parser().parse_args(["schedule"])
    assert args.command == "schedule"


def test_load_settings_copies_configured_jobs():
    settings = load_settings()
    assert settings.jobs == JOBS
    assert settings.jobs[0]["assistant_id"] == "default"
    assert settings.jobs is not JOBS
    settings.jobs[0]["task"] = "changed in test"
    assert settings.jobs[0] is not JOBS[0]
    assert JOBS[0]["task"] != "changed in test"


def test_cli_closes_runtime_when_start_fails(monkeypatch):
    class FailingRuntime:
        def __init__(self) -> None:
            self.closed = False

        async def start(self):
            raise RuntimeError("startup failed")

        async def close(self):
            self.closed = True

    runtime = FailingRuntime()
    monkeypatch.setattr(cli, "AgentRuntime", lambda settings, bus: runtime)

    result = asyncio.run(cli._cmd_run(Namespace(assistant="default", task="test", resume=None)))

    assert result == 2
    assert runtime.closed


def test_cli_converts_unexpected_run_error_and_closes_runtime(monkeypatch):
    class FailingRuntime:
        def __init__(self) -> None:
            self.closed = False

        async def start(self):
            return None

        async def run(self, assistant_id, task, session_id=None):
            raise ValueError("unexpected failure")

        async def close(self):
            self.closed = True

    runtime = FailingRuntime()
    monkeypatch.setattr(cli, "AgentRuntime", lambda settings, bus: runtime)

    result = asyncio.run(cli._cmd_run(Namespace(assistant="default", task="test", resume=None)))

    assert result == 2
    assert runtime.closed
