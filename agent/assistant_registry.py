"""助手注册表（架构 §4）：扫描 data/assistants/ 加载助手定义，文件即配置。"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from memory.store import MemoryStore

logger = logging.getLogger(__name__)

_DEFAULT_PERSONA = "你是一名严谨的中文写作助手，注重事实准确、逻辑清晰、引用可追溯。"

# persona 上限与 API 层（api/models.py）同口径；下沉 registry 单点收口后
# CLI 直通路径同样受限（phase10 P3-2）。
PERSONA_MAX_CHARS = 50_000


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
        # 进程内互斥：API 写端经 asyncio.to_thread 在工作线程执行，与读端并发
        # 时 reload 不得暴露清空窗口（phase10 P2-4）；RLock 允许持锁调 reload。
        self._lock = threading.RLock()
        self.warnings: list[str] = []
        self.reload()
        if "default" not in self._assistants:
            self.create("default", "通用写作助手", "默认兜底助手")

    def reload(self) -> None:
        """整体重建后单次替换，读端不会命中「先清空再逐个重建」的瞬时 404 窗口。"""
        with self._lock:
            self.warnings.clear()
            self.root.mkdir(parents=True, exist_ok=True)
            assistants: dict[str, Assistant] = {}
            for child in sorted(self.root.iterdir()):
                cfg = child / "assistant.yaml"
                if not child.is_dir() or not cfg.exists():
                    continue
                try:
                    raw = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
                    persona_file = raw.get("persona_file", "persona.md")
                    persona_path = self._resolve_persona_path(child, persona_file)
                    persona = (
                        persona_path.read_text(encoding="utf-8").strip()
                        if persona_path.exists()
                        else _DEFAULT_PERSONA
                    )
                    assistants[raw["id"]] = Assistant(
                        id=raw["id"],
                        name=raw.get("name", raw["id"]),
                        description=raw.get("description", ""),
                        skills=raw.get("skills"),
                        persona=persona,
                        directory=child,
                    )
                except Exception as exc:  # 目录损坏不阻断启动（架构 §9）
                    self.warnings.append(f"助手目录 {child.name} 解析失败：{exc}")
            self._assistants = assistants

    def get(self, assistant_id: str) -> Assistant:
        # 单次查找：与 reload 的整体替换配合，不存在「先判在再取」的竞态
        assistant = self._assistants.get(assistant_id)
        if assistant is None:
            available = ", ".join(sorted(self._assistants)) or "(空)"
            raise KeyError(f"助手不存在：{assistant_id}。可用助手：{available}")
        return assistant

    def list(self) -> list[Assistant]:
        return list(self._assistants.values())

    _ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$")

    @staticmethod
    def _require_display_name(name: str) -> str:
        """显示名收口：空白一律拒绝，CLI 与 API 同口径（phase10 P2-5）。"""
        if not (name or "").strip():
            raise ValueError("显示名不能为空白")
        return name

    @staticmethod
    def _resolve_persona_path(directory: Path, persona_file: str) -> Path:
        """persona_file 只允许指向助手目录之内：绝对路径整体替换与 `../` 越界
        一律按损坏配置拒绝（phase10 P3-4）。"""
        path = (directory / persona_file).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ValueError(
                f"persona_file 指向助手目录之外，按损坏配置拒绝：{persona_file!r}"
            )
        return path

    @classmethod
    def _require_persona_size(cls, persona: str | None) -> None:
        if persona is not None and len(persona) > PERSONA_MAX_CHARS:
            raise ValueError(f"persona 超过上限 {PERSONA_MAX_CHARS} 字符")

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        """同目录临时文件 + fsync + os.replace 原子写：进程中断或断电不留截断/空文件
        （phase10 P2-3，fsync 为复审补强，对齐 llm_providers.json 写入标准）。"""
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

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
        self._require_display_name(name)
        self._require_persona_size(persona)
        directory = (self.root / assistant_id).resolve()
        if directory.parent != self.root.resolve():
            raise ValueError(f"助手目录越界：{directory}")
        with self._lock:
            if directory.exists():
                raise ValueError(f"助手已存在：{assistant_id}")
            (directory / "memory").mkdir(parents=True, exist_ok=True)
            self._atomic_write_text(
                directory / "assistant.yaml",
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
            )
            self._atomic_write_text(
                directory / "persona.md", self._normalize_persona(persona)
            )
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
        if name is not None:
            self._require_display_name(name)
        self._require_persona_size(persona)
        mutation_task = f"assistant-update-{uuid.uuid4().hex[:12]}"
        self.store.acquire_lock(assistant_id, mutation_task)
        try:
            with self._lock:
                cfg_path = assistant.directory / "assistant.yaml"
                original_yaml = cfg_path.read_text(encoding="utf-8")
                raw = yaml.safe_load(original_yaml)
                if not isinstance(raw, dict):
                    raw = {}
                persona_path = self._resolve_persona_path(
                    assistant.directory, raw.get("persona_file", "persona.md")
                )
                original_persona = (
                    persona_path.read_text(encoding="utf-8")
                    if persona_path.is_file()
                    else None
                )
                rollback_failed = False
                failure: Exception | None = None
                try:
                    if persona is not None:
                        self._atomic_write_text(
                            persona_path, self._normalize_persona(persona)
                        )
                    if name is not None or description is not None:
                        if name is not None:
                            raw["name"] = name
                        if description is not None:
                            raw["description"] = description
                        self._atomic_write_text(
                            cfg_path,
                            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                        )
                except Exception as exc:
                    # 磁盘写入失败：按原内容尽力回滚，不留半程状态；原始异常照常抛出。
                    failure = exc
                    try:
                        if persona is not None:
                            if original_persona is None:
                                persona_path.unlink(missing_ok=True)
                            else:
                                self._atomic_write_text(persona_path, original_persona)
                        self._atomic_write_text(cfg_path, original_yaml)
                    except Exception:
                        rollback_failed = True
                self.reload()
                if rollback_failed:
                    # 运行期可见（phase10 P2-2）：self.warnings 会被下次 reload 清空、
                    # 启动后无人消费，不能作为运行期告警通道。
                    logger.warning(
                        "助手 %s 更新写入失败且回滚未完成，请人工检查 %s",
                        assistant_id, cfg_path, exc_info=True,
                    )
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
            with self._lock:
                shutil.move(str(assistant.directory), str(target))
                if purge:
                    self.store.purge_assistant(assistant_id, owner_task_id=mutation_task)
                    shutil.rmtree(target)
                    shutil.rmtree(self.archive_root / "projects" / assistant_id, ignore_errors=True)
                self.reload()
            return target
        finally:
            self.store.release_lock(assistant_id, mutation_task)
