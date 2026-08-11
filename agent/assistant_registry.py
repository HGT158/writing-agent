"""助手注册表（架构 §4）：扫描 data/assistants/ 加载助手定义，文件即配置。"""
from __future__ import annotations

import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from memory.store import MemoryStore

_DEFAULT_PERSONA = "你是一名严谨的中文写作助手，注重事实准确、逻辑清晰、引用可追溯。"


@dataclass
class Assistant:
    id: str
    name: str
    description: str
    skills: list[str] | None  # None = 全部可用
    persona: str
    directory: Path


class AssistantRegistry:
    def __init__(self, data_dir: Path, store: MemoryStore) -> None:
        self.root = data_dir / "assistants"
        self.archive_root = data_dir / "archive"
        self.store = store
        self._assistants: dict[str, Assistant] = {}
        self.warnings: list[str] = []
        self.reload()
        if "default" not in self._assistants:
            self.create("default", "通用写作助手", "默认兜底助手")

    def reload(self) -> None:
        self._assistants.clear()
        self.warnings.clear()
        self.root.mkdir(parents=True, exist_ok=True)
        for child in sorted(self.root.iterdir()):
            cfg = child / "assistant.yaml"
            if not child.is_dir() or not cfg.exists():
                continue
            try:
                raw = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
                persona_file = raw.get("persona_file", "persona.md")
                persona_path = child / persona_file
                persona = (
                    persona_path.read_text(encoding="utf-8").strip()
                    if persona_path.exists()
                    else _DEFAULT_PERSONA
                )
                self._assistants[raw["id"]] = Assistant(
                    id=raw["id"],
                    name=raw.get("name", raw["id"]),
                    description=raw.get("description", ""),
                    skills=raw.get("skills"),
                    persona=persona,
                    directory=child,
                )
            except Exception as exc:  # 目录损坏不阻断启动（架构 §9）
                self.warnings.append(f"助手目录 {child.name} 解析失败：{exc}")

    def get(self, assistant_id: str) -> Assistant:
        if assistant_id not in self._assistants:
            available = ", ".join(sorted(self._assistants)) or "(空)"
            raise KeyError(f"助手不存在：{assistant_id}。可用助手：{available}")
        return self._assistants[assistant_id]

    def list(self) -> list[Assistant]:
        return list(self._assistants.values())

    _ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$")

    def create(self, assistant_id: str, name: str, description: str = "") -> Assistant:
        # id 格式校验：防路径穿越写出 data/ 之外（审查 P1-7）
        if not self._ID_RE.match(assistant_id):
            raise ValueError(f"助手 id 非法：{assistant_id!r}（须匹配 ^[a-z0-9][a-z0-9_-]{{0,49}}$）")
        directory = (self.root / assistant_id).resolve()
        if directory.parent != self.root.resolve():
            raise ValueError(f"助手目录越界：{directory}")
        if directory.exists():
            raise ValueError(f"助手已存在：{assistant_id}")
        (directory / "memory").mkdir(parents=True, exist_ok=True)
        (directory / "assistant.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": assistant_id,
                    "name": name,
                    "description": description,
                    "created_at": datetime.now().strftime("%Y-%m-%d"),
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (directory / "persona.md").write_text(_DEFAULT_PERSONA + "\n", encoding="utf-8")
        self.reload()
        return self.get(assistant_id)

    def delete(self, assistant_id: str, purge: bool = False) -> Path:
        """默认归档（目录移到 data/archive/，SQL 行保留不可见）；purge=True 级联清理。"""
        assistant = self.get(assistant_id)
        if assistant_id == "default":
            raise ValueError("default 助手不可删除")
        mutation_task = f"assistant-delete-{uuid.uuid4().hex[:12]}"
        self.store.acquire_lock(assistant_id, mutation_task)
        self.archive_root.mkdir(parents=True, exist_ok=True)
        target = self.archive_root / f"{assistant_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        try:
            shutil.move(str(assistant.directory), str(target))
            if purge:
                self.store.purge_assistant(assistant_id, owner_task_id=mutation_task)
                shutil.rmtree(target)
                shutil.rmtree(self.archive_root / "projects" / assistant_id, ignore_errors=True)
            self.reload()
            return target
        finally:
            self.store.release_lock(assistant_id, mutation_task)
