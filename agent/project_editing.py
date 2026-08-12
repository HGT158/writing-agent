"""项目选区改写与项目聊天的结构化输入输出。"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from memory.projects import ChangeSetRecord


class ProjectEditChange(BaseModel):
    document_id: str
    old_text: str = Field(
        description="要替换的精确原文；仅当目标文档为空时可传空字符串以插入首稿"
    )
    new_text: str
    document_version: int = Field(ge=1)


class ProjectEditBatch(BaseModel):
    changes: list[ProjectEditChange] = Field(min_length=1)


@dataclass(frozen=True)
class ProjectChatResult:
    reply: str
    changes: list[ChangeSetRecord]
