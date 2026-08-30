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

    def create(
        self,
        assistant_id: str,
        name: str,
        description: str = "",
        persona: str | None = None,
    ) -> Assistant:
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
        (directory / "persona.md").write_text(self._normalize_persona(persona), encoding="utf-8")
        self.reload()
        return self.get(assistant_id)

    def update(
        self,
        assistant_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        persona: str | None = None,
    ) -> Assistant:
        """部分更新助手定义（v1.28）：仅提供的字段生效，运行锁边界与删除一致。"""
        assistant = self.get(assistant_id)
        if name is None and description is None and persona is None:
            raise ValueError("没有提供任何需要更新的字段")
        mutation_task = f"assistant-update-{uuid.uuid4().hex[:12]}"
        self.store.acquire_lock(assistant_id, mutation_task)
        try:
            cfg_path = assistant.directory / "assistant.yaml"
            original_yaml = cfg_path.read_text(encoding="utf-8")
            raw = yaml.safe_load(original_yaml)
            if not isinstance(raw, dict):
                raw = {}
            persona_path = assistant.directory / raw.get("persona_file", "persona.md")
            original_persona = (
                persona_path.read_text(encoding="utf-8")
                if persona_path.is_file()
                else None
            )
            rollback_failed = False
            failure: Exception | None = None
            try:
                if persona is not None:
                    persona_path.write_text(self._normalize_persona(persona), encoding="utf-8")
                if name is not None or description is not None:
                    if name is not None:
                        raw["name"] = name
                    if description is not None:
                        raw["description"] = description
                    cfg_path.write_text(
                        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                        encoding="utf-8",
                    )
            except Exception as exc:
                # 磁盘写入失败：按原内容尽力回滚，不留半程状态；原始异常照常抛出。
                failure = exc
                try:
                    if persona is not None:
                        if original_persona is None:
                            persona_path.unlink(missing_ok=True)
                        else:
                            persona_path.write_text(original_persona, encoding="utf-8")
                    cfg_path.write_text(original_yaml, encoding="utf-8")
                except Exception:
                    rollback_failed = True
            self.reload()
            if rollback_failed:
                self.warnings.append(f"助手 {assistant_id} 更新写入失败且回滚未完成，请人工检查 {cfg_path}")
            if failure is not None:
                raise failure
            return self.get(assistant_id)
        finally:
            self.store.release_lock(assistant_id, mutation_task)

    @staticmethod
    def _normalize_persona(persona: str | None) -> str:
        """空白 persona 一律落为默认人设，不产生空系统提示词（架构 §4.2 v1.28）。"""
        text = (persona or "").strip()
        if not text:
            text = _DEFAULT_PERSONA
        return text + "\n"

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
