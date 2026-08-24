"""助手注册测试（审查 P1-7）：id 校验防路径穿越、创建/删除/归档。"""
import pytest

from agent.assistant_registry import AssistantRegistry
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
