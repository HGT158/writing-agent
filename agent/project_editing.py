"""项目选区改写与项目聊天的结构化输入输出。"""
from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel, Field

from memory.projects import ChangeSetRecord

from .llm import extract_json


class ChatChange(BaseModel):
    document_id: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    original_text: str
    replacement_text: str
    document_version: int = Field(ge=1)


class ChatPayload(BaseModel):
    reply: str = ""
    changes: list[ChatChange] = Field(default_factory=list)


@dataclass(frozen=True)
class ProjectChatResult:
    reply: str
    changes: list[ChangeSetRecord]


def parse_chat_payload(text: str) -> ChatPayload:
    return ChatPayload.model_validate(json.loads(extract_json(text)))

