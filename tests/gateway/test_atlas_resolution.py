from types import SimpleNamespace

import pytest

from agent import atlas_sol_budget as budget
from agent.atlas_init_snapshot import capture_atlas_init_snapshot
from gateway.atlas_resolution import (
    PreparedTurn,
    ResolutionDrift,
    ResolutionStore,
    ResolutionUnavailable,
    _responses_normalized_tools,
    constructor_defaults,
)


def _factory_defaults(
    tool_delay=1.0, disabled_toolsets=None, providers_allowed=None,
    providers_ignored=None, providers_order=None, provider_sort=None,
    provider_require_parameters=True, provider_data_collection=None,
    service_tier=None, request_overrides=None,
):
    pass


def _kwargs(model="openai/gpt-6.1-sol", api_key="fixture-api-key", credential_pool=None):
    return {
        "model": model,
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_mode": "codex_responses",
        "api_key": api_key,
        "credential_pool": credential_pool,
        "enabled_toolsets": ["web"],
        "disabled_toolsets": None,
        "max_tokens": 4096,
        "max_iterations": 4,
        "reasoning_config": {"enabled": True, "effort": "low"},
        "fallback_model": None,
        "tool_delay": 1.0,
        "providers_allowed": None,
        "providers_ignored": None,
        "providers_order": None,
        "provider_sort": None,
        "provider_require_parameters": True,
        "provider_data_collection": None,
        "service_tier": None,
        "request_overrides": {},
    }


def _snapshot(config=None, tools=None, generation=7):
    return capture_atlas_init_snapshot(
        config=config or {"model": {"context_length": 100_000, "max_tokens": 4096}, "mcp_servers": {}},
        tool_definitions=tools if tools is not None else [{
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web.",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
        }],
        tool_generation=generation,
    )


