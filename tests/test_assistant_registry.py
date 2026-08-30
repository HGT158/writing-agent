"""助手注册测试（审查 P1-7）：id 校验防路径穿越、创建/删除/归档、persona 可写可编辑（v1.28）。"""
import yaml
import pytest

import agent.assistant_registry as assistant_registry_module
from agent.assistant_registry import AssistantRegistry, _DEFAULT_PERSONA
from memory.store import MemoryStore


def _registry(tmp_path):
    store = MemoryStore(tmp_path)
    return AssistantRegistry(tmp_path, store), store


def test_default_assistant_auto_created(tmp_path):
    registry, store = _registry(tmp_path)
    assert registry.get("default").name
    store.close()


def test_create_rejects_path_traversal(tmp_path):
    registry, store = _registry(tmp_path)
    with pytest.raises(ValueError, match="非法"):
        registry.create("../../evil", "evil")
    assert not (tmp_path.parent / "evil").exists()  # 无文件系统副作用
    for bad in ("UPPER", "has space", "中文id", "-lead", ""):
        with pytest.raises(ValueError):
            registry.create(bad, "x")
    store.close()


def test_create_and_duplicate(tmp_path):
    registry, store = _registry(tmp_path)
    a = registry.create("marketing", "营销文案", "短平快")
    assert a.id == "marketing" and (a.directory / "assistant.yaml").exists()
    with pytest.raises(ValueError, match="已存在"):
        registry.create("marketing", "x")
    store.close()


def test_delete_archives_and_locked_rejected(tmp_path):
    registry, store = _registry(tmp_path)
    registry.create("marketing", "营销文案")
    store.acquire_lock("marketing", "task-1")
    with pytest.raises(RuntimeError, match="运行中"):
        registry.delete("marketing")
    store.release_lock("marketing", "task-1")
    target = registry.delete("marketing")
    assert target.exists() and "archive" in str(target)
    with pytest.raises(KeyError):
        registry.get("marketing")
    store.close()


def test_purge_deletes_project_metadata_change_sets_and_project_archives(tmp_path):
    registry, store = _registry(tmp_path)
    registry.create("marketing", "营销文案")
    archived_project = store.create_project("marketing", "旧项目")
    archived_path = store.archive_project("marketing", archived_project.project_id)
    current = store.create_project("marketing", "当前项目")
    change = store.create_selection_change_set(
        "marketing", current.project_id, current.entry_document_id,
        task_id="task-purge", start=0, end=0, original_text="",
        replacement_text="新正文", base_version=1, source="selection",
    )

    target = registry.delete("marketing", purge=True)

    assert not target.exists()
    assert not archived_path.exists()
    assert store.list_projects("marketing") == []
    with pytest.raises(KeyError):
        store.get_change_set("marketing", current.project_id, change.change_set_id)
    store.close()


def test_create_with_persona_writes_custom_content(tmp_path):
    registry, store = _registry(tmp_path)
    registry.create("editor", "编辑助手", "润色", persona="你是一名严谨的编辑。")
    assert registry.get("editor").persona == "你是一名严谨的编辑。"
    store.close()


def test_create_blank_persona_falls_back_to_default(tmp_path):
    registry, store = _registry(tmp_path)
    registry.create("editor", "编辑助手", persona="   ")
    assert registry.get("editor").persona == _DEFAULT_PERSONA
    store.close()


def test_update_rewrites_fields_and_preserves_yaml_extras(tmp_path):
    registry, store = _registry(tmp_path)
    registry.create("editor", "编辑助手", "旧描述")
    cfg = registry.root / "editor" / "assistant.yaml"
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    raw["skills"] = ["editing"]
    cfg.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    registry.reload()

    updated = registry.update("editor", name="新名字", description="新描述", persona="新的人设")

    assert updated.name == "新名字"
    assert updated.description == "新描述"
    assert updated.persona == "新的人设"
    preserved = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert preserved["skills"] == ["editing"]
    assert preserved["created_at"] == raw["created_at"]
    assert (registry.root / "editor" / "persona.md").read_text(encoding="utf-8").strip() == "新的人设"
    store.close()


def test_update_partial_fields_keep_others(tmp_path):
    registry, store = _registry(tmp_path)
    registry.create("editor", "编辑助手", "旧描述", persona="旧人设")
    updated = registry.update("editor", persona="只改人设")
    assert updated.name == "编辑助手"
    assert updated.description == "旧描述"
    assert updated.persona == "只改人设"
    store.close()


def test_update_blank_persona_resets_to_default(tmp_path):
    registry, store = _registry(tmp_path)
    registry.create("editor", "编辑助手", persona="旧人设")
    updated = registry.update("editor", persona="  ")
    assert updated.persona == _DEFAULT_PERSONA
    store.close()


def test_update_default_assistant_is_allowed(tmp_path):
    registry, store = _registry(tmp_path)
    updated = registry.update("default", name="自定义默认", persona="默认人设")
    assert updated.name == "自定义默认"
    assert updated.persona == "默认人设"
    store.close()


def test_update_rejects_running_assistant_and_releases_lock(tmp_path):
    registry, store = _registry(tmp_path)
    registry.create("editor", "编辑助手")
    store.acquire_lock("editor", "task-1")
    with pytest.raises(RuntimeError, match="运行中"):
        registry.update("editor", name="新名字")
    store.release_lock("editor", "task-1")
    updated = registry.update("editor", name="新名字")
    assert updated.name == "新名字"
    store.close()


def test_update_unknown_assistant_raises_without_lock_side_effect(tmp_path):
    registry, store = _registry(tmp_path)
    with pytest.raises(KeyError):
        registry.update("ghost", name="x")
    assert store.current_lock_task_id("ghost") is None
    store.close()


def test_update_without_fields_raises(tmp_path):
    registry, store = _registry(tmp_path)
    registry.create("editor", "编辑助手")
    with pytest.raises(ValueError, match="字段"):
        registry.update("editor")
    store.close()


def test_update_stops_when_persona_write_fails(tmp_path):
    registry, store = _registry(tmp_path)
    created = registry.create("editor", "编辑助手", persona="旧人设")
    (created.directory / "persona.md").unlink()
    (created.directory / "persona.md").mkdir()  # 目录占位使 persona 写入失败

    with pytest.raises(OSError):
        registry.update("editor", name="新名字", persona="新人设")

    assert "新名字" not in (created.directory / "assistant.yaml").read_text(encoding="utf-8")
    store.close()


def test_update_rolls_back_persona_when_yaml_write_fails(tmp_path, monkeypatch):
    registry, store = _registry(tmp_path)
    created = registry.create("editor", "编辑助手", persona="旧人设")

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(assistant_registry_module.yaml, "safe_dump", boom)
    with pytest.raises(OSError, match="disk full"):
        registry.update("editor", name="新名字", persona="新人设")
    monkeypatch.undo()

    assert (created.directory / "persona.md").read_text(encoding="utf-8").strip() == "旧人设"
    registry.reload()
    assert registry.get("editor").persona == "旧人设"
    assert registry.get("editor").name == "编辑助手"
    store.close()
