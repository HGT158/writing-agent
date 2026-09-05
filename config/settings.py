"""全局配置：从 .env 加载，禁止硬编码密钥。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]

JOBS = [
    {
        "id": "daily-ai-news",
        "assistant_id": "default",
        "cron": "0 8 * * *",
        "task": "搜索今日 AI 新闻，生成技术日报并保存为 Markdown",
    },
]


@dataclass
class Settings:
    project_root: Path
    data_dir: Path
    skills_dir: Path
    mcp_config: Path
    openai_api_key: str
    openai_base_url: str
    model_name: str
    max_steps: int = 25
    run_lock_ttl_hours: float = 2.0
    llm_timeout_seconds: float = 120.0
    llm_stream_timeout_seconds: float = 300.0
    tool_timeout_seconds: float = 30.0
    api_max_request_body_mb: int = 520
    project_import_max_files: int = 5000
    project_import_max_total_mb: int = 512
    project_import_max_file_mb: int = 100
    # 项目聊天上下文预算（架构 §3.3）：预算设为 0 关闭压缩，恢复全量历史行为。
    chat_context_token_budget: int = 24000
    chat_context_keep_recent: int = 8
    chat_context_doc_max_chars: int = 12000
    # 聊天轮次终态选择性记忆沉淀（架构 §5.4 v1.30）：False 整体关闭沉淀，注入不受影响。
    chat_memory_consolidation: bool = True
    # 沉淀提取调用的独立超时（phase10 P2-7）：无界调用会拖住任务终态与助手锁。
    chat_memory_extraction_timeout_seconds: float = 30.0
    json_mode: bool = True  # LLM 是否优先用 response_format=json_object（失败自动回退纯文本+宽容解析）
    # Scheduler 长驻模式消费，见架构 §5.8。
    jobs: list[dict] = field(default_factory=list)


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    data_dir = PROJECT_ROOT / "data"
    return Settings(
        project_root=PROJECT_ROOT,
        data_dir=data_dir,
        skills_dir=PROJECT_ROOT / "skills",
        mcp_config=Path(os.environ.get("MCP_SERVERS_JSON", PROJECT_ROOT / "config" / "mcp_servers.json")),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        openai_base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
        model_name=os.environ.get("MODEL_NAME", "deepseek-chat"),
        max_steps=int(os.environ.get("MAX_STEPS", "25")),
        run_lock_ttl_hours=float(os.environ.get("RUN_LOCK_TTL", "2")),
        llm_timeout_seconds=float(os.environ.get("LLM_TIMEOUT_SECONDS", "120")),
        llm_stream_timeout_seconds=float(os.environ.get("LLM_STREAM_TIMEOUT_SECONDS", "300")),
        tool_timeout_seconds=float(os.environ.get("TOOL_TIMEOUT_SECONDS", "30")),
        api_max_request_body_mb=int(os.environ.get("API_MAX_REQUEST_BODY_MB", "520")),
        project_import_max_files=int(os.environ.get("PROJECT_IMPORT_MAX_FILES", "5000")),
        project_import_max_total_mb=int(os.environ.get("PROJECT_IMPORT_MAX_TOTAL_MB", "512")),
        project_import_max_file_mb=int(os.environ.get("PROJECT_IMPORT_MAX_FILE_MB", "100")),
        chat_context_token_budget=max(0, int(os.environ.get("CHAT_CONTEXT_TOKEN_BUDGET", "24000"))),
        chat_context_keep_recent=max(1, int(os.environ.get("CHAT_CONTEXT_KEEP_RECENT", "8"))),
        chat_context_doc_max_chars=max(0, int(os.environ.get("CHAT_CONTEXT_DOC_MAX_CHARS", "12000"))),
        chat_memory_consolidation=os.environ.get("CHAT_MEMORY_CONSOLIDATION", "true").lower()
        not in ("0", "false", "no"),
        chat_memory_extraction_timeout_seconds=float(
            os.environ.get("CHAT_MEMORY_EXTRACTION_TIMEOUT_SECONDS", "30")
        ),
        json_mode=os.environ.get("LLM_JSON_MODE", "true").lower() not in ("0", "false", "no"),
        jobs=[dict(job) for job in JOBS],
    )