def _policy(monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_ENABLED", "true")
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_TRANSPORT", "openrouter")
    monkeypatch.setenv("ATLAS_SOL_ACCOUNTING_SOCKET", "/run/atlas-accounting.sock")
    monkeypatch.setenv("ATLAS_SOL_ACCOUNTING_TOKEN_FILE", "/run/atlas-accounting-token")
    return budget.capture_admission_policy()


def _prepared(monkeypatch, *, config=None, tools=None, api_key="fixture-api-key", credential_pool=None):
    return PreparedTurn.capture(
        _kwargs(api_key=api_key, credential_pool=credential_pool),
        _snapshot(config=config, tools=tools), _policy(monkeypatch),
        constructor_defaults(_factory_defaults), "atlas-sol", "a" * 64, "main", False,
        {"code": "fixed"},
    )


def _store(monkeypatch, prepared):
    store = ResolutionStore()
    store.identity = {"state": "linux_proc_identity", "pid": 99}
    generation = store.publish(prepared)
    return store, generation


def _guarded_agent(prepared):
    return SimpleNamespace(
        model="openai/gpt-6.1-sol", provider="openrouter",
        base_url="https://openrouter.ai/api/v1", api_mode="codex_responses",
        reasoning_config={"enabled": True, "effort": "low"},
        enabled_toolsets=["web"], disabled_toolsets=None,
        max_iterations=4, max_tokens=4096, tool_delay=1.0,
        providers_allowed=None, providers_ignored=None, providers_order=None,
        provider_sort=None, provider_require_parameters=True,
        provider_data_collection=None, service_tier=None, request_overrides={},
        tools=prepared.initialization.tools_copy(), _tool_snapshot_generation=7,
        _fallback_activated=False, _config_context_length=100_000,
        context_compressor=SimpleNamespace(context_length=100_000),
        api_key=prepared._kwargs["api_key"],
        _credential_pool=prepared._kwargs["credential_pool"],
        _client_kwargs={
            "api_key": prepared._kwargs["api_key"],
            "base_url": prepared._kwargs["base_url"],
        },
    )


def _outbound(tools=None):
    return {
        "model": "openai/gpt-6.1-sol",
        "reasoning": {"effort": "low"},
        "max_output_tokens": 4096,
        "tools": _responses_normalized_tools(tools if tools is not None else _snapshot().tools_copy(), "openai/gpt-6.1-sol"),
        "extra_body": {"provider": {
            "order": ["openai"], "only": ["openai"],
            "allow_fallbacks": False, "require_parameters": True,
        }},
        "service_tier": "auto",
    }


def test_store_accept_returns_original_immutable_prepared_object(monkeypatch):
    first = _prepared(monkeypatch)
    store, generation = _store(monkeypatch, first)
    fresh_equivalent = _prepared(monkeypatch)
    guard = store.accept(generation, fresh_equivalent, lambda: {"code": "fixed"})
    assert guard.prepared is first
    assert guard.prepared.policy is first.policy


def test_omitted_factory_inputs_use_inspected_defaults_but_explicit_none_is_preserved(monkeypatch):
    import json

    kwargs = _kwargs()
    kwargs.pop("tool_delay")
    kwargs.pop("provider_require_parameters")
    omitted = PreparedTurn.capture(
        kwargs, _snapshot(), _policy(monkeypatch), constructor_defaults(_factory_defaults),
        "atlas-sol", "a" * 64, "main", False, {"code": "fixed"},
    )
    omitted_spec = json.loads(omitted.semantic_json)["constructor"]
    assert omitted_spec["tool_delay"] == 1.0
    assert omitted_spec["provider_require_parameters"] is True
    assert omitted.factory_kwargs()["tool_delay"] == 1.0
    assert omitted.factory_kwargs()["provider_require_parameters"] is True

    explicit = _kwargs()
    explicit["tool_delay"] = None
    explicit_prepared = PreparedTurn.capture(
        explicit, _snapshot(), _policy(monkeypatch), constructor_defaults(_factory_defaults),
        "atlas-sol", "a" * 64, "main", False, {"code": "fixed"},
    )
    assert json.loads(explicit_prepared.semantic_json)["constructor"]["tool_delay"] is None


def test_private_config_changes_generation_without_exporting_config(monkeypatch):
    first = _prepared(monkeypatch)
    store, generation = _store(monkeypatch, first)
    changed = _prepared(monkeypatch, config={
        "model": {"context_length": 100_000, "max_tokens": 4096},
        "mcp_servers": {}, "prompt_caching": {"enabled": True, "label": "café"},
    })
    with pytest.raises(ResolutionDrift):
        store.accept(generation, changed, lambda: {"code": "fixed"})
    new_generation = store.publish(changed)
    assert new_generation != generation
    proof = store.proof("nonce-1234567890abc", lambda _scope: {"code": "fixed"})
    assert "prompt_caching" not in repr(proof)
    assert "fixture-api-key" not in repr(proof)


def test_prepared_capture_does_not_copy_or_repr_opaque_credentials(monkeypatch):
    class OpaquePool:
        def __deepcopy__(self, _memo):
            raise AssertionError("credential pool must remain opaque")

        def __repr__(self):
            raise AssertionError("credential pool must not be represented")

    pool = OpaquePool()
    prepared = _prepared(monkeypatch, credential_pool=pool)
    assert prepared.factory_kwargs()["credential_pool"] is pool
    assert "fixture-api-key" not in repr(prepared)
    assert "OpaquePool" not in repr(prepared)


def test_unknown_callback_or_session_object_is_rejected_before_copy(monkeypatch):
    class Dangerous:
        def __deepcopy__(self, _memo):
            raise AssertionError("unexpected object was copied")

        def __repr__(self):
            raise AssertionError("unexpected object was represented")

    kwargs = _kwargs()
    kwargs["session_db"] = Dangerous()
    with pytest.raises(ResolutionUnavailable):
        PreparedTurn.capture(
            kwargs, _snapshot(), _policy(monkeypatch), constructor_defaults(_factory_defaults),
            "atlas-sol", "a" * 64, "main", False, {"code": "fixed"},
        )


def test_construction_and_dispatch_bind_mode_context_output_defaults_and_registry(monkeypatch):
    prepared = _prepared(monkeypatch)
    store, generation = _store(monkeypatch, prepared)
    guard = store.accept(generation, prepared, lambda: {"code": "fixed"})
    agent = _guarded_agent(prepared)
    guard.check_constructed(agent)
    guard.check_dispatch(agent, _outbound(), prepared.policy)
    assert guard.dispatch_count == 1

    mutations = (
        {"api_mode": "chat_completions"},
        {"max_tokens": 2048},
        {"_tool_snapshot_generation": 8},
        {"model": "openai/gpt-6-luna"},
        {"providers_order": ["anthropic"]},
    )
    for changes in mutations:
        candidate = _guarded_agent(prepared)
        for name, value in changes.items():
            setattr(candidate, name, value)
        with pytest.raises(ResolutionDrift):
            guard.check_constructed(candidate)

    changed_context = _guarded_agent(prepared)
    guard.check_constructed(changed_context)
    changed_context.context_compressor.context_length += 1
    with pytest.raises(ResolutionDrift):
        guard.check_dispatch(changed_context, _outbound(), prepared.policy)


def test_construction_and_dispatch_bind_private_credentials(monkeypatch):
    pool = object()
    prepared = _prepared(monkeypatch, api_key="café-key", credential_pool=pool)
    store, generation = _store(monkeypatch, prepared)
    guard = store.accept(generation, prepared, lambda: {"code": "fixed"})

    agent = _guarded_agent(prepared)
    guard.check_constructed(agent)
    guard.check_dispatch(agent, _outbound(), prepared.policy)

    for attr, value in (
        ("api_key", "changed-key"),
        ("_credential_pool", object()),
        ("_client_kwargs", {"api_key": "changed-key", "base_url": "https://openrouter.ai/api/v1"}),
        ("_client_kwargs", {"api_key": "café-key", "base_url": "https://other.example/v1"}),
    ):
        changed = _guarded_agent(prepared)
        setattr(changed, attr, value)
        with pytest.raises(ResolutionDrift):
            guard.check_constructed(changed)

    constructed = _guarded_agent(prepared)
    guard.check_constructed(constructed)
    constructed.api_key = "rotated-key"
    with pytest.raises(ResolutionDrift):
        guard.check_dispatch(constructed, _outbound(), prepared.policy)


def test_private_credential_change_requires_new_generation(monkeypatch):
    first = _prepared(monkeypatch, api_key="first-key")
    store, generation = _store(monkeypatch, first)
    changed = _prepared(monkeypatch, api_key="second-key")
    with pytest.raises(ResolutionDrift):
        store.accept(generation, changed, lambda: {"code": "fixed"})
    assert store.publish(changed) != generation


def test_responses_dispatch_allows_only_captured_normalized_tools(monkeypatch):
    prepared = _prepared(monkeypatch)
    store, generation = _store(monkeypatch, prepared)
    guard = store.accept(generation, prepared, lambda: {"code": "fixed"})
    agent = _guarded_agent(prepared)
    guard.check_constructed(agent)

    extra = {"type": "function", "name": "delete_everything", "description": "bad",
             "strict": False, "parameters": {"type": "object", "properties": {}}}
    malicious = _outbound()
    malicious["tools"] = malicious["tools"] + [extra]
    with pytest.raises(ResolutionDrift):
        guard.check_dispatch(agent, malicious, prepared.policy)
    changed_schema = _outbound()
    changed_schema["tools"][0]["description"] = "mutated after construction"
    with pytest.raises(ResolutionDrift):
        guard.check_dispatch(agent, changed_schema, prepared.policy)
    no_temperature = _outbound()
    no_temperature["temperature"] = 0.2
    with pytest.raises(ResolutionDrift):
        guard.check_dispatch(agent, no_temperature, prepared.policy)
    assert guard.dispatch_count == 0


def test_sol_to_luna_dispatch_requires_one_gate_issued_fallback_marker(monkeypatch):
    prepared = _prepared(monkeypatch)
    store, generation = _store(monkeypatch, prepared)
    guard = store.accept(generation, prepared, lambda: {"code": "fixed"})
    agent = _guarded_agent(prepared)
    guard.check_constructed(agent)
    agent.model = "openai/gpt-6-luna"
    agent.reasoning_config = {"enabled": True, "effort": "xhigh"}
    fallback = _outbound()
    fallback["model"] = "openai/gpt-6-luna"
    fallback["reasoning"] = {"effort": "xhigh"}
    with pytest.raises(ResolutionDrift):
        guard.check_dispatch(agent, fallback, prepared.policy)
    agent._atlas_sol_budget_fallback = guard.authorize_budget_fallback(
        prepared.policy, "openai/gpt-6.1-sol", "openai/gpt-6-luna", "xhigh",
    )
    guard.check_dispatch(agent, fallback, prepared.policy)
    assert guard.dispatch_count == 1
    with pytest.raises(ResolutionDrift):
        guard.authorize_budget_fallback(
            prepared.policy, "openai/gpt-6-luna", "openai/gpt-6.1-sol", "low",
        )
