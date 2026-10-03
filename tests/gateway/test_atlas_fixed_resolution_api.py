"""Actual API resolution/HTTP/executor path, with inert inference boundaries."""
import asyncio
import copy
import inspect
import json
import types
from types import SimpleNamespace

from aiohttp import ClientSession
import pytest
import yaml

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway import atlas_resolution as resolution
from agent import atlas_sol_budget as budget

KEY = "local-synthetic-api-auth"
SESSION = "fixed_phase2_worker"
NONCE = "fixed_resolution_nonce_123456"
SCHEMA = {"type": "function", "function": {"name": "allowed_fixture_tool", "description": "fixture",
          "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    import run_agent
    import gateway.run
    import hermes_cli.runtime_provider
    from tools.registry import registry
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway.run, "_hermes_home", tmp_path)
    import hermes_cli.tools_config
    monkeypatch.setattr(hermes_cli.tools_config, "_get_plugin_toolset_keys", lambda: set())
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_ENABLED", "true")
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_TRANSPORT", "openrouter")
    monkeypatch.setenv("ATLAS_SOL_ACCOUNTING_SOCKET", "/synthetic/accounting.sock")
    monkeypatch.setenv("ATLAS_SOL_ACCOUNTING_TOKEN_FILE", "/synthetic/accounting.token")
    monkeypatch.delenv("HERMES_MAX_TOKENS", raising=False)
    monkeypatch.setenv("HERMES_MAX_ITERATIONS", "90")
    monkeypatch.setattr(resolution, "process_identity", lambda: {
        "pid": 123, "start_ticks": 321, "boot_id": "synthetic-boot", "state": "linux_proc_identity"})
    cfg = {"model": {"default": "openai/gpt-6-luna", "provider": "openrouter",
                     "base_url": "https://openrouter.ai/api/v1", "max_tokens": 8192, "context_length": 100000},
           "agent": {"reasoning_effort": "xhigh", "max_turns": 12},
           "platform_toolsets": {"api_server": ["web", "acre-filemaker", "atlas_vault"]},
           "mcp_servers": {"acre-filemaker": {}, "atlas_vault": {}}}
    path = tmp_path / "config.yaml"
    def write_config():
        path.write_text(yaml.safe_dump(cfg))
    write_config()
    state = {"constructed": [], "dispatched": [], "before_dispatch": None, "skip_dispatch": False, "cfg": cfg, "write": write_config,
             "schemas": [copy.deepcopy(SCHEMA)], "credential": "synthetic-upstream-key"}
    # Real gateway resolution and config loaders; only credential selection is inert.
    def credentials(*args, **kwargs):
        return {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1",
                "api_mode": "codex_responses", "api_key": state["credential"]}
    monkeypatch.setattr(hermes_cli.runtime_provider, "resolve_runtime_provider", credentials)
    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda **kwargs:
                        copy.deepcopy(state["schemas"]) if kwargs["enabled_toolsets"] else [])
    monkeypatch.setattr(registry, "_generation", 7)
    original_signature = inspect.signature(run_agent.AIAgent)
    class InertAgent:
        __signature__ = original_signature
        def __init__(self, **kwargs):
            state["constructed"].append(kwargs)
            vars(self).update(kwargs)
            for name in resolution._DEFAULT_FIELDS:
                if name not in kwargs:
                    setattr(self, name, original_signature.parameters[name].default)
            from hermes_cli.config import load_config
            snap = kwargs.get("atlas_init_snapshot")
            config = snap.config_copy() if snap else load_config()
            self.tools = snap.tools_copy() if snap else copy.deepcopy(state["schemas"] if self.enabled_toolsets else [])
            self.valid_tool_names = {tool["function"]["name"] for tool in self.tools}
            self._tool_snapshot_generation = snap.tool_generation if snap else registry._generation
            self._config_context_length = config["model"].get("context_length")
            self.max_tokens = kwargs.get("max_tokens") or config["model"].get("max_tokens")
            self._fallback_activated = False
            self._atlas_init_snapshot = snap
            self.session_id = kwargs.get("session_id")
            self.api_mode = "codex_responses"
            self.request_overrides = kwargs.get("request_overrides") or {}
            self.context_compressor = SimpleNamespace(context_length=self._config_context_length or 400000)
        def run_conversation(self, **kwargs):
            if state["before_dispatch"]:
                state["before_dispatch"](self)
            if state["skip_dispatch"]:
                return {"final_response": "fixture without dispatch", "completed": True}
            from agent.codex_responses_adapter import _responses_tools
            payload = {"model": self.model, "reasoning": {"effort": self.reasoning_config["effort"]},
                       "input": [{"role": "user", "content": kwargs["user_message"]}],
                       "tools": _responses_tools(self.tools)}
            if self.max_tokens is not None:
                payload["max_output_tokens"] = self.max_tokens
            budget.admitted_call(self, payload, lambda outbound: state["dispatched"].append(outbound) or {})
            if state.get("fabricated_tool_call"):
                # This shim intentionally swallows the denial; the API must
                # still refuse an accepted completion receipt afterwards.
                try:
                    self._atlas_resolution_guard.check_tool_execution(self)
                except resolution.ResolutionDrift:
                    pass
            return {"final_response": "fixture complete", "completed": True}
    monkeypatch.setattr(run_agent, "AIAgent", InertAgent)
    routes = {"atlas-luna": {"model": "openai/gpt-6-luna", "provider": "openrouter", "reasoning_effort": "xhigh"},
              "atlas-sol": {"model": "openai/gpt-6.1-sol", "provider": "openrouter", "reasoning_effort": "low",
                            "max_iterations": 4}}
    def adapter(role="main"):
        result = APIServerAdapter(PlatformConfig(enabled=True, extra={
            "host": "127.0.0.1", "port": 8093 if role == "main" else 8092, "key": KEY,
            "model_routes": routes if role == "main" else {}}))
        monkeypatch.setattr(result, "_ensure_session_db", lambda: None)
        return result
    return adapter, state, registry


