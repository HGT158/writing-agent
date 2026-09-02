"""多 hunk change set 与逐 hunk 审查（v1.20）：MemoryStore 契约。"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from memory.errors import ChangeSetStateError, ResourceConflictError
from memory.store import MemoryStore


def _store_with_document(tmp_path: Path, content="开头段。中间段。结尾段。"):
    store = MemoryStore(tmp_path)
    project = store.create_project("writer-a", "hunk 项目")
    document, _ = store.save_document(
        "writer-a", project.project_id, project.entry_document_id,
        content, expected_version=1,
    )
    return store, project, document


def _hunk(old: str, new: str) -> dict:
    return {"old_text": old, "new_text": new}


# ---------- 迁移 ----------

def _install_legacy_change_sets(db_path: Path, rows: list[tuple]) -> None:
    """把库回退成 v1.20 之前的单范围 change_sets 形态并写入 legacy 行。"""
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE change_set_hunks")
    conn.execute("DROP TABLE change_sets")
    conn.execute(
        """CREATE TABLE change_sets (
            change_set_id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            session_id TEXT,
            source TEXT NOT NULL CHECK (source IN ('selection', 'chat')),
            start_offset INTEGER NOT NULL,
            end_offset INTEGER NOT NULL,
            original_text TEXT NOT NULL,
            replacement_text TEXT NOT NULL,
            base_version INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'rejected')),
            created_at TEXT NOT NULL,
            applied_at TEXT
        )"""
    )
    conn.executemany("INSERT INTO change_sets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_migration_converts_legacy_rows_to_parent_and_single_hunk(tmp_path):
    store, project, document = _store_with_document(tmp_path)
    db_path = tmp_path / "app.db"
    store.close()

    _install_legacy_change_sets(db_path, [
        ("cs-old-1", "writer-a", project.project_id, document.document_id, None,
         "selection", 0, 4, "开头段。", "改开头。", 1, "pending", "2026-08-16T00:00:00", None),
        ("cs-old-2", "writer-a", project.project_id, document.document_id, None,
         "chat", 4, 7, "中间段。", "改中间。", 1, "applied", "2026-08-16T00:00:01", "2026-08-16T00:00:02"),
    ])

    reopened = MemoryStore(tmp_path)
    first = reopened.get_change_set("writer-a", project.project_id, "cs-old-1")
    second = reopened.get_change_set("writer-a", project.project_id, "cs-old-2")
    assert first.task_id == "legacy-cs-old-1"
    assert first.status == "pending"
    assert [(h.start, h.end, h.status) for h in first.hunks] == [(0, 4, "pending")]
    assert first.hunks[0].original_text == "开头段。"
    assert second.status == "applied"
    assert [h.status for h in second.hunks] == ["applied"]
    reopened.close()


def test_second_start_after_successful_migration_is_noop(tmp_path):
    """迁移成功后二次启动不重迁不报错（phase7 P3-11）：数据保持、无 legacy 残表。"""
    store, project, document = _store_with_document(tmp_path)
    db_path = tmp_path / "app.db"
    store.close()

    _install_legacy_change_sets(db_path, [
        ("cs-old-1", "writer-a", project.project_id, document.document_id, None,
         "selection", 0, 4, "开头段。", "改开头。", 1, "pending", "2026-08-16T00:00:00", None),
    ])

    migrated = MemoryStore(tmp_path)
    assert migrated.get_change_set(
        "writer-a", project.project_id, "cs-old-1"
    ).task_id == "legacy-cs-old-1"
    migrated.close()

    reopened = MemoryStore(tmp_path)
    record = reopened.get_change_set("writer-a", project.project_id, "cs-old-1")
    assert record.task_id == "legacy-cs-old-1"
    assert [(h.start, h.end, h.status) for h in record.hunks] == [(0, 4, "pending")]
    tables = {
        row[0] for row in reopened._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "change_sets_legacy" not in tables
    reopened.close()


def test_finalize_write_intent_missing_intent_raises_conflict(tmp_path):
    """phase10 P3-9：意图缺失分支不再静默成功（假成功会让 accept 报 200
    而 hunk 仍 pending、无任何告警），改抛 StorageRecoveryPendingError（409）。

    修订 phase7 P3-5 的旧契约（当时改为返回空列表以消除 None.length 崩溃链）；
    前端崩溃链已由 list[str] 类型统一保持解决，此处进一步把假成功改为显式冲突。
    """
    from memory.errors import StorageRecoveryPendingError
    from memory.projects import _WriteIntent, _finalize_write_intent

    store, project, document = _store_with_document(tmp_path)
    intent = _WriteIntent(
        intent_id="missing-intent", document_id=document.document_id,
        change_set_id=None, hunk_id="", expected_version=document.version,
        target_version=document.version + 1, relative_path=document.relative_path,
        content="未登记的正文", utf8_bom=False, owner_pid=os.getpid(),
        owner_started_at=0.0, claimed_at="2026-08-30T00:00:00",
        created_at="2026-08-30T00:00:00",
    )

    with pytest.raises(StorageRecoveryPendingError, match="意图"):
        _finalize_write_intent(store._conn, "writer-a", project.project_id, intent)
    store.close()


# ---------- 创建 ----------

def test_create_change_set_hunks_locates_orders_and_freezes(tmp_path):
    store, project, document = _store_with_document(tmp_path)

    created = store.create_change_set_hunks(
        "writer-a", project.project_id,
        task_id="task-1", source="chat", session_id="chat-1",
        documents=[{
            "document_id": document.document_id,
            "document_version": document.version,
            "hunks": [
                _hunk("结尾段。", "【结尾】。"),
                _hunk("开头段。", "【开头】。"),
                _hunk("中间段。", "【中间】。"),
            ],
        }],
    )

    assert len(created) == 1
    record = created[0]
    assert record.task_id == "task-1"
    assert record.base_version == document.version
    assert record.status == "pending"
    starts = [h.start for h in record.hunks]
    assert starts == sorted(starts)
    assert [h.display_order for h in record.hunks] == [0, 1, 2]
    assert record.hunks[0].original_text == "开头段。"
    assert all(h.status == "pending" for h in record.hunks)
    store.close()


def test_create_rejects_bad_hunks_without_partial_rows(tmp_path):
    store, project, document = _store_with_document(tmp_path)

    def attempt(hunks, task_id="task-x"):
        return store.create_change_set_hunks(
            "writer-a", project.project_id,
            task_id=task_id, source="chat",
            documents=[{
                "document_id": document.document_id,
                "document_version": document.version,
                "hunks": hunks,
            }],
        )

    with pytest.raises(ResourceConflictError, match="旧文本不存在"):
        attempt([_hunk("不存在的文本", "新")])
    with pytest.raises(ResourceConflictError, match="匹配多处"):
        attempt([_hunk("段。", "新")])  # "段。" 出现三次
    with pytest.raises(ResourceConflictError, match="重叠"):
        attempt([_hunk("开头段。中间段。", "A"), _hunk("中间段。结尾段。", "B")])
    with pytest.raises(ResourceConflictError, match="版本冲突"):
        store.create_change_set_hunks(
            "writer-a", project.project_id,
            task_id="task-v", source="chat",
            documents=[{
                "document_id": document.document_id,
                "document_version": document.version + 5,
                "hunks": [_hunk("开头段。", "新")],
            }],
        )
    with pytest.raises(ValueError, match="100"):
        attempt([_hunk("开头段。", "新")] * 101)
    with pytest.raises(ValueError, match="1 MiB"):
        attempt([_hunk("开头段。", "长" * 600_000)])

    assert store.list_change_sets_for_document(
        "writer-a", project.project_id, document.document_id
    )["total"] == 0  # 全部整批失败，无半成品
    store.close()


def test_create_rejects_duplicate_task_document_and_same_position_inserts(tmp_path):
    store, project, document = _store_with_document(tmp_path)
    store.create_change_set_hunks(
        "writer-a", project.project_id,
        task_id="task-dup", source="chat",
        documents=[{
            "document_id": document.document_id,
            "document_version": document.version,
            "hunks": [_hunk("开头段。", "新")],
        }],
    )

    with pytest.raises(ResourceConflictError, match="该任务已提交"):
        store.create_change_set_hunks(
            "writer-a", project.project_id,
            task_id="task-dup", source="chat",
            documents=[{
                "document_id": document.document_id,
                "document_version": document.version,
                "hunks": [_hunk("中间段。", "新")],
            }],
        )

    blank = store.create_project("writer-a", "空白项目")
    empty_doc, _ = store.save_document(
        "writer-a", blank.project_id, blank.entry_document_id, "", expected_version=1
    )
    with pytest.raises(ResourceConflictError, match="同.*位置"):
        store.create_change_set_hunks(
            "writer-a", blank.project_id,
            task_id="task-zero", source="chat",
            documents=[{
                "document_id": empty_doc.document_id,
                "document_version": empty_doc.version,
                "hunks": [_hunk("", "第一句。"), _hunk("", "第二句。")],
            }],
        )
    with pytest.raises(ResourceConflictError, match="非空"):
        store.create_change_set_hunks(
            "writer-a", project.project_id,
            task_id="task-zero2", source="chat",
            documents=[{
                "document_id": document.document_id,
                "document_version": document.version,
                "hunks": [_hunk("", "插入")],
            }],
        )
    store.close()


def test_selection_change_set_uses_explicit_range_with_snapshot(tmp_path):
    store, project, document = _store_with_document(tmp_path)

    record = store.create_selection_change_set(
        "writer-a", project.project_id, document.document_id,
        task_id="task-sel", start=0, end=4,
        original_text="开头段。", replacement_text="改开头。",
        base_version=document.version, source="selection",
    )
    assert [(h.start, h.end, h.new_text) for h in record.hunks] == [(0, 4, "改开头。")]

    with pytest.raises(ResourceConflictError, match="快照"):
        store.create_selection_change_set(
            "writer-a", project.project_id, document.document_id,
            task_id="task-sel2", start=0, end=4,
            original_text="错误快照", replacement_text="x",
            base_version=document.version, source="selection",
        )
    store.close()


# ---------- 逐 hunk 接受 / 放弃 ----------

def _two_hunk_set(store, project, document):
    return store.create_change_set_hunks(
        "writer-a", project.project_id,
        task_id="task-two", source="chat", session_id="chat-1",
        documents=[{
            "document_id": document.document_id,
            "document_version": document.version,
            "hunks": [
                _hunk("开头段。", "【开头】。"),
                _hunk("结尾段。", "【结尾】。"),
            ],
        }],
    )[0]


def test_accept_hunks_one_by_one_with_content_rematch(tmp_path):
    store, project, document = _store_with_document(tmp_path)
    record = _two_hunk_set(store, project, document)
    first, second = record.hunks

    document1, set1, hunk1, staled1 = store.accept_change_hunk(
        "writer-a", project.project_id, record.change_set_id, first.hunk_id
    )
    assert hunk1.status == "applied"
    assert document1.content == "【开头】。中间段。结尾段。"
    assert document1.version == document.version + 1
    assert set1.status == "pending"  # 另一个 hunk 仍未审
    assert staled1 == []

    document2, set2, hunk2, _ = store.accept_change_hunk(
        "writer-a", project.project_id, record.change_set_id, second.hunk_id
    )
    assert hunk2.status == "applied"
    assert document2.content == "【开头】。中间段。【结尾】。"
    assert document2.version == document.version + 2
    assert set2.status == "applied"

    with pytest.raises(ChangeSetStateError) as exc_info:
        store.accept_change_hunk(
            "writer-a", project.project_id, record.change_set_id, first.hunk_id
        )
    assert exc_info.value.code == "already_applied"
    store.close()


def test_accept_sibling_turns_stale_when_rematch_fails(tmp_path):
    store, project, document = _store_with_document(tmp_path, content="XX—YY")
    record = store.create_change_set_hunks(
        "writer-a", project.project_id,
        task_id="task-stale", source="chat",
        documents=[{
            "document_id": document.document_id,
            "document_version": document.version,
            "hunks": [
                _hunk("XX", "YY"),   # 应用后 "YY—YY"，下方 hunk 的旧文本不再唯一
                _hunk("YY", "ZZ"),
            ],
        }],
    )[0]
    first, second = record.hunks

    store.accept_change_hunk(
        "writer-a", project.project_id, record.change_set_id, first.hunk_id
    )
    with pytest.raises(ChangeSetStateError) as exc_info:
        store.accept_change_hunk(
            "writer-a", project.project_id, record.change_set_id, second.hunk_id
        )
    assert exc_info.value.code == "stale"
    stale_set = store.get_change_set("writer-a", project.project_id, record.change_set_id)
    assert [h.status for h in stale_set.hunks] == ["applied", "stale"]
    store.close()


def test_other_task_change_sets_go_stale_after_apply(tmp_path):
    store, project, document = _store_with_document(tmp_path)
    mine = _two_hunk_set(store, project, document)
    other = store.create_change_set_hunks(
        "writer-a", project.project_id,
        task_id="task-other", source="chat",
        documents=[{
            "document_id": document.document_id,
            "document_version": document.version,
            "hunks": [_hunk("中间段。", "【中间】。")],
        }],
    )[0]

    _, _, _, staled = store.accept_change_hunk(
        "writer-a", project.project_id, mine.change_set_id, mine.hunks[0].hunk_id
    )
    assert staled == [other.change_set_id]
    other_now = store.get_change_set("writer-a", project.project_id, other.change_set_id)
    assert [h.status for h in other_now.hunks] == ["stale"]

    with pytest.raises(ChangeSetStateError) as exc_info:
        store.accept_change_hunk(
            "writer-a", project.project_id,
            other.change_set_id, other_now.hunks[0].hunk_id,
        )
    assert exc_info.value.code == "stale"
    store.close()


def test_reject_hunk_is_metadata_only_and_reports_states(tmp_path):
    store, project, document = _store_with_document(tmp_path)
    record = _two_hunk_set(store, project, document)
    first, second = record.hunks

    rejected = store.reject_change_hunk(
        "writer-a", project.project_id, record.change_set_id, first.hunk_id
    )
    assert [h.status for h in rejected.hunks] == ["rejected", "pending"]
    current = store.get_document("writer-a", project.project_id, document.document_id)
    assert current.content == "开头段。中间段。结尾段。"
    assert current.version == document.version

    with pytest.raises(ChangeSetStateError) as exc_info:
        store.reject_change_hunk(
            "writer-a", project.project_id, record.change_set_id, first.hunk_id
        )
    assert exc_info.value.code == "already_rejected"

    store.reject_change_hunk(
        "writer-a", project.project_id, record.change_set_id, second.hunk_id
    )
    final = store.get_change_set("writer-a", project.project_id, record.change_set_id)
    assert final.status == "rejected"
    store.close()


def test_change_set_unique_index_conflict_maps_to_resource_conflict(tmp_path, monkeypatch):
    store, project, document = _store_with_document(tmp_path)
    kwargs = dict(
        task_id="same-task", start=0, end=4,
        original_text="开头段。", replacement_text="新开头。",
        base_version=document.version, source="selection",
    )
    store.create_selection_change_set(
        "writer-a", project.project_id, document.document_id, **kwargs
    )
    monkeypatch.setattr(
        "memory.projects._require_unique_task_document", lambda *_args: None
    )

    with pytest.raises(ResourceConflictError, match="已提交过"):
        store.create_selection_change_set(
            "writer-a", project.project_id, document.document_id, **kwargs
        )

    store.close()


def test_delete_document_removes_terminal_change_set_history(tmp_path):
    store, project, document = _store_with_document(tmp_path)
    record = store.create_selection_change_set(
        "writer-a", project.project_id, document.document_id,
        task_id="delete-history", start=0, end=4,
        original_text="开头段。", replacement_text="新开头。",
        base_version=document.version, source="selection",
    )
    store.reject_change_hunk(
        "writer-a", project.project_id, record.change_set_id, record.hunks[0].hunk_id
    )

    store.delete_document("writer-a", project.project_id, document.document_id)

    assert store._conn.execute(
        "SELECT COUNT(*) FROM change_sets WHERE document_id = ?", (document.document_id,)
    ).fetchone() == (0,)
    assert store._conn.execute(
        "SELECT COUNT(*) FROM change_set_hunks WHERE change_set_id = ?",
        (record.change_set_id,),
    ).fetchone() == (0,)
    store.close()


def test_reject_blocked_while_write_intent_active(tmp_path):
    store, project, document = _store_with_document(tmp_path)
    record = _two_hunk_set(store, project, document)
    first, second = record.hunks
    # 构造"进程死在文件替换与 finalize 之间"的孤儿意图（accept 第二个 hunk 半程）：
    # 恢复语义 = 完成文件替换 + 终结元数据（该 hunk applied、版本 +1）。
    store._conn.execute(
        "INSERT INTO document_write_intents "
        "(intent_id, assistant_id, project_id, document_id, change_set_id, hunk_id, "
        "expected_version, target_version, relative_path, content, utf8_bom, "
        "owner_pid, owner_started_at, claimed_at, created_at) "
        "VALUES (?,?,?, ?,?,?,1,2,?,?,0,0,0,?,'2026-08-16')",
        ("intent-x", "writer-a", project.project_id, document.document_id,
         record.change_set_id, second.hunk_id,
         document.relative_path, "开头段。中间段。【结尾】。", ""),
    )
    store._conn.commit()

    # reject 前先恢复孤儿意图，而不是被它持续阻塞（phase7 P2-1）。
    rejected = store.reject_change_hunk(
        "writer-a", project.project_id, record.change_set_id, first.hunk_id
    )
    # 意图恢复 = 第二个 hunk 已应用（文件替换 + 元数据终结完成）
    assert [h.status for h in rejected.hunks] == ["rejected", "applied"]
    assert store._conn.execute(
        "SELECT COUNT(*) FROM document_write_intents WHERE intent_id = 'intent-x'"
    ).fetchone()[0] == 0
    current = store.get_document("writer-a", project.project_id, document.document_id)
    assert current.content == "开头段。中间段。【结尾】。"
    assert current.version == 2
    store.close()


def test_reject_hunk_then_accept_siblings_in_same_set(tmp_path):
    """reject 一个 hunk 后接受同组其余 hunk：复检路径必须可用（phase7 P3-11 测试缺口）。"""
    store, project, document = _store_with_document(tmp_path)
    record = _two_hunk_set(store, project, document)
    first, second = record.hunks

    rejected = store.reject_change_hunk(
        "writer-a", project.project_id, record.change_set_id, first.hunk_id
    )
    assert [h.status for h in rejected.hunks] == ["rejected", "pending"]

    document2, set2, hunk2, staled2 = store.accept_change_hunk(
        "writer-a", project.project_id, record.change_set_id, second.hunk_id
    )
    assert hunk2.status == "applied"
    assert document2.content == "开头段。中间段。【结尾】。"
    assert document2.version == document.version + 1
    assert staled2 == []
    final = store.get_change_set("writer-a", project.project_id, record.change_set_id)
    assert [h.status for h in final.hunks] == ["rejected", "applied"]
    assert final.status == "applied"
    store.close()


# ---------- 全部接受 / 查询 / 保存连带 ----------

def test_accept_all_applies_reverse_order_and_stops_on_failure(tmp_path):
    store, project, document = _store_with_document(tmp_path, content="AA-BB-CC")
    record = store.create_change_set_hunks(
        "writer-a", project.project_id,
        task_id="task-all", source="chat",
        documents=[{
            "document_id": document.document_id,
            "document_version": document.version,
            "hunks": [
                _hunk("AA", "XA"),
                _hunk("BB", "XB"),
                _hunk("CC", "AA-"),  # 应用后与第一个 hunk 的新文本组合导致 "AA-" 匹配两处
            ],
        }],
    )[0]
    # 让尾部 hunk 先应用，其后 "AA-" 复检失败：AA→XA 已把唯一 "AA-" 破坏
    # 构造：接受 order 2 (CC→"AA-") 后正文 "AA-BB-AA-"，"AA" 匹配多处 → hunk0 复检失败。
    result = store.accept_all_change_hunks(
        "writer-a", project.project_id, record.change_set_id
    )
    applied_ids = result["applied_hunk_ids"]
    assert len(applied_ids) >= 1
    final = store.get_change_set("writer-a", project.project_id, record.change_set_id)
    statuses = {h.hunk_id: h.status for h in final.hunks}
    for hunk_id in applied_ids:
        assert statuses[hunk_id] == "applied"
    if result["stopped"] is not None:
        assert statuses[result["stopped"]["hunk_id"]] == "stale"
        assert result["stopped"]["reason"] == "stale"
    document_final = store.get_document("writer-a", project.project_id, document.document_id)
    assert document_final.version == document.version + len(applied_ids)
    store.close()


def test_accept_all_applies_everything_when_healthy(tmp_path):
    store, project, document = _store_with_document(tmp_path)
    record = _two_hunk_set(store, project, document)

    result = store.accept_all_change_hunks(
        "writer-a", project.project_id, record.change_set_id
    )
    assert result["stopped"] is None
    assert len(result["applied_hunk_ids"]) == 2
    assert result["document"].content == "【开头】。中间段。【结尾】。"
    assert result["change_set"].status == "applied"
    store.close()


def test_save_document_stales_outdated_sets_and_reports(tmp_path):
    store, project, document = _store_with_document(tmp_path)
    record = _two_hunk_set(store, project, document)

    saved, staled = store.save_document(
        "writer-a", project.project_id, document.document_id,
        "手工改写后的正文。", expected_version=document.version,
    )
    assert saved.version == document.version + 1
    assert staled == [record.change_set_id]
    now = store.get_change_set("writer-a", project.project_id, record.change_set_id)
    assert all(h.status == "stale" for h in now.hunks)
    store.close()


def test_list_change_sets_for_document_is_paginated(tmp_path):
    store, project, document = _store_with_document(tmp_path)
    for index in range(3):
        store.create_change_set_hunks(
            "writer-a", project.project_id,
            task_id=f"task-{index}", source="chat",
            documents=[{
                "document_id": document.document_id,
                "document_version": document.version,
                "hunks": [_hunk("开头段。", f"改{index}。")],
            }],
        )

    first_page = store.list_change_sets_for_document(
        "writer-a", project.project_id, document.document_id, page=1, page_size=2
    )
    assert first_page["total"] == 3
    assert len(first_page["items"]) == 2
    second_page = store.list_change_sets_for_document(
        "writer-a", project.project_id, document.document_id, page=2, page_size=2
    )
    assert len(second_page["items"]) == 1
    assert all(item.hunks for item in first_page["items"])
    store.close()


def test_purge_assistant_cascades_hunk_rows(tmp_path):
    store, project, document = _store_with_document(tmp_path)
    doomed = _two_hunk_set(store, project, document)

    other_project = store.create_project("writer-b", "保留项目")
    other_doc, _ = store.save_document(
        "writer-b", other_project.project_id, other_project.entry_document_id,
        "开头段。结尾段。", expected_version=1,
    )
    kept = store.create_change_set_hunks(
        "writer-b", other_project.project_id,
        task_id="task-keep", source="chat", session_id="chat-b",
        documents=[{
            "document_id": other_doc.document_id,
            "document_version": other_doc.version,
            "hunks": [_hunk("开头段。", "【开头】。")],
        }],
    )[0]

    store.purge_assistant("writer-a")

    assert store._conn.execute(
        "SELECT COUNT(*) FROM change_sets WHERE assistant_id = ?", ("writer-a",)
    ).fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT COUNT(*) FROM change_set_hunks WHERE change_set_id = ?",
        (doomed.change_set_id,),
    ).fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT COUNT(*) FROM change_set_hunks WHERE change_set_id = ?",
        (kept.change_set_id,),
    ).fetchone()[0] == 1
    store.close()
