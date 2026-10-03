from __future__ import annotations

import yaml

from agent.atlas_init_snapshot import (
    capture_atlas_init_snapshot,
)


def test_snapshot_copies_config_and_exact_tool_definitions_without_exposing_contents():
    config = {"agent": {"max_tokens": 321}, "provider": {"private": "snapshot-private-marker"}}
    tools = [{"type": "function", "function": {"name": "snapshot_tool", "parameters": {"type": "object"}}}]
    snapshot = capture_atlas_init_snapshot(config=config, tool_definitions=tools, tool_generation=14)

    config["agent"]["max_tokens"] = 999
    tools[0]["function"]["name"] = "mutated"
    assert snapshot.config_copy()["agent"]["max_tokens"] == 321
    assert snapshot.tools_copy()[0]["function"]["name"] == "snapshot_tool"
    assert snapshot.tool_generation == 14
    assert "snapshot-private-marker" not in repr(snapshot)

    returned_config = snapshot.config_copy()
    returned_tools = snapshot.tools_copy()
    returned_config["agent"]["max_tokens"] = 1000
    returned_tools.clear()
    assert snapshot.config_copy()["agent"]["max_tokens"] == 321
    assert len(snapshot.tools_copy()) == 1


def test_snapshot_config_path_uses_temp_hermes_home_and_skips_second_read(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"agent": {"max_tokens": 111}}), encoding="utf-8")

    from hermes_cli.config import load_config

    resolved = load_config()
    snapshot = capture_atlas_init_snapshot(config=resolved, tool_definitions=[], tool_generation=0)
    config_path.write_text(yaml.safe_dump({"agent": {"max_tokens": 222}}), encoding="utf-8")

    assert snapshot.config_copy()["agent"]["max_tokens"] == 111
    assert load_config()["agent"]["max_tokens"] == 222


def test_aiagent_forwarder_passes_snapshot_without_initializing_client(monkeypatch):
    import agent.agent_init
    from run_agent import AIAgent

    snapshot = capture_atlas_init_snapshot(config={}, tool_definitions=[], tool_generation=0)
    forwarded = {}

    def fake_init(agent, **kwargs):
        forwarded.update(kwargs)

    monkeypatch.setattr(agent.agent_init, "init_agent", fake_init)
    AIAgent("prompt", atlas_init_snapshot=snapshot)

    assert forwarded["atlas_init_snapshot"] is snapshot


