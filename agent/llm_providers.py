"""模型提供商配置存储（架构 §5.4 v1.31）。

全部提供商与当前选择持久化于项目根目录 ``llm_providers.json``（与 .env 同目录、
已 gitignore、白盒可手改）。密钥边界（AGENTS.md 规则 4 v1.31 修订）：API Key 只
来自 .env（首次引导合成 default 提供商）或本文件，两者都不得进入 git。写入走
「临时文件 + 原子替换」，并尽力收紧文件权限（失败只记日志，不阻断）。

文件损坏（JSON 解析失败、current 指向未知提供商或未声明模型、id 重复）显式报错
并指向文件路径，不静默回退；人工修复或删除后重启自动重建。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

PROVIDERS_FILE_NAME = "llm_providers.json"
DEFAULT_TEMPERATURE = 0.3


@dataclass
class LLMProvider:
    id: str
    name: str
    base_url: str
    api_key: str
    models: list[str] = field(default_factory=list)
    temperature: float = DEFAULT_TEMPERATURE


@dataclass
class ProviderSelection:
    provider_id: str
    model: str


def mask_api_key(api_key: str) -> str:
    """API 载荷只回掩码尾缀，原文永不下发（架构 §5.9 v1.31）。"""
    if not api_key:
        return ""
    if len(api_key) <= 8:
        return "***"
    return f"{api_key[:3]}***{api_key[-4:]}"


def _tighten_permissions(path: Path) -> None:
    """尽力收紧密钥文件权限：POSIX 0600 / Windows icacls 限当前用户与 SYSTEM。"""
    try:
        if os.name == "posix":
            os.chmod(path, 0o600)
        else:
            user = os.environ.get("USERNAME")
            if not user:
                return
            subprocess.run(
                ["icacls", str(path), "/inheritance:r",
                 "/grant:r", f"{user}:F", "SYSTEM:F"],
                check=True, capture_output=True, timeout=10,
            )
    except Exception:
        logger.debug("收紧模型提供商配置文件权限失败（不影响使用）", exc_info=True)


def _clean_models(models: list[str]) -> list[str]:
    cleaned: list[str] = []
    for item in models:
        name = str(item).strip()
        if not name:
            raise ValueError("可用模型不能为空")
        if len(name) > 200:
            raise ValueError("单个模型名不能超过 200 字符")
        if name not in cleaned:
            cleaned.append(name)
    if not cleaned:
        raise ValueError("至少提供一个可用模型")
    return cleaned


def _clean_temperature(temperature: float | None) -> float:
    value = DEFAULT_TEMPERATURE if temperature is None else float(temperature)
    if not 0 <= value <= 2:
        raise ValueError("温度须在 0 到 2 之间")
    return value


class LLMProviderStore:
    """提供商注册表 + 当前选择指针；进程内持有解析后的状态，变更才落盘。"""

    def __init__(self, path: Path, *, default_provider: LLMProvider | None = None) -> None:
        self.path = path
        self._providers: list[LLMProvider] = []
        self._selection = ProviderSelection(provider_id="", model="")
        if self.path.exists():
            self._load()
        else:
            self._bootstrap(default_provider)

    # ---------------------------------------------------------------- 加载

    def _bootstrap(self, default_provider: LLMProvider | None) -> None:
        if default_provider is None:
            raise RuntimeError(
                f"模型提供商配置 {self.path.name} 缺失且无首次引导配置："
                "请复制 .env.example 为 .env 并填写后重启"
            )
        if not default_provider.models:
            raise RuntimeError(
                "MODEL_NAME 为空，无法合成 default 提供商：请编辑 .env 设置模型名后重启"
            )
        self._providers = [default_provider]
        self._selection = ProviderSelection(
            default_provider.id, default_provider.models[0]
        )
        self._flush()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"模型提供商配置 {self.path} 无法解析（{exc}）："
                "请手工修复或删除该文件后重启（重启会按 .env 重建 default 提供商）"
            ) from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("providers"), list):
            raise RuntimeError(
                f"模型提供商配置 {self.path} 结构非法：缺少 providers 列表"
            )
        providers: list[LLMProvider] = []
        seen_ids: set[str] = set()
        for item in raw["providers"]:
            if not isinstance(item, dict):
                raise RuntimeError(f"模型提供商配置 {self.path} 结构非法：providers 含非对象项")
            provider_id = str(item.get("id", "")).strip()
            if not provider_id:
                raise RuntimeError(f"模型提供商配置 {self.path} 结构非法：提供商缺少 id")
            if provider_id in seen_ids:
                raise RuntimeError(f"模型提供商配置 {self.path} 结构非法：提供商 id 重复（{provider_id}）")
            seen_ids.add(provider_id)
            models_raw = item.get("models", [])
            if not isinstance(models_raw, list) or not models_raw:
                raise RuntimeError(
                    f"模型提供商配置 {self.path} 结构非法：提供商 {provider_id} 的 models 不能为空"
                )
            providers.append(LLMProvider(
                id=provider_id,
                name=str(item.get("name", provider_id)),
                base_url=str(item.get("base_url", "")),
                api_key=str(item.get("api_key", "")),
                models=[str(model) for model in models_raw],
                temperature=_clean_temperature(item.get("temperature")),
            ))
        if not providers:
            raise RuntimeError(f"模型提供商配置 {self.path} 结构非法：providers 不能为空")
        current_raw = raw.get("current")
        if not isinstance(current_raw, dict):
            raise RuntimeError(f"模型提供商配置 {self.path} 结构非法：缺少 current 选择")
        provider_id = str(current_raw.get("provider_id", ""))
        model = str(current_raw.get("model", ""))
        provider = next((item for item in providers if item.id == provider_id), None)
        if provider is None:
            raise RuntimeError(
                f"模型提供商配置 {self.path} 损坏：current 指向未知提供商 {provider_id or '(空)'}"
            )
        if model not in provider.models:
            raise RuntimeError(
                f"模型提供商配置 {self.path} 损坏：提供商 {provider_id} 未声明当前模型 {model or '(空)'}"
            )
        self._providers = providers
        self._selection = ProviderSelection(provider_id, model)

    # ---------------------------------------------------------------- 查询

    def list(self) -> list[LLMProvider]:
        return list(self._providers)

    def get(self, provider_id: str) -> LLMProvider:
        for provider in self._providers:
            if provider.id == provider_id:
                return provider
        raise KeyError(provider_id)

    def selection(self) -> ProviderSelection:
        return ProviderSelection(self._selection.provider_id, self._selection.model)

    def resolve(self) -> tuple[LLMProvider, str]:
        provider = self.get(self._selection.provider_id)
        return provider, self._selection.model

    def payload(self) -> dict:
        return {
            "current": {
                "provider_id": self._selection.provider_id,
                "model": self._selection.model,
            },
            "providers": [{
                "id": provider.id,
                "name": provider.name,
                "base_url": provider.base_url,
                "models": list(provider.models),
                "temperature": provider.temperature,
                "api_key_hint": mask_api_key(provider.api_key),
            } for provider in self._providers],
        }

    # ---------------------------------------------------------------- 变更

    def add(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        models: list[str],
        temperature: float | None = None,
    ) -> LLMProvider:
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 100:
            raise ValueError("提供商名称须为 1-100 个字符")
        clean_url = base_url.strip()
        if not clean_url.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        clean_key = api_key.strip()
        if not clean_key:
            raise ValueError("API Key 不能为空")
        if len(clean_key) > 4096:
            raise ValueError("API Key 不能超过 4096 字符")
        provider = LLMProvider(
            id=f"p-{uuid.uuid4().hex[:8]}",
            name=clean_name,
            base_url=clean_url,
            api_key=clean_key,
            models=_clean_models(models),
            temperature=_clean_temperature(temperature),
        )
        self._providers.append(provider)
        self._flush()
        return provider

    def select(self, provider_id: str, model: str) -> ProviderSelection:
        provider = self.get(provider_id)  # KeyError → API 404
        if model not in provider.models:
            raise ValueError(f"提供商「{provider.name}」未声明模型 {model}")
        self._selection = ProviderSelection(provider_id, model)
        self._flush()
        return ProviderSelection(provider_id, model)

    # ---------------------------------------------------------------- 落盘

    def _flush(self) -> None:
        data = {
            "version": 1,
            "current": {
                "provider_id": self._selection.provider_id,
                "model": self._selection.model,
            },
            "providers": [{
                "id": provider.id,
                "name": provider.name,
                "base_url": provider.base_url,
                "api_key": provider.api_key,
                "models": list(provider.models),
                "temperature": provider.temperature,
            } for provider in self._providers],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".llm_providers-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        _tighten_permissions(self.path)
