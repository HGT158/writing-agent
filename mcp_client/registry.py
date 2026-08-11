"""MCP server 配置注册（架构 §5.6）。

字段与 Claude Desktop 配置兼容（command/args/env），但 ${VAR} 环境变量插值
与内置占位符 ${PROJECT_ROOT} 是本实现的超集扩展。

容错原则（架构 §9）：配置解析失败/结构错误只记 warning 并降级，绝不阻断启动。
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _expand(value: str, project_root: Path, warn: Callable[[str], None]) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name == "PROJECT_ROOT":
            return str(project_root)
        val = os.environ.get(name, "")
        if not val:
            warn(f"MCP 配置引用的环境变量 ${{{name}}} 未定义，已替换为空串（server 可能启动失败）")
        return val

    return _VAR_RE.sub(repl, value)


def _expand_any(obj: Any, project_root: Path, warn: Callable[[str], None]) -> Any:
    if isinstance(obj, str):
        return _expand(obj, project_root, warn)
    if isinstance(obj, list):
        return [_expand_any(v, project_root, warn) for v in obj]
    if isinstance(obj, dict):
        return {k: _expand_any(v, project_root, warn) for k, v in obj.items()}
    return obj


def load_server_configs(
    config_path: Path,
    project_root: Path,
    warn: Callable[[str], None] | None = None,
) -> dict[str, dict[str, Any]]:
    """返回 {server_name: {command, args, env}}；env 与当前进程环境合并（子进程需要 PATH 等）。

    坏 JSON / 结构错误 / 缺 command：warning + 跳过，不抛异常（审查 P1-9）。
    """
    warn = warn or (lambda msg: logger.warning(msg))
    if not config_path.exists():
        return {}
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        warn(f"MCP 配置文件解析失败（按无 MCP server 降级）：{config_path}：{exc}")
        return {}
    servers = raw.get("mcpServers", {})
    if not isinstance(servers, dict):
        warn(f"MCP 配置 mcpServers 字段应为对象，按空配置降级：{config_path}")
        return {}

    configs: dict[str, dict[str, Any]] = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict) or not cfg.get("command"):
            warn(f"MCP server {name} 配置缺少 command 字段，已跳过")
            continue
        expanded = _expand_any(cfg, project_root, warn)
        declared_env = expanded.get("env") or {}
        configs[name] = {
            "command": expanded["command"],
            "args": expanded.get("args", []),
            "env": {**os.environ, **declared_env},
        }
    return configs
