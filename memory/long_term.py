"""长期记忆：每个助手独立的 profile.md（白盒，Agent 自维护，人可手改）。"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from .validation import validate_id

logger = logging.getLogger(__name__)

_KIND_TITLES = {
    "preference": "偏好",
    "style": "风格",
    "topic": "常用主题",
}

# 画像整文替换上限（架构 §5.7 v1.30）；追加路径仍由行数上限兜底。
ASSISTANT_PROFILE_MAX_CHARS = 50_000


def profile_path(data_dir: Path, assistant_id: str) -> Path:
    validate_id(assistant_id, "assistant_id")
    return data_dir / "assistants" / assistant_id / "memory" / "profile.md"


def read_profile(data_dir: Path, assistant_id: str) -> str:
    path = profile_path(data_dir, assistant_id)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


_MAX_PROFILE_LINES = 200  # 超限时保留头部 + 最近 150 条，防画像无限膨胀（审查 P2-16）


def append_profile(data_dir: Path, assistant_id: str, kind: str, content: str) -> None:
    """增量追加一条画像记录；超过行数上限时归并保留最近条目。"""
    path = profile_path(data_dir, assistant_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"# 助手 {assistant_id} 的长期画像\n\n", encoding="utf-8")
    title = _KIND_TITLES.get(kind, kind)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"- [{title}] {content} （{ts}）\n")

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if len(lines) > _MAX_PROFILE_LINES:
        header = lines[0]
        kept = lines[-150:]
        path.write_text(header + "\n" + "".join(kept), encoding="utf-8")


def replace_profile(data_dir: Path, assistant_id: str, content: str) -> None:
    """白盒整文替换：UTF-8 原样写入，不重排格式、不补写头部；空白属显式清空。

    写入失败按原内容尽力回滚，不留下半程状态（与 persona 编辑同模式）。
    """
    if len(content) > ASSISTANT_PROFILE_MAX_CHARS:
        raise ValueError(f"profile 内容超过上限 {ASSISTANT_PROFILE_MAX_CHARS} 字符")
    path = profile_path(data_dir, assistant_id)
    original = path.read_text(encoding="utf-8") if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(content, encoding="utf-8")
    except (OSError, UnicodeError):
        if original is not None:
            try:
                path.write_text(original, encoding="utf-8")
            except (OSError, UnicodeError):
                logger.warning(
                    "profile 回滚失败（assistant=%s）", assistant_id, exc_info=True
                )
        raise