@pytest.mark.parametrize("role,alias", [("main", "default"), ("main", "atlas-luna"),
                                      ("main", "atlas-sol"), ("support", "default")])
def test_actual_resolver_constructor_parity_and_original_published_snapshot(harness, role, alias):
    factory, state, registry = harness
    if role == "support":
        state["cfg"]["platform_toolsets"]["api_server"] = []
        state["cfg"]["mcp_servers"] = {}
        state["cfg"]["agent"]["reasoning_effort"] = "medium"
        state["cfg"]["model"].pop("max_tokens")
        state["cfg"]["model"].pop("context_length")
        state["write"]()
    adapter = factory(role)
    route = adapter._resolve_route(alias) if alias != "default" else None
    ordinary = adapter._create_agent(gateway_session_key=SESSION, route=route)
    normal = state["constructed"][-1]
    generation = adapter._prepare_atlas_resolution(gateway_session_key=SESSION, model_alias=alias)
    scope = adapter._atlas_scope(SESSION, alias)
    prepared = adapter._atlas_store()._records[scope]["prepared"]
    guarded = adapter._create_agent(gateway_session_key=SESSION, route=route,
                                   atlas_resolution_expected=generation, atlas_resolution_alias=alias)
    captured = state["constructed"][-1]
    for name in ("model", "provider", "api_mode", "base_url", "max_tokens", "reasoning_config",
                 "enabled_toolsets", "max_iterations", "fallback_model", "api_key"):
        assert normal[name] == captured[name]
    assert guarded._atlas_init_snapshot is prepared.initialization
    assert guarded._atlas_resolution_guard.prepared is prepared
    assert ordinary.max_tokens == guarded.max_tokens
    assert ordinary._config_context_length == guarded._config_context_length
    assert guarded.tools == ordinary.tools
    with pytest.raises(resolution.ResolutionDrift):
        adapter._create_agent(gateway_session_key=SESSION, route=route)


