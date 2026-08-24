"""Memory 层公共标识符校验。"""
from __future__ import annotations

import re

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def validate_id(value: str, label: str = "id") -> str:
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{label} 非法：{value!r}")
    return value


def is_valid_id(value: str) -> bool:
    return _ID_RE.fullmatch(value) is not None
