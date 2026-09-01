"""模型提供商切换（架构 §5.4/§5.9 v1.31）：配置存储、按任务路由、温度配置化与 API。

红线边界：API Key 只落在 llm_providers.json（不入 git），API 载荷永不携带密钥原文。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent.runtime import AgentRuntime
from config.settings import Settings


def _settings(tmp_path: Path) -> Settings:
    empty = tmp_path / "empty.json"
    empty.write_text('{"mcpServers": {}}', encoding="utf-8")
    return Settings(
        project_root=tmp_path,
        data_dir=tmp_path,
        skills_dir=Path(__file__).resolve().parents[1] / "skills",
        mcp_config=empty,
        openai_api_key="sk-test-1234567890",
        openai_base_url="https://api.example.com",
        model_name="test-chat",
    )


def _app(tmp_path: Path, runtime: AgentRuntime | None = None):
    from api.main import create_app

    return create_app(settings=_settings(tmp_path), runtime=runtime, start_runtime=False)


def _providers_path(tmp_path: Path) -> Path:
    return tmp_path / "llm_providers.json"


# ---------------------------------------------------------------- 存储层


def test_missing_file_bootstraps_default_provider_from_env(tmp_path):
    from agent.llm_providers import LLMProviderStore

    store = LLMProviderStore(
        _providers_path(tmp_path),
        default_provider=store_default(_settings(tmp_path)),
    )
    assert [item.id for item in store.list()] == ["default"]
    provider = store.get("default")
    assert provider.base_url == "https://api.example.com"
    assert provider.api_key == "sk-test-1234567890"
    assert provider.models == ["test-chat"]
    assert provider.temperature == 0.3
    selection = store.selection()
    assert (selection.provider_id, selection.model) == ("default", "test-chat")
    # 引导即落盘：重启（重新加载）后配置仍在
    reloaded = LLMProviderStore(
        _providers_path(tmp_path),
        default_provider=store_default(_settings(tmp_path)),
    )
    assert reloaded.get("default").api_key == "sk-test-1234567890"
    assert reloaded.selection().model == "test-chat"


def store_default(settings: Settings):
    from agent.llm_providers import LLMProvider

    return LLMProvider(
        id="default",
        name="默认提供商",
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        models=[settings.model_name],
    )


def test_add_and_select_persist_across_reload(tmp_path):
    from agent.llm_providers import LLMProviderStore

    store = LLMProviderStore(
        _providers_path(tmp_path), default_provider=store_default(_settings(tmp_path))
    )
    added = store.add(
        name="备选厂商",
        base_url="https://api.other.com/v1",
        api_key="sk-other-key-9876",
        models=["other-chat", "other-reasoner"],
        temperature=0.7,
    )
    assert added.id.startswith("p-")
    store.select(added.id, "other-reasoner")

    reloaded = LLMProviderStore(
        _providers_path(tmp_path), default_provider=store_default(_settings(tmp_path))
    )
    assert [item.name for item in reloaded.list()] == ["默认提供商", "备选厂商"]
    selection = reloaded.selection()
    assert (selection.provider_id, selection.model) == (added.id, "other-reasoner")
    assert reloaded.get(added.id).temperature == 0.7


def test_corrupt_provider_file_fails_loudly_without_rewrite(tmp_path):
    from agent.llm_providers import LLMProviderStore

    path = _providers_path(tmp_path)
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="llm_providers.json"):
        LLMProviderStore(path, default_provider=store_default(_settings(tmp_path)))
    assert path.read_text(encoding="utf-8") == "{ not json"


def test_current_pointing_to_unknown_provider_or_model_fails(tmp_path):
    from agent.llm_providers import LLMProviderStore

    path = _providers_path(tmp_path)
    store = LLMProviderStore(
        path, default_provider=store_default(_settings(tmp_path))
    )
    good = json.loads(path.read_text(encoding="utf-8"))

    broken = json.loads(json.dumps(good))
    broken["current"]["provider_id"] = "ghost"
    path.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(RuntimeError, match="ghost"):
        LLMProviderStore(path, default_provider=store_default(_settings(tmp_path)))

    broken = json.loads(json.dumps(good))
    broken["current"]["model"] = "未声明模型"
    path.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(RuntimeError, match="未声明模型"):
        LLMProviderStore(path, default_provider=store_default(_settings(tmp_path)))


def test_add_provider_rejects_invalid_input(tmp_path):
    from agent.llm_providers import LLMProviderStore

    store = LLMProviderStore(
        _providers_path(tmp_path), default_provider=store_default(_settings(tmp_path))
    )
    with pytest.raises(ValueError):
        store.add(name="x", base_url="ftp://bad", api_key="sk-1", models=["m"])
    with pytest.raises(ValueError):
        store.add(name="x", base_url="https://ok.com", api_key="sk-1", models=["  "])
    with pytest.raises(ValueError):
        store.add(name="x", base_url="https://ok.com", api_key="sk-1", models=["m"], temperature=2.5)
    with pytest.raises(ValueError):
        store.add(name="  ", base_url="https://ok.com", api_key="sk-1", models=["m"])


def test_select_unknown_provider_or_model_keeps_selection(tmp_path):
    from agent.llm_providers import LLMProviderStore

    store = LLMProviderStore(
        _providers_path(tmp_path), default_provider=store_default(_settings(tmp_path))
    )
    with pytest.raises(KeyError):
        store.select("ghost", "test-chat")
    with pytest.raises(ValueError):
        store.select("default", "未声明模型")
    assert store.selection().model == "test-chat"


def test_api_key_hint_never_reveals_full_secret(tmp_path):
    from agent.llm_providers import LLMProviderStore

    store = LLMProviderStore(
        _providers_path(tmp_path), default_provider=store_default(_settings(tmp_path))
    )
    payload = store.payload()
    hint = payload["providers"][0]["api_key_hint"]
    assert hint
    assert "sk-test-1234567890" not in hint
    raw = json.dumps(json.loads((_providers_path(tmp_path)).read_text(encoding="utf-8")))
    assert "sk-test-1234567890" in raw  # 明文只存在于本地配置文件本身


# ---------------------------------------------------------------- Runtime 按任务路由


class RecordingLLM:
    """记录 chat_text 调用参数的最小假客户端。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="替换后的文本"))]
        )


