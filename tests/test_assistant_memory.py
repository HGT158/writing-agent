"""助手记忆系统完善（v1.30）：画像白盒读写、recall_trace 结构化命中、分页 clamp。

沉淀行为（门槛/提取/直达）的用例见 test_chat_memory_consolidation.py；
本文件只覆盖 Memory 层契约。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memory.long_term import ASSISTANT_PROFILE_MAX_CHARS
from memory.store import MemoryStore


@pytest.fixture()
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path)


def _store_with_document(tmp_path: Path):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "记忆项目")
    document, _ = store.save_document(
        "writer-a", project.project_id, project.entry_document_id,
        "开头段。中间段。结尾段。", expected_version=1,
    )
    return store, project, document


# ---------- 画像白盒读写 ----------

def test_profile_roundtrip_and_human_replace(tmp_path):
    store = MemoryStore(tmp_path)
    store.memorize("writer-a", "preference", "摘要放在文末")
    profile = store.get_assistant_profile("writer-a")
    assert "摘要放在文末" in profile

    raw = "# 自定义画像\n\n- 人工写入的一条\n"
    store.replace_assistant_profile("writer-a", raw)
    # 白盒整文替换：磁盘原样落盘，不重排格式、不补写头部；读取端保留既有 strip 语义
    on_disk = (tmp_path / "assistants" / "writer-a" / "memory" / "profile.md").read_text(encoding="utf-8")
    assert on_disk == raw
    assert store.get_assistant_profile("writer-a") == raw.strip()


def test_profile_missing_file_returns_empty(tmp_path):
    store = MemoryStore(tmp_path)
    assert store.get_assistant_profile("writer-a") == ""


def test_profile_empty_content_is_explicit_wipe(tmp_path):
    store = MemoryStore(tmp_path)
    store.memorize("writer-a", "style", "正式语气")
    store.replace_assistant_profile("writer-a", "")
    assert store.get_assistant_profile("writer-a") == ""
    # 清空后仍可继续追加沉淀
    store.memorize("writer-a", "style", "短句为主")
    assert "短句为主" in store.get_assistant_profile("writer-a")


def test_profile_replace_over_limit_rejected_without_touching_file(tmp_path):
    store = MemoryStore(tmp_path)
    store.memorize("writer-a", "preference", "原有偏好")
    before = store.get_assistant_profile("writer-a")
    with pytest.raises(ValueError):
        store.replace_assistant_profile("writer-a", "字" * (ASSISTANT_PROFILE_MAX_CHARS + 1))
    assert store.get_assistant_profile("writer-a") == before


def test_profile_isolated_per_assistant(tmp_path):
    store = MemoryStore(tmp_path)
    store.memorize("writer-a", "preference", "A 的偏好")
    store.memorize("writer-b", "preference", "B 的偏好")
    assert "A 的偏好" not in store.get_assistant_profile("writer-b")
    assert "B 的偏好" not in store.get_assistant_profile("writer-a")


# ---------- recall_trace ----------

def test_recall_trace_counts_and_text_equality(tmp_path):
    store = MemoryStore(tmp_path)
    store.memorize("writer-a", "preference", "偏好甲")
    store.memorize("writer-a", "style", "风格乙")
    store.memorize("writer-a", "article", "模型蒸馏入门 | data/articles/writer-a/x.md", session_id="s1")
    store.add_message("writer-a", "s1", "user", "聊聊模型蒸馏的工程实践")

    trace = store.recall_trace("writer-a", "模型蒸馏")
    assert trace.profile_entries == 2
    assert any("模型蒸馏入门" in title for title, _, _ in trace.article_hits)
    assert any("模型蒸馏" in content for content, _ in trace.message_hits)
    assert trace.degraded == []
    # recall 的返回必须与 trace.text 完全一致（recall 改为基于 trace 组装）
    assert trace.text == store.recall("writer-a", "模型蒸馏")
    assert "偏好甲" in trace.text and "风格乙" in trace.text


def test_recall_trace_empty_query_profile_only(tmp_path):
    store = MemoryStore(tmp_path)
    store.memorize("writer-a", "preference", "只看画像")
    trace = store.recall_trace("writer-a", "   ")
    assert trace.profile_entries == 1
    assert trace.article_hits == []
    assert trace.message_hits == []
    assert trace.text == store.recall("writer-a", "")


def test_recall_trace_cross_assistant_isolation(tmp_path):
    store = MemoryStore(tmp_path)
    store.memorize("writer-a", "preference", "A 的独家偏好")
    trace = store.recall_trace("writer-b", "独家偏好")
    assert "A 的独家偏好" not in trace.text


# ---------- 分页 clamp（phase7 P3-4 随行） ----------

def test_list_change_sets_page_size_clamped_at_memory_layer(tmp_path):
    store, project, document = _store_with_document(tmp_path)
    store.create_change_set_hunks(
        "writer-a", project.project_id,
        task_id="task-clamp", source="chat",
        documents=[{
            "document_id": document.document_id,
            "document_version": document.version,
            "hunks": [{"old_text": "开头段。", "new_text": "改后开头。"}],
        }],
    )
    result = store.list_change_sets_for_document(
        "writer-a", project.project_id, document.document_id, page=1, page_size=1000,
    )
    assert result["page_size"] == 100  # Memory 层收口，不再只依赖 API 层 le=100

    with pytest.raises(ValueError):
        store.list_change_sets_for_document(
            "writer-a", project.project_id, document.document_id, page=1, page_size=0,
        )


# ---------- 编码损坏的自愈途径（phase10 P2-6） ----------

def test_replace_profile_overwrites_unreadable_file(tmp_path):
    """白盒手改把 profile 存成非 UTF-8（如记事本 ANSI）后，PUT 应能覆盖修复而非 500。"""
    store = MemoryStore(tmp_path)
    store.memorize("writer-a", "preference", "原始条目")
    path = tmp_path / "assistants" / "writer-a" / "memory" / "profile.md"
    path.write_bytes("这不是 UTF-8 编码的画像内容".encode("gbk"))

    store.replace_assistant_profile("writer-a", "重新保存的画像内容")

    assert store.get_assistant_profile("writer-a") == "重新保存的画像内容"


def test_recalls_survive_corrupted_profile_file(tmp_path):
    """recall 六路分路降级不受编码损坏影响（既有契约，覆盖编码损坏这一形态）。"""
    store = MemoryStore(tmp_path)
    path = tmp_path / "assistants" / "writer-a" / "memory" / "profile.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xb2\xbb\xba\xcf\xb7\xa8")

    trace = store.recall_trace("writer-a", "任意查询")

    assert trace.degraded  # profile 路标记降级，不抛异常


def test_profile_replace_is_atomic_and_fails_without_touching_file(tmp_path, monkeypatch):
    """phase10 P3-12/P2-6：profile 整文替换走临时文件+os.replace，
    落盘失败不截断既有文件（GET 并发读也不会看到半程文件）。"""
    import os

    store = MemoryStore(tmp_path)
    store.replace_assistant_profile("writer-a", "旧画像内容")
    real_replace = os.replace

    def broken_replace(src, dst):
        if str(dst).endswith("profile.md"):
            raise OSError("replace failed")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError, match="replace failed"):
        store.replace_assistant_profile("writer-a", "新画像内容")
    monkeypatch.undo()

    assert store.get_assistant_profile("writer-a") == "旧画像内容"
    assert not list((tmp_path / "assistants" / "writer-a" / "memory").glob("*.tmp"))
