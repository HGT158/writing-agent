"""长期记忆：每个助手独立的 profile.md（白盒，Agent 自维护，人可手改）。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

_KIND_TITLES = {
    "preference": "偏好",
    "style": "风格",
    "topic": "常用主题",
}


def profile_path(data_dir: Path, assistant_id: str) -> Path:
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