def test_runtime_resolves_snapshot_and_reuses_clients(tmp_path):
    runtime = AgentRuntime(_settings(tmp_path))
    try:
        first = runtime._resolve_llm()
        assert first[1] == "test-chat"
        assert first[2] == 0.3
        assert runtime._resolve_llm()[0] is first[0]  # 同提供商 client 缓存复用

        added = runtime.providers.add(
            name="备选厂商",
            base_url="https://api.other.com/v1",
            api_key="sk-other-key-9876",
            models=["other-chat"],
            temperature=0.7,
        )
        runtime.providers.select(added.id, "other-chat")
        second = runtime._resolve_llm()
        assert second[1] == "other-chat"
        assert second[2] == 0.7
        assert second[0] is not first[0]
        # 旧任务持有的快照不受切换影响（运行中任务不打断）
        assert first[1] == "test-chat" and first[2] == 0.3
    finally:
        runtime.store.close()


def test_runtime_llm_property_reflects_current_provider_and_accepts_override(tmp_path):
    runtime = AgentRuntime(_settings(tmp_path))
    try:
        assert runtime.llm is runtime._resolve_llm()[0]
        fake = RecordingLLM()
        runtime.llm = fake  # 既有测试注入路径必须保持可用
        assert runtime.llm is fake
        assert runtime._resolve_llm()[0] is fake
        runtime.llm = None
        assert runtime.llm is runtime._resolve_llm()[0]
    finally:
        runtime.store.close()