@pytest.mark.parametrize("change", ["config", "credential", "registry", "alias", "policy", "session"])
def test_actual_changed_resolution_stops_before_factory(harness, monkeypatch, change):
    factory, state, registry = harness
    adapter = factory()
    gen = adapter._prepare_atlas_resolution(gateway_session_key=SESSION, model_alias="atlas-luna")
    if change == "config":
        state["cfg"]["model"]["context_length"] = 110000
        state["write"]()
    elif change == "credential":
        state["credential"] = "different-synthetic-key"
    elif change == "registry":
        registry._generation += 1
    elif change == "alias":
        adapter._model_routes["atlas-luna"]["reasoning_effort"] = "medium"
    elif change == "policy":
        monkeypatch.setattr(budget, "LUNA", "unsupported-luna")
    else:
        monkeypatch.setattr(adapter, "_lookup_session_model_override", lambda key: {"model": "override"})
    with pytest.raises((resolution.ResolutionDrift, resolution.ResolutionUnavailable)):
        adapter._create_agent(gateway_session_key=SESSION, route=adapter._resolve_route("atlas-luna"),
                              atlas_resolution_expected=gen, atlas_resolution_alias="atlas-luna")
    assert state["constructed"] == []
    assert state["dispatched"] == []


@pytest.mark.parametrize("target", ["initializer", "factory"])
def test_changed_captured_wrapped_implementation_stops_before_factory(harness, monkeypatch, target):
    factory, state, _registry = harness
    adapter = factory()
    generation = adapter._prepare_atlas_resolution(gateway_session_key=SESSION, model_alias="atlas-luna")
    if target == "initializer":
        from agent import agent_init
        wrapper = agent_init.init_agent
    else:
        wrapper = APIServerAdapter._create_agent
    implementation = next(cell.cell_contents for cell in wrapper.__closure__ or ()
                          if isinstance(cell.cell_contents, types.FunctionType))
    # __wrapped__ can be replaced without altering the function actually called.
    monkeypatch.setattr(wrapper, "__wrapped__", lambda *args, **kwargs: "metadata-decoy")
    scope = adapter._atlas_scope(SESSION, "atlas-luna")
    prepared = adapter._atlas_store()._records[scope]["prepared"]
    assert adapter._atlas_dependencies(scope) == json.loads(prepared.dependency_json)

    def changed_implementation(*args, **kwargs):
        return "changed-behavior"

    assert implementation.__code__.co_freevars == changed_implementation.__code__.co_freevars
    monkeypatch.setattr(implementation, "__code__", changed_implementation.__code__)
    # This is a real behavior change behind the unchanged public wrapper.
    if target == "initializer":
        assert wrapper(object()) == "changed-behavior"
    else:
        assert adapter._create_agent(gateway_session_key="ordinary_for_mutation_probe") == "changed-behavior"
    with pytest.raises(resolution.ResolutionDrift):
        adapter._create_agent(gateway_session_key=SESSION, route=adapter._resolve_route("atlas-luna"),
                              atlas_resolution_expected=generation, atlas_resolution_alias="atlas-luna")
    assert state["constructed"] == [] and state["dispatched"] == []


def test_delegation_module_code_is_part_of_guarded_generation(harness, monkeypatch):
    from agent import atlas_delegation
    factory, state, _registry = harness
    adapter = factory()
    generation = adapter._prepare_atlas_resolution(gateway_session_key=SESSION, model_alias="atlas-luna")
    implementation = atlas_delegation.validate_dispatch
    changed = implementation.__code__.replace(co_firstlineno=implementation.__code__.co_firstlineno + 1)
    monkeypatch.setattr(implementation, "__code__", changed)
    with pytest.raises(resolution.ResolutionDrift):
        adapter._create_agent(gateway_session_key=SESSION, route=adapter._resolve_route("atlas-luna"),
                              atlas_resolution_expected=generation, atlas_resolution_alias="atlas-luna")
    assert state["constructed"] == [] and state["dispatched"] == []


