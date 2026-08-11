"""Change set 的生成、预览、应用与并发校验。"""
from __future__ import annotations

import pytest

from memory.store import MemoryStore


def _document(store: MemoryStore):
    project = store.create_project("writer-a", "改写测试")
    document = store.save_document(
        "writer-a", project.project_id, project.entry_document_id,
        "这是原始文本。", expected_version=1,
    )
    return project, document


def test_create_change_set_does_not_mutate_document_until_apply(tmp_path):
    store = MemoryStore(tmp_path)
    project, document = _document(store)

    change = store.create_change_set(
        "writer-a", project.project_id, document.document_id,
        source="selection", start=0, end=4,
        original_text="这是原始", replacement_text="这是改写",
        base_version=document.version,
    )

    current = store.get_document("writer-a", project.project_id, document.document_id)
    assert change.status == "pending"
    assert current.content == "这是原始文本。"
    assert current.version == document.version
    store.close()


def test_apply_change_set_replaces_unicode_range_and_increments_version(tmp_path):
    store = MemoryStore(tmp_path)
    project, document = _document(store)

    change = store.create_change_set(
        "writer-a", project.project_id, document.document_id,
        source="selection", start=0, end=4,
        original_text="这是原始", replacement_text="这是改写",
        base_version=document.version,
    )
    updated, applied = store.apply_change_set(
        "writer-a", project.project_id, change.change_set_id,
        expected_version=document.version,
    )

    assert updated.content == "这是改写文本。"
    assert updated.version == document.version + 1
    assert applied.status == "applied"
    store.close()


def test_apply_change_set_rejects_stale_version_without_mutating_content(tmp_path):
    store = MemoryStore(tmp_path)
    project, document = _document(store)
    change = store.create_change_set(
        "writer-a", project.project_id, document.document_id,
        source="selection", start=0, end=4,
        original_text="这是原始", replacement_text="这是改写",
        base_version=document.version,
    )
    store.save_document(
        "writer-a", project.project_id, document.document_id,
        "用户先编辑。", expected_version=document.version,
    )

    with pytest.raises(RuntimeError, match="版本冲突"):
        store.apply_change_set(
            "writer-a", project.project_id, change.change_set_id,
            expected_version=document.version,
        )
    current = store.get_document("writer-a", project.project_id, document.document_id)
    assert current.content == "用户先编辑。"
    assert store.get_change_set("writer-a", project.project_id, change.change_set_id).status == "pending"
    store.close()


def test_apply_change_set_rejects_original_text_mismatch(tmp_path):
    store = MemoryStore(tmp_path)
    project, document = _document(store)
    with pytest.raises(RuntimeError, match="原文"):
        store.create_change_set(
            "writer-a", project.project_id, document.document_id,
            source="selection", start=0, end=4,
            original_text="错误快照", replacement_text="不会应用",
            base_version=document.version,
        )
    assert store.get_document("writer-a", project.project_id, document.document_id).content == "这是原始文本。"
    store.close()


def test_reject_change_set_keeps_document_unchanged(tmp_path):
    store = MemoryStore(tmp_path)
    project, document = _document(store)
    change = store.create_change_set(
        "writer-a", project.project_id, document.document_id,
        source="chat", start=0, end=4,
        original_text="这是原始", replacement_text="这是改写",
        base_version=document.version,
    )

    rejected = store.reject_change_set("writer-a", project.project_id, change.change_set_id)

    assert rejected.status == "rejected"
    assert store.get_document("writer-a", project.project_id, document.document_id).content == "这是原始文本。"
    store.close()


def test_change_set_isolation_requires_assistant_and_project_owner(tmp_path):
    store = MemoryStore(tmp_path)
    project, document = _document(store)
    change = store.create_change_set(
        "writer-a", project.project_id, document.document_id,
        source="selection", start=0, end=4,
        original_text="这是原始", replacement_text="这是改写",
        base_version=document.version,
    )

    with pytest.raises(KeyError):
        store.get_change_set("writer-b", project.project_id, change.change_set_id)
    with pytest.raises(KeyError):
        store.apply_change_set("writer-b", project.project_id, change.change_set_id, expected_version=1)
    store.close()