def test_provider_temperature_flows_into_llm_calls(tmp_path):
    settings = _settings(tmp_path)
    runtime = AgentRuntime(settings)
    try:
        project = runtime.store.create_project("default", "温度项目")
        document = runtime.store.get_project_tree("default", project.project_id)[0]
        runtime.store.save_document(
            "default", project.project_id, document.document_id,
            "第一段正文。", expected_version=document.version,
        )
        document = runtime.store.get_document(
            "default", project.project_id, document.document_id
        )
        fake = RecordingLLM()
        runtime.llm = fake
        asyncio.run(runtime.rewrite_selection(
            "default", project.project_id, document.document_id,
            start=0, end=5, selected_text="第一段正文",
            instruction="润色", document_version=document.version,
        ))
        assert fake.calls[0]["temperature"] == 0.3
        assert fake.calls[0]["model"] == "test-chat"

        added = runtime.providers.add(
            name="低温厂商",
            base_url="https://api.cold.com",
            api_key="sk-cold-1111",
            models=["cold-chat"],
            temperature=0.7,
        )
        runtime.providers.select(added.id, "cold-chat")
        asyncio.run(runtime.rewrite_selection(
            "default", project.project_id, document.document_id,
            start=0, end=5, selected_text="第一段正文",
            instruction="润色", document_version=document.version,
        ))
        assert fake.calls[1]["temperature"] == 0.7
        assert fake.calls[1]["model"] == "cold-chat"
    finally:
        runtime.store.close()


def test_runtime_rejects_task_when_current_provider_key_missing(tmp_path):
    settings = _settings(tmp_path)
    settings.openai_api_key = ""
    runtime = AgentRuntime(settings)
    try:
        with pytest.raises(RuntimeError, match="未配置 OPENAI_API_KEY"):
            asyncio.run(runtime.run("default", "写一篇短文"))
    finally:
        runtime.store.close()


def test_runtime_bootstraps_provider_file_next_to_env(tmp_path):
    runtime = AgentRuntime(_settings(tmp_path))
    try:
        path = _providers_path(tmp_path)
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["providers"][0]["api_key"] == "sk-test-1234567890"
    finally:
        runtime.store.close()


# ---------------------------------------------------------------- API


def test_provider_api_list_masks_key_and_never_leaks_secret(tmp_path):
    runtime = AgentRuntime(_settings(tmp_path))
    with TestClient(_app(tmp_path, runtime)) as client:
        payload = client.get("/api/llm/providers").json()
        assert payload["current"] == {"provider_id": "default", "model": "test-chat"}
        assert len(payload["providers"]) == 1
        item = payload["providers"][0]
        assert item["api_key_hint"].endswith("7890")
        assert "sk-test-1234567890" not in json.dumps(payload)


def test_provider_api_add_switch_and_validation(tmp_path):
    runtime = AgentRuntime(_settings(tmp_path))
    with TestClient(_app(tmp_path, runtime)) as client:
        created = client.post("/api/llm/providers", json={
            "name": "备选厂商",
            "base_url": "https://api.other.com/v1",
            "api_key": "sk-other-key-9876",
            "models": ["other-chat", "other-reasoner"],
            "temperature": 0.7,
        })
        assert created.status_code == 201
        payload = created.json()
        added = next(item for item in payload["providers"] if item["name"] == "备选厂商")
        assert added["models"] == ["other-chat", "other-reasoner"]
        assert added["temperature"] == 0.7

        switched = client.post(
            "/api/llm/providers/current",
            json={"provider_id": added["id"], "model": "other-reasoner"},
        )
        assert switched.status_code == 200
        assert switched.json()["current"] == {
            "provider_id": added["id"], "model": "other-reasoner",
        }
        # 切换立即影响后续任务的解析快照
        assert runtime._resolve_llm()[1] == "other-reasoner"
        assert runtime._resolve_llm()[2] == 0.7

        unknown = client.post(
            "/api/llm/providers/current",
            json={"provider_id": "ghost", "model": "other-reasoner"},
        )
        assert unknown.status_code == 404
        undeclared = client.post(
            "/api/llm/providers/current",
            json={"provider_id": added["id"], "model": "未声明模型"},
        )
        assert undeclared.status_code == 400
        bad_url = client.post("/api/llm/providers", json={
            "name": "坏地址", "base_url": "notaurl", "api_key": "sk-1", "models": ["m"],
        })
        assert bad_url.status_code in (400, 422)
        missing = client.post("/api/llm/providers", json={"name": "缺模型"})
        assert missing.status_code == 422
        # 失败的切换不改变当前选择
        assert client.get("/api/llm/providers").json()["current"]["model"] == "other-reasoner"
