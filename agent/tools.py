"""内置工具（架构 §5.4 / §8 注 2）：全部沙箱限制在 data/ 目录内。

- save_markdown：纯写文件，用于大纲/素材笔记等中间产物
- read_file：沙箱内读文件
- finalize_article：完成态文章收口 = 写 data/articles/<assistant_id>/<标题>-<时间戳>.md
  + 登记 articles 索引 + 触发 memorize(kind="article")
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from memory.errors import ResourceConflictError
from memory.store import MemoryStore
from pydantic import ValidationError

from .project_editing import ProjectEditBatch
from .schemas import ToolContext, ToolSpec


def _safe_resolve(data_dir: Path, rel_path: str) -> Path:
    """路径沙箱：拒绝逃逸 data/ 目录的相对路径。"""
    target = (data_dir / rel_path).resolve()
    root = data_dir.resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"路径越界：{rel_path} 不在 {root} 沙箱内")
    return target


def _reject_managed_assistant_write(data_dir: Path, target: Path) -> None:
    managed_root = (data_dir / "assistants").resolve()
    try:
        relative = target.relative_to(managed_root)
    except ValueError:
        return
    parts = relative.parts
    if len(parts) >= 3 and parts[1] == "projects":
        raise ValueError("save_markdown 禁止写入受管项目；请使用 change set/文档保存接口")
    raise ValueError("save_markdown 禁止写入受管助手数据")


def finalize_article_impl(
    store: MemoryStore,
    ctx: ToolContext,
    title: str,
    content: str,
) -> Path:
    """定稿核心逻辑（供 ToolSpec handler 与 done 节点直接复用，返回结构化 Path）。

    同分钟重名时追加 -2/-3 序号，不静默覆盖（审查 P2-14）。
    """
    title = title.strip()
    slug = re.sub(r'[\\/:*?"<>|\s]+', "-", title)[:40].strip("-") or "untitled"
    ts = datetime.now().strftime("%Y%m%d-%H%M")
    out_dir = Path(ctx.data_dir) / "articles" / ctx.assistant_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{slug}-{ts}.md"
    counter = 2
    while path.exists():
        path = out_dir / f"{slug}-{ts}-{counter}.md"
        counter += 1
    path.write_text(content, encoding="utf-8")
    store.memorize(ctx.assistant_id, "article", f"{title} | {path}", session_id=ctx.session_id)
    return path


def make_builtin_tools(data_dir: Path, store: MemoryStore) -> list[ToolSpec]:

    async def save_markdown(args: dict[str, Any], ctx: ToolContext) -> str:
        path = _safe_resolve(Path(ctx.data_dir), args["path"])
        _reject_managed_assistant_write(Path(ctx.data_dir), path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        return f"已保存：{path}"

    async def read_file(args: dict[str, Any], ctx: ToolContext) -> str:
        path = _safe_resolve(Path(ctx.data_dir), args["path"])
        if not path.exists():
            raise FileNotFoundError(f"文件不存在：{path}")
        return path.read_text(encoding="utf-8")[:2000]

    async def finalize_article(args: dict[str, Any], ctx: ToolContext) -> str:
        path = finalize_article_impl(store, ctx, args["title"], args["content"])
        return f"文章已定稿：{path}"

    return [
        ToolSpec(
            name="save_markdown",
            description="把 Markdown 内容写入 data/ 内的指定相对路径（中间产物：大纲、素材笔记等）",
            args_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对 data/ 的路径，如 notes/outline.md"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            handler=save_markdown,
        ),
        ToolSpec(
            name="read_file",
            description="读取 data/ 内指定相对路径的文件内容（截断 2000 字符）",
            args_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=read_file,
        ),
        ToolSpec(
            name="finalize_article",
            description="文章定稿收口：写入 data/articles/<当前助手>/ 并登记文章索引。每篇文章只调用一次。",
            args_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string", "description": "完整 Markdown 正文"},
                },
                "required": ["title", "content"],
            },
            handler=finalize_article,
            idempotent=False,  # 每次调用生成新文件并登记索引，失败不重试
        ),
    ]


def make_project_edit_tool(store: MemoryStore, project_id: str) -> ToolSpec:
    """创建仅绑定当前项目的编辑提案工具；工具只写 pending change set。"""

    async def propose(args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            batch = ProjectEditBatch.model_validate(args)
        except ValidationError as exc:
            raise ValueError("修改建议参数无效，请重试") from exc
        seen: set[str] = set()
        drafts: list[dict[str, object]] = []
        for item in batch.changes:
            if item.document_id in seen:
                raise ValueError("同一次编辑调用中每个文档只能出现一次")
            seen.add(item.document_id)
            document = store.get_document(ctx.assistant_id, project_id, item.document_id)
            if document.version != item.document_version:
                raise ResourceConflictError("版本冲突")
            content = document.content or ""
            if item.old_text == "":
                if content:
                    raise ResourceConflictError("非空文档不能使用空旧文本")
                start = 0
            else:
                start = content.find(item.old_text)
                if start < 0:
                    raise ResourceConflictError("旧文本不存在")
                if content.find(item.old_text, start + 1) >= 0:
                    raise ResourceConflictError("旧文本匹配多处，请提供更多上下文")
            drafts.append({
                "document_id": item.document_id,
                "start": start,
                "end": start + len(item.old_text),
                "original_text": item.old_text,
                "replacement_text": item.new_text,
                "base_version": item.document_version,
            })
        changes = store.create_change_sets(
            ctx.assistant_id,
            project_id,
            drafts,
            source="chat",
            session_id=ctx.session_id,
        )
        return json.dumps(
            {
                "change_set_ids": [item.change_set_id for item in changes],
                "count": len(changes),
            },
            ensure_ascii=False,
        )

    return ToolSpec(
        name="propose_project_edits",
        description=(
            "为项目文档提出精确修改建议；改写、增删或替换正文时必须调用。"
            "目标文档为空时，使用空 old_text 提交首稿。"
            "工具只生成待审核 diff，不会直接写文件。"
        ),
        args_schema=ProjectEditBatch.model_json_schema(),
        handler=propose,
        idempotent=False,
        captures_source=False,
    )
