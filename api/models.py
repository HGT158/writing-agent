from __future__ import annotations

from pydantic import BaseModel, Field


class AssistantCreate(BaseModel):
    id: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    persona: str | None = Field(default=None, max_length=50_000)


class AssistantUpdate(BaseModel):
    """助手编辑（v1.28）：PATCH 部分更新语义，仅提供的字段生效。"""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, min_length=1, max_length=500)
    persona: str | None = Field(default=None, max_length=50_000)


class MemoryProfileUpdate(BaseModel):
    """助手长期画像整文替换（v1.30）：白盒语义，空白属显式清空。

    长度上限由 Memory 层（ASSISTANT_PROFILE_MAX_CHARS）校验，统一返回 400。
    """

    content: str


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


class DocumentRename(BaseModel):
    assistant_id: str
    relative_path: str = Field(min_length=1, max_length=1024)


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


class ChangeSetHunkAction(BaseModel):
    assistant_id: str


class LLMProviderCreate(BaseModel):
    """新增模型提供商（v1.31）：api_key 只落 llm_providers.json，不入 git。"""

    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=1, max_length=2_000)
    api_key: str = Field(min_length=1, max_length=4_096)
    models: list[str] = Field(min_length=1, max_length=100)
    temperature: float | None = Field(default=None, ge=0, le=2)


class LLMProviderSelect(BaseModel):
    """切换当前模型与提供商（v1.31）。"""

    provider_id: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)


class ProjectChatRequest(BaseModel):
    assistant_id: str
    message: str = Field(min_length=1, max_length=100_000)
    chat_session_id: str | None = None
    current_document_id: str | None = None
