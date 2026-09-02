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


# ---------- phase10 第三批次：助手文件一致性 ----------

def test_update_and_create_reject_blank_name(tmp_path):
    """phase10 P2-5：registry 层收口空白显示名，CLI/API 两端自动对齐。"""
    registry, store = _registry(tmp_path)
    registry.create("editor", "编辑助手")
    with pytest.raises(ValueError, match="显示名"):
        registry.update("editor", name="   ")
    assert registry.get("editor").name == "编辑助手"
    with pytest.raises(ValueError, match="显示名"):
        registry.create("blank-name", "   ")
    store.close()


def test_update_rollback_failure_is_logged_and_raises(tmp_path, monkeypatch, caplog):
    """phase10 P2-2：回滚失败经 logging 运行期可见，不再写进会被 reload 清空的列表。"""
    import logging

    registry, store = _registry(tmp_path)
    created = registry.create("editor", "编辑助手", persona="旧人设")

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(registry, "_atomic_write_text", boom)
    with caplog.at_level(logging.WARNING, logger="agent.assistant_registry"):
        with pytest.raises(OSError, match="disk full"):
            registry.update("editor", name="新名字", persona="新人设")

    assert any("回滚" in record.message for record in caplog.records)
    monkeypatch.undo()
    # 所有写入（含回滚）都失败：文件保持原样，助手仍在注册表中
    assert "新名字" not in (created.directory / "assistant.yaml").read_text(encoding="utf-8")
    assert registry.get("editor").name == "编辑助手"
    store.close()


def test_update_writes_are_atomic_no_partial_file(tmp_path, monkeypatch):
    """phase10 P2-3：assistant.yaml/persona.md 走临时文件+os.replace，
    落盘失败不截断既有文件、不留临时文件。"""
    registry, store = _registry(tmp_path)
    created = registry.create("editor", "编辑助手", persona="旧人设")
    real_replace = assistant_registry_module.os.replace

    def broken_replace(src, dst):
        if str(dst).endswith(("assistant.yaml", "persona.md")):
            raise OSError("replace failed")
        return real_replace(src, dst)

    monkeypatch.setattr(assistant_registry_module.os, "replace", broken_replace)
    with pytest.raises(OSError, match="replace failed"):
        registry.update("editor", name="新名字", persona="新人设")
    monkeypatch.undo()

    assert (created.directory / "persona.md").read_text(encoding="utf-8").strip() == "旧人设"
    assert "编辑助手" in (created.directory / "assistant.yaml").read_text(encoding="utf-8")
    assert not list(created.directory.glob("*.tmp"))
    registry.reload()
    assert registry.get("editor").name == "编辑助手"
    assert registry.get("editor").persona == "旧人设"
    store.close()


def test_reload_is_safe_against_concurrent_reads(tmp_path):
    """phase10 P2-4：reload 以「整体重建+单次替换」进行，读端不会命中清空窗口。"""
    import threading

    registry, store = _registry(tmp_path)
    registry.create("writer-a", "作者甲")
    stop = threading.Event()
    errors: list[Exception] = []

    def reader():
        while not stop.is_set():
            try:
                registry.get("writer-a")
                registry.list()
            except KeyError as exc:  # 清空窗口内存在的助手瞬时 404
                errors.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for _ in range(20):
            registry.reload()
    finally:
        stop.set()
        thread.join()

    assert not errors
    store.close()


def test_persona_over_limit_rejected_in_registry(tmp_path):
    """phase10 P3-2：persona 50,000 字符上限下沉 registry 单点收口，CLI 与 API 同口径。"""
    registry, store = _registry(tmp_path)
    with pytest.raises(ValueError, match="50000"):
        registry.create("big-persona", "编辑助手", persona="字" * 50_001)
    registry.create("editor", "编辑助手")
    with pytest.raises(ValueError, match="50000"):
        registry.update("editor", persona="字" * 50_001)
    assert registry.get("editor").persona == _DEFAULT_PERSONA
    store.close()


def test_persona_file_outside_directory_rejected(tmp_path):
    """phase10 P3-4：手改 persona_file 指向助手目录之外按损坏配置拒绝。"""
    registry, store = _registry(tmp_path)
    created = registry.create("editor", "编辑助手", persona="旧人设")
    cfg = created.directory / "assistant.yaml"
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))

    raw["persona_file"] = "../escaped-persona.md"
    cfg.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="persona_file"):
        registry.update("editor", name="新名字")

    raw["persona_file"] = str(tmp_path / "outside-persona.md")
    cfg.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="persona_file"):
        registry.update("editor", name="新名字")

    # reload 不阻断启动，但该助手按损坏配置标记警告并跳过
    registry.reload()
    assert all(item.id != "editor" for item in registry.list())
    assert any("editor" in warning for warning in registry.warnings)
    store.close()
