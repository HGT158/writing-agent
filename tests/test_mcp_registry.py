"""MCP 配置注册容错测试（审查 P1-9）：坏配置只警告不抛异常。"""
import json

from mcp_client.registry import load_server_configs


def test_bad_json_degrades_to_empty(tmp_path):
    cfg = tmp_path / "bad.json"
    cfg.write_text("{not valid json", encoding="utf-8")
    warnings = []
    result = load_server_configs(cfg, tmp_path, warn=warnings.append)
    assert result == {}
    assert any("解析失败" in w for w in warnings)


def test_missing_command_server_skipped(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"mcpServers": {"no-cmd": {"args": []}, "ok": {"command": "uvx", "args": ["x"]}}}),
                   encoding="utf-8")
    warnings = []
    result = load_server_configs(cfg, tmp_path, warn=warnings.append)
    assert set(result) == {"ok"}
    assert any("no-cmd" in w for w in warnings)


def test_var_interpolation_treats_empty_as_unset_without_warning(tmp_path, monkeypatch):
    """P3-3：空/未定义变量等价未设置，只记 debug，不再产生启动噪音 warning。"""
    monkeypatch.setenv("DEFINED_KEY", "secret-1")
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"mcpServers": {"s": {
        "command": "npx",
        "args": ["-y", "pkg", "${PROJECT_ROOT}/data"],
        "env": {"A": "${DEFINED_KEY}", "B": "${UNDEFINED_KEY}"},
    }}}), encoding="utf-8")
    warnings = []
    result = load_server_configs(cfg, tmp_path, warn=warnings.append)
    server = result["s"]
    assert server["args"][-1] == f"{tmp_path}/data"
    assert server["env"]["A"] == "secret-1"
    assert server["env"]["B"] == ""
    assert warnings == []


def test_subprocess_env_does_not_inherit_unlisted_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("PATH", "safe-path")
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"mcpServers": {"s": {
        "command": "tool", "env": {"SERVER_TOKEN": "declared"},
    }}}), encoding="utf-8")

    env = load_server_configs(cfg, tmp_path)["s"]["env"]

    assert env["PATH"] == "safe-path"
    assert env["SERVER_TOKEN"] == "declared"
    assert "OPENAI_API_KEY" not in env