def test_unknown_session_state_is_unavailable_only_for_guarded_capture(harness, monkeypatch):
    factory, state, registry = harness
    adapter = factory()
    def failed_lookup(key):
        raise RuntimeError("private-lookup-failure")
    monkeypatch.setattr(adapter, "_lookup_session_model_override", failed_lookup)
    ordinary = adapter._create_agent(gateway_session_key="ordinary_worker")
    assert ordinary.model == "openai/gpt-6-luna"
    with pytest.raises(resolution.ResolutionUnavailable, match="^atlas_resolution_generation_unavailable$"):
        adapter._prepare_atlas_resolution(gateway_session_key=SESSION)


def auth(**extra):
    return {"Authorization": "Bearer " + KEY, **extra}


@pytest.mark.asyncio
async def test_registered_probe_and_guarded_executor_path(harness, monkeypatch):
    factory, state, registry = harness
    adapter = factory()
    # This case isolates explicit owner publication; startup publication has
    # its own actual-connect coverage below.
    monkeypatch.setattr(adapter, "_bootstrap_atlas_phase2_resolution", lambda: None)
    adapter._port = 0
    assert await adapter.connect()
    port = adapter._site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession(base_url=f"http://127.0.0.1:{port}") as client:
            probe = "/v1/atlas/resolution-generation"
            route = "/v1/atlas/guarded/chat/completions"
            h = auth(**{"X-Atlas-Baseline-Nonce": NONCE})
            assert (await client.get(probe)).status == 401
            assert (await client.get(probe, headers=auth())).status == 400
            assert (await client.get(probe + "?session=arbitrary", headers=h)).status == 400
            empty = await (await client.get(probe, headers=h)).json()
            assert empty["state"] == "unavailable"
            gen = adapter._prepare_atlas_resolution(gateway_session_key=SESSION, model_alias="atlas-luna")
            first = await (await client.get(probe, headers=h)).json()
            text = json.dumps(first)
            assert SESSION not in text and state["credential"] not in text and KEY not in text
            assert first["resolutions"][0]["generation_id"] == gen
            assert first["resolutions"][0]["state"] == "ready_conditioned_on_admission"
            # Probes cannot construct, read config, or refresh provider credentials.
            import hermes_cli.config
            import hermes_cli.runtime_provider
            def forbidden(*args, **kwargs):
                raise AssertionError("probe attempted effectful resolution")
            with monkeypatch.context() as m:
                m.setattr(hermes_cli.config, "load_config", forbidden)
                m.setattr(hermes_cli.runtime_provider, "resolve_runtime_provider", forbidden)
                m.setattr(adapter, "_create_agent", forbidden)
                second = await (await client.get(probe, headers=h)).json()
                assert second["resolutions"][0]["resolved_at_utc"] == first["resolutions"][0]["resolved_at_utc"]
                assert second["resolutions"][0]["generation_id"] == gen
            bound = auth(**{"X-Hermes-Session-Key": SESSION, "X-Atlas-Resolution-Generation": gen})
            body = {"model": "atlas-luna", "messages": [{"role": "user", "content": "fixture request"}]}
            assert (await client.post(route, json=body)).status == 401
            assert (await client.post(route, json=body, headers=auth())).status == 400
            assert (await client.post(route, json=body | {"stream": True}, headers=bound)).status == 400
            assert (await client.post(route, json=body, headers=bound | {"Idempotency-Key": "cached"})).status == 400
            response = await client.post(route, json=body, headers=bound)
            data = await response.json()
            assert response.status == 200, data
            assert response.headers["X-Atlas-Accepted-Resolution-Generation"] == gen
            assert data["atlas_resolution"]["dispatch_count"] == 1
            assert len(state["dispatched"]) == 1
            state["skip_dispatch"] = True
            bypass = await client.post(route, json=body, headers=bound)
            assert bypass.status == 503
            assert "X-Atlas-Accepted-Resolution-Generation" not in bypass.headers
            state["skip_dispatch"] = False
            # Same settings with a different worker binding must not borrow readiness.
            bad = await client.post(route, json=body, headers=bound | {"X-Hermes-Session-Key": "other_worker"})
            assert bad.status == 409
            assert len(state["constructed"]) == 2
            registry._generation += 1
            stale = await client.post(route, json=body, headers=bound)
            assert stale.status == 409
            assert len(state["constructed"]) == 2
            invalidated = await (await client.get(probe, headers=h)).json()
            assert invalidated["resolutions"][0]["state"] == "invalidated"
    finally:
        tasks = list(adapter._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await adapter.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["registry", "model", "cap", "tools", "provider", "override", "key", "context"])
