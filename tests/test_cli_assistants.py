"""CLI 助手管理命令测试（v1.28）：create/edit 的 persona 参数与部分更新语义。"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent.__main__ import _build_parser, _cmd_assistants
from agent.assistant_registry import _DEFAULT_PERSONA
from config.settings import Settings
from memory.store import MemoryStore


def _settings(tmp_path: Path) -> Settings:
    empty = tmp_path / "empty.json"
    empty.write_text('{"mcpServers": {}}', encoding="utf-8")
    return Settings(
        project_root=tmp_path,
        data_dir=tmp_path,
        skills_dir=Path(__file__).resolve().parents[1] / "skills",
        mcp_config=empty,
        openai_api_key="fake",
        openai_base_url="",
        model_name="fake",
    )


def _run_cli(tmp_path: Path, *cli_args: str) -> int:
    args = _build_parser().parse_args(["assistants", *cli_args])
    return _cmd_assistants(args, settings=_settings(tmp_path))


def _persona_file(tmp_path: Path) -> Path:
    return tmp_path / "assistants" / "editor" / "persona.md"


def _assistant_yaml(tmp_path: Path) -> dict:
    return yaml.safe_load(
        (tmp_path / "assistants" / "editor" / "assistant.yaml").read_text(encoding="utf-8")
    )


def test_create_with_persona_flag_writes_custom_content(tmp_path):
    code = _run_cli(tmp_path, "create", "editor", "--name", "编辑助手", "--persona", "你是一名毒舌编辑。")

    assert code == 0
    assert _persona_file(tmp_path).read_text(encoding="utf-8").strip() == "你是一名毒舌编辑。"


def test_create_with_persona_file_flag_reads_utf8_text(tmp_path):
    source = tmp_path / "persona-source.txt"
    source.write_text("文件里的人设", encoding="utf-8")

    code = _run_cli(tmp_path, "create", "editor", "--persona-file", str(source))

    assert code == 0
    assert _persona_file(tmp_path).read_text(encoding="utf-8").strip() == "文件里的人设"


def test_create_without_persona_uses_default_and_blank_description(tmp_path):
    code = _run_cli(tmp_path, "create", "editor")

    assert code == 0
    assert _persona_file(tmp_path).read_text(encoding="utf-8").strip() == _DEFAULT_PERSONA
    assert _assistant_yaml(tmp_path)["description"] == ""


def test_create_missing_persona_file_fails_cleanly(tmp_path):
    code = _run_cli(tmp_path, "create", "editor", "--persona-file", str(tmp_path / "missing.txt"))

    assert code == 2
    assert not (tmp_path / "assistants" / "editor").exists()


def test_persona_and_persona_file_are_mutually_exclusive(tmp_path):
    source = tmp_path / "persona-source.txt"
    source.write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit):
        _build_parser().parse_args([
            "assistants", "create", "editor",
            "--persona", "a", "--persona-file", str(source),
        ])


def test_edit_updates_persona(tmp_path):
    assert _run_cli(tmp_path, "create", "editor", "--persona", "旧人设") == 0

    code = _run_cli(tmp_path, "edit", "editor", "--persona", "新人设")

    assert code == 0
    assert _persona_file(tmp_path).read_text(encoding="utf-8").strip() == "新人设"


def test_edit_is_partial_and_keeps_unprovided_fields(tmp_path):
    assert _run_cli(
        tmp_path, "create", "editor",
        "--name", "旧名", "--description", "旧描述", "--persona", "旧人设",
    ) == 0

    code = _run_cli(tmp_path, "edit", "editor", "--name", "新名")

    assert code == 0
    raw = _assistant_yaml(tmp_path)
    assert raw["name"] == "新名"
    assert raw["description"] == "旧描述"  # 未提供 --description 不得清空
    assert _persona_file(tmp_path).read_text(encoding="utf-8").strip() == "旧人设"


def test_edit_requires_id_and_at_least_one_field(tmp_path):
    assert _run_cli(tmp_path, "create", "editor") == 0

    assert _run_cli(tmp_path, "edit") == 2
    assert _run_cli(tmp_path, "edit", "editor") == 2


def test_edit_unknown_assistant_fails(tmp_path):
    assert _run_cli(tmp_path, "edit", "ghost", "--name", "x") == 2


def test_edit_rejected_while_task_running(tmp_path):
    assert _run_cli(tmp_path, "create", "editor") == 0
    store = MemoryStore(tmp_path)
    try:
        store.acquire_lock("editor", "task-1")
        assert _run_cli(tmp_path, "edit", "editor", "--persona", "新人设") == 2
        store.release_lock("editor", "task-1")
        assert _run_cli(tmp_path, "edit", "editor", "--persona", "新人设") == 0
    finally:
        store.close()


def test_edit_blank_name_is_rejected(tmp_path):
    assert _run_cli(tmp_path, "create", "editor") == 0

    assert _run_cli(tmp_path, "edit", "editor", "--name", "  ") == 2
    assert _assistant_yaml(tmp_path)["name"] != "  "