def test_guarded_real_init_consumes_snapshot_config_and_tools_without_provider_io(tmp_path, monkeypatch):
    """Exercise the constructor/init path with an inert client and temp profile."""
    import hermes_cli.config as config_module
    import run_agent
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    home = tmp_path / "hermes-home"
    home.mkdir()
    token = set_hermes_home_override(home)
    monkeypatch.setattr(run_agent, "_hermes_home", home)
    client_kwargs_seen = []

    def fake_create_client(self, client_kwargs, **_kwargs):
        client_kwargs_seen.append(dict(client_kwargs))
        return object()

    monkeypatch.setattr(run_agent.AIAgent, "_create_openai_client", fake_create_client)
    monkeypatch.setattr(run_agent.AIAgent, "_ensure_lmstudio_runtime_loaded", lambda self, *_a, **_kw: None)
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda: {})
    monkeypatch.setattr(
        run_agent, "get_tool_definitions",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("snapshot path reloaded tools")),
    )

    config_path = home / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "model": {
            "max_tokens": 4096,
            "context_length": 131072,
            "default_headers": {"X-Snapshot-Header": "captured"},
            "extra_headers": {"X-Snapshot-Override": "captured"},
        },
        "providers": {"custom": {"request_timeout_seconds": 45}},
        "prompt_caching": {"cache_ttl": "1h"},
        "sessions": {"write_json_snapshots": True},
        "compression": {"enabled": False},
        "context": {"engine": "compressor"},
        "bedrock": {"guardrail": {
            "guardrail_identifier": "snapshot-guardrail",
            "guardrail_version": "1",
            "stream_processing_mode": "async",
        }},
    }), encoding="utf-8")
    resolved_config = config_module.load_config()
    tool_definitions = [{
        "type": "function",
        "function": {"name": "snapshot_probe", "parameters": {"type": "object", "properties": {}}},
    }]
    snapshot = capture_atlas_init_snapshot(
        config=resolved_config,
        tool_definitions=tool_definitions,
        tool_generation=29,
    )
    config_path.write_text(yaml.safe_dump({
        "model": {
            "max_tokens": 888,
            "context_length": 65536,
            "default_headers": {"X-Snapshot-Header": "current"},
            "extra_headers": {"X-Snapshot-Override": "current"},
        },
        "providers": {"custom": {"request_timeout_seconds": 99}},
        "prompt_caching": {"cache_ttl": "5m"},
        "sessions": {"write_json_snapshots": False},
        "compression": {"enabled": False},
        "context": {"engine": "compressor"},
        "bedrock": {"guardrail": {}},
    }), encoding="utf-8")

    load_attempts = []
    original_load_config = config_module.load_config

    def tracked_load_config():
        load_attempts.append(True)
        return original_load_config()

    readonly_load_attempts = []
    original_load_config_readonly = config_module.load_config_readonly

    def tracked_load_config_readonly():
        readonly_load_attempts.append(True)
        return original_load_config_readonly()

    monkeypatch.setattr(config_module, "load_config", tracked_load_config)
    monkeypatch.setattr(config_module, "load_config_readonly", tracked_load_config_readonly)
    try:
        from run_agent import AIAgent

        common = {
            "base_url": "https://provider.invalid/v1",
            "api_key": "test-only-stub-key",
            "provider": "custom",
            "api_mode": "chat_completions",
            "model": "stub-model",
            "quiet_mode": True,
            "skip_memory": True,
            "skip_context_files": True,
            "session_db": None,
            "atlas_init_snapshot": snapshot,
        }
        agent_from_config = AIAgent(**common)
        agent_from_argument = AIAgent(**common, max_tokens=1234)
        agent_bedrock = AIAgent(**{
            **common,
            "provider": "bedrock",
            "api_mode": "bedrock_converse",
        })
    finally:
        reset_hermes_home_override(token)

    assert load_attempts == []
    assert readonly_load_attempts == []
    for initialized, expected_tokens in ((agent_from_config, 4096), (agent_from_argument, 1234)):
        assert initialized.max_tokens == expected_tokens
        assert initialized._session_init_model_config["max_tokens"] == expected_tokens
        assert initialized._config_context_length == 131072
        assert initialized.context_compressor.context_length == 131072
        assert initialized.context_compressor.max_tokens == expected_tokens
        assert initialized._cache_ttl == "1h"
        assert initialized._session_json_enabled is True
        assert initialized.tools == tool_definitions
        assert initialized._tool_snapshot_generation == 29
        assert initialized._atlas_init_snapshot is snapshot
        assert initialized._provider_request_timeout() == 45
    for kwargs in client_kwargs_seen:
        assert kwargs["timeout"] == 45
        assert kwargs["default_headers"]["X-Snapshot-Header"] == "captured"
        assert kwargs["default_headers"]["X-Snapshot-Override"] == "captured"
    assert agent_bedrock._bedrock_guardrail_config == {
        "guardrailIdentifier": "snapshot-guardrail",
        "guardrailVersion": "1",
        "streamProcessingMode": "async",
        "trace": "disabled",
    }
    assert agent_bedrock._config_context_length == 131072

    # Ordinary initialization keeps its existing config/tool resolution and
    # does not retain an Atlas snapshot on the agent.
    ordinary_tools = [{"type": "function", "function": {"name": "ordinary_tool"}}]
    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda **_kwargs: ordinary_tools)
    ordinary_kwargs = dict(common)
    ordinary_kwargs.pop("atlas_init_snapshot")
    ordinary = run_agent.AIAgent(**ordinary_kwargs)
    assert not hasattr(ordinary, "_atlas_init_snapshot")
    assert load_attempts
    assert readonly_load_attempts
