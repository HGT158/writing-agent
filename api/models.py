from __future__ import annotations

from pydantic import BaseModel, Field


class AssistantCreate(BaseModel):
    id: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)


class AgentTaskRequest(BaseModel):
    assistant_id: str
    task: str = Field(min_length=1, max_length=100_000)
    session_id: str | None = None


class ProjectCreate(BaseModel):
    assistant_id: str
    name: str = Field(min_length=1, max_length=120)


class ProjectRename(BaseModel):
    assistant_id: str
    name: str = Field(min_length=1, max_length=120)


class DocumentSave(BaseModel):
    assistant_id: str
    content: str = Field(max_length=2_000_000)
    document_version: int = Field(ge=1)


class SelectionRewriteRequest(BaseModel):
    assistant_id: str
    start: int = Field(
        ge=0,
        description="Selection start in Unicode code points, inclusive.",
    )
    end: int = Field(
        ge=0,
        description="Selection end in Unicode code points, exclusive.",
    )
    selected_text: str
    instruction: str = Field(min_length=1)
    document_version: int = Field(ge=1)


class ChangeSetAction(BaseModel):
    assistant_id: str
    document_version: int = Field(ge=1)


class ChangeSetReject(BaseModel):
    assistant_id: str


class ProjectChatRequest(BaseModel):
    assistant_id: str
    message: str = Field(min_length=1)
    current_document_id: str | None = None
