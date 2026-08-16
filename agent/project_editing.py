"""项目选区改写与项目聊天的结构化输入输出（v1.20：按文档分组的 hunk 输入）。"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from memory.projects import ChangeSetRecord


class ProjectEditHunk(BaseModel):
    old_text: str = Field(
        description="要替换的精确原文；仅当目标文档为空时可传空字符串以插入首稿"
    )
    new_text: str


class ProjectEditDocument(BaseModel):
    document_id: str
    document_version: int = Field(ge=1)
    hunks: list[ProjectEditHunk] = Field(min_length=1)


class ProjectEditBatch(BaseModel):
    documents: list[ProjectEditDocument] = Field(min_length=1)


@dataclass(frozen=True)
class ProjectChatResult:
    reply: str
    changes: list[ChangeSetRecord]


def hunk_count(batch: ProjectEditBatch) -> int:
    return sum(len(document.hunks) for document in batch.documents)


def edit_documents_payload(batch: ProjectEditBatch) -> list[dict]:
    """把校验后的批次转换为 MemoryStore.create_change_set_hunks 的输入。"""
    return [
        {
            "document_id": document.document_id,
            "document_version": document.document_version,
            "hunks": [
                {"old_text": hunk.old_text, "new_text": hunk.new_text}
                for hunk in document.hunks
            ],
        }
        for document in batch.documents
    ]