async def test_actual_executor_dispatch_drift_is_blocked(harness, change):
    from gateway.platforms.api_server import _atlas_guard_context
    factory, state, registry = harness
    adapter = factory()
    gen = adapter._prepare_atlas_resolution(gateway_session_key=SESSION)
    def mutate(agent):
        if change == "registry":
            registry._generation += 1
        elif change == "model":
            agent.model = "openai/gpt-6.1-sol"
        elif change == "cap":
            agent.max_tokens += 1
        elif change == "tools":
            agent.tools.append({"type": "function", "function": {"name": "unapproved"}})
        elif change == "provider":
            agent.provider = "unapproved"
        elif change == "override":
            agent.request_overrides = {"model": "unapproved"}
        elif change == "key":
            agent.api_key = "rotated-synthetic-key"
        else:
            agent.context_compressor.context_length += 1
    state["before_dispatch"] = mutate
    token = _atlas_guard_context.set((gen, "default", SESSION))
    try:
        with pytest.raises(resolution.ResolutionDrift):
            await adapter._run_agent(user_message="fixture", conversation_history=[], gateway_session_key=SESSION)
    finally:
        _atlas_guard_context.reset(token)
    assert len(state["constructed"]) == 1
    assert state["dispatched"] == []


@pytest.mark.asyncio
async def test_phase2_startup_probe_and_fixed_request_admission(harness, monkeypatch):
    factory, state, registry = harness
    adapter = factory()
    adapter._port = 0  # Ephemeral socket; role was captured from main's port.
    assert await adapter.connect()
    port = adapter._site._server.sockets[0].getsockname()[1]
    fixed = "atlas-phase2-ownership-v1"
    scope = adapter._atlas_scope(fixed, "default")
    prepared = adapter._atlas_store()._records[scope]["prepared"]
    assert state["constructed"] == [] and state["dispatched"] == []
    try:
        async with ClientSession(base_url=f"http://127.0.0.1:{port}") as client:
            headers = auth(**{"X-Atlas-Baseline-Nonce": NONCE})
            first = await (await client.get("/v1/atlas/resolution-generation", headers=headers)).json()
            assert first["state"] == "available"
            assert len(first["resolutions"]) == 1
            entry = first["resolutions"][0]
            assert entry["state"] == "ready_conditioned_on_admission"
            assert entry["semantic"]["tool_access"] == "none"
            assert entry["semantic"]["loaded_tool_names"] == []
            generation = entry["generation_id"]
            assert fixed not in json.dumps(first)
            # A second fresh observation performs no startup/capture effects.
            import hermes_cli.config
            import hermes_cli.runtime_provider
            def forbidden(*args, **kwargs):
                raise AssertionError("probe attempted owner initialization")
            with monkeypatch.context() as m:
                m.setattr(adapter, "_prepare_atlas_resolution", forbidden)
                m.setattr(hermes_cli.config, "load_config", forbidden)
                m.setattr(hermes_cli.runtime_provider, "resolve_runtime_provider", forbidden)
                second = await (await client.get("/v1/atlas/resolution-generation", headers=headers)).json()
                assert second["resolutions"][0]["generation_id"] == generation
                assert second["resolutions"][0]["resolved_at_utc"] == entry["resolved_at_utc"]
            bound = auth(**{"X-Hermes-Session-Key": fixed,
                            "X-Atlas-Resolution-Generation": generation})
            body = {"model": "default", "messages": [{"role": "user", "content": "synthetic fixed turn"}]}
            accepted = await client.post("/v1/atlas/guarded/chat/completions", headers=bound, json=body)
            data = await accepted.json()
            assert accepted.status == 200, data
            assert accepted.headers["X-Atlas-Accepted-Resolution-Generation"] == generation
            assert data["atlas_resolution"] == {"generation_id": generation, "dispatch_count": 1}
            assert state["constructed"][0]["atlas_init_snapshot"] is prepared.initialization
            assert state["constructed"][0]["enabled_toolsets"] == []
            assert len(state["dispatched"]) == 1
            assert state["dispatched"][0]["model"] == "openai/gpt-6-luna"
            assert state["dispatched"][0]["reasoning"]["effort"] == "xhigh"
            assert not state["dispatched"][0].get("tools")
            state["fabricated_tool_call"] = True
            denied = await client.post("/v1/atlas/guarded/chat/completions", headers=bound, json=body)
            assert denied.status == 409
            assert "X-Atlas-Accepted-Resolution-Generation" not in denied.headers
            state["fabricated_tool_call"] = False
            before = len(state["constructed"])
            # A borrowed/stale generation cannot cause construction.
            stale = await client.post("/v1/atlas/guarded/chat/completions", json=body,
                                      headers=bound | {"X-Atlas-Resolution-Generation": "0" * 64})
            assert stale.status == 409 and len(state["constructed"]) == before
            with pytest.raises(resolution.ResolutionDrift):
                adapter._create_agent(gateway_session_key=fixed)
            ordinary = adapter._create_agent(gateway_session_key="ordinary_after_startup")
            assert ordinary.model == "openai/gpt-6-luna"
            assert ordinary._atlas_init_snapshot is None
            assert ordinary.tools and ordinary.enabled_toolsets
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_phase2_failed_startup_discards_old_publication_and_blocks_legacy(harness, monkeypatch, caplog):
    factory, state, registry = harness
    adapter = factory()
    fixed = "atlas-phase2-ownership-v1"
    old = adapter._prepare_atlas_resolution(gateway_session_key=fixed)
    import hermes_cli.runtime_provider
    def unavailable(*args, **kwargs):
        raise RuntimeError("private-credential-error-must-not-be-logged")
    monkeypatch.setattr(hermes_cli.runtime_provider, "resolve_runtime_provider", unavailable)
    adapter._port = 0
    assert await adapter.connect()  # Ordinary service remains available.
    port = adapter._site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession(base_url=f"http://127.0.0.1:{port}") as client:
            response = await client.get("/v1/atlas/resolution-generation", headers=auth(**{"X-Atlas-Baseline-Nonce": NONCE}))
            assert response.status == 200
            proof = await response.json()
            assert proof["state"] == "unavailable" and proof["resolutions"] == []
            assert (await client.get("/health")).status == 200
            with pytest.raises(resolution.ResolutionDrift):
                adapter._create_agent(gateway_session_key=fixed)
            bound = auth(**{"X-Hermes-Session-Key": fixed, "X-Atlas-Resolution-Generation": old})
            rejected = await client.post("/v1/atlas/guarded/chat/completions", headers=bound,
                                         json={"model": "default", "messages": [{"role": "user", "content": "fixture"}]})
            assert rejected.status in {409, 503}
            assert "X-Atlas-Accepted-Resolution-Generation" not in rejected.headers
            assert state["constructed"] == [] and state["dispatched"] == []
        assert "private-credential-error-must-not-be-logged" not in caplog.text
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["support", "disabled", "other_port"])
async def test_phase2_bootstrap_does_not_prepare_other_workers(harness, monkeypatch, mode):
    factory, state, registry = harness
    adapter = factory("support" if mode == "support" else "main")
    if mode == "disabled":
        adapter._atlas_evidence_enabled = False
    elif mode == "other_port":
        adapter._atlas_resolution_role = None
    def forbidden(*args, **kwargs):
        raise AssertionError("unrelated API startup performed Atlas preparation")
    monkeypatch.setattr(adapter, "_prepare_atlas_resolution", forbidden)
    monkeypatch.setattr(adapter, "_atlas_store", forbidden)
    adapter._port = 0
    assert await adapter.connect()
    try:
        assert adapter._atlas_fixed_bindings == {}
        assert state["constructed"] == [] and state["dispatched"] == []
    finally:
        await adapter.disconnect()
