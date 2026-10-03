"""Owner metadata behavior through the real adapter, with no provider traffic."""
import asyncio
import datetime as dt
import json
import sys
import types
from functools import wraps

from aiohttp import ClientSession
import pytest
import pytest_asyncio

from gateway import atlas_runtime_evidence as evidence
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from agent import atlas_sol_budget as budget

KEY = "atlas-owner-test-bearer-76e84a12"
NONCE = "fixture_nonce_123456789"


def agent(**updates):
    fields = dict(model="openai/gpt-6-luna", provider="openrouter",
                  base_url="https://openrouter.ai/api/v1", max_tokens=8192,
                  _config_context_length=100000, max_iterations=90,
                  enabled_toolsets=["atlas_vault", "web", "acre-filemaker"],
                  reasoning_config={"enabled": True, "effort": "xhigh"}, api_key="DO_NOT_EXPORT")
    return types.SimpleNamespace(**(fields | updates))


@pytest.fixture(autouse=True)
def reset_evidence():
    evidence._last_dispatch.clear()
    yield
    evidence._last_dispatch.clear()


@pytest_asyncio.fixture
async def server(monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_ENABLED", "true")
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_TRANSPORT", "openrouter")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={
        "host": "127.0.0.1", "key": KEY,
        "model_routes": {"atlas-luna": {"model": "openai/gpt-6-luna", "provider": "openrouter",
                                        "reasoning_effort": "xhigh", "api_key": "ROUTE_SECRET"}},
    }))
    adapter._port = 0
    def forbidden(*args, **kwargs):
        raise AssertionError("metadata must not create agents or reload config")
    monkeypatch.setattr(adapter, "_create_agent", forbidden)
    import gateway.run
    monkeypatch.setattr(gateway.run, "_load_gateway_config", forbidden)
    monkeypatch.setattr(gateway.run, "_resolve_runtime_agent_kwargs", forbidden)
    assert await adapter.connect()
    port = adapter._site._server.sockets[0].getsockname()[1]
    async with ClientSession(base_url=f"http://127.0.0.1:{port}") as client:
        yield adapter, client
    tasks = list(adapter._background_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await adapter.disconnect()


def headers(nonce=NONCE, key=KEY):
    return {"Authorization": "Bearer " + key, "X-Atlas-Baseline-Nonce": nonce}


@pytest.mark.asyncio
async def test_actual_registered_endpoint_auth_nonce_and_safe_output(server, monkeypatch):
    adapter, client = server
    # Denied calls must not invoke the metadata helper at all.
    original = evidence.snapshot
    def forbidden(*args):
        raise AssertionError("unauthorized metadata read")
    monkeypatch.setattr(evidence, "snapshot", forbidden)
    assert (await client.get("/v1/atlas/runtime-evidence")).status == 401
    assert (await client.get("/v1/atlas/runtime-evidence", headers=headers(key="wrong"))).status == 401
    assert (await client.get("/v1/atlas/runtime-evidence", headers=headers(nonce="short"))).status == 400
    assert (await client.get("/v1/atlas/runtime-evidence?pid=1", headers=headers())).status == 400
    monkeypatch.setattr(evidence, "snapshot", original)
    response = await client.get("/v1/atlas/runtime-evidence", headers=headers())
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    result = await response.json()
    assert result["nonce"] == NONCE
    assert result["process"]["pid"] > 0
    assert result["loaded_code"]["gateway.platforms.api_server"]["state"] == "loaded_python_code"
    assert result["loaded_code"]["gateway.atlas_runtime_evidence"]["sha256"] == evidence.loaded_code(evidence)["sha256"]
    assert result["loaded_source_sha256"] is None
    assert result["loaded_profile_sha256"] is None
    assert result["last_dispatch_state"] == "not_observed"
    assert result["external_byok_state"] == "not_observed"
    assert dt.datetime.fromisoformat(result["collection_started_at_utc"]) <= dt.datetime.fromisoformat(result["observed_at_utc"])
    assert "ROUTE_SECRET" not in json.dumps(result)
    assert KEY not in json.dumps(result)
    # Normalized cached state changes its hash without reading config.yaml.
    adapter._model_routes["atlas-luna"]["reasoning_effort"] = "medium"
    next_result = await (await client.get("/v1/atlas/runtime-evidence", headers=headers("another_nonce_12345"))).json()
    assert next_result["adapter_routes_sha256"] != result["adapter_routes_sha256"]


@pytest.mark.asyncio
async def test_no_key_disabled_or_non_atlas_bind_cannot_publish(server):
    adapter, client = server
    adapter._api_key = ""
    assert (await client.get("/v1/atlas/runtime-evidence", headers=headers())).status == 401
    adapter._api_key = KEY
    adapter._atlas_evidence_enabled = False
    assert (await client.get("/v1/atlas/runtime-evidence", headers=headers())).status == 404


@pytest.mark.asyncio
async def test_fresh_endpoint_does_not_refresh_dispatch_age_or_reload_disk(server, monkeypatch, tmp_path):
    adapter, client = server
    monkeypatch.setattr(evidence, "_now", lambda: "2026-09-30T00:00:00+00:00")
    budget.admitted_call(agent(), {"model": "openai/gpt-6-luna", "reasoning": {"effort": "xhigh"}}, lambda _: None)
    monkeypatch.setattr(evidence, "_now", lambda: "2026-10-01T00:00:00+00:00")
    result = await (await client.get("/v1/atlas/runtime-evidence", headers=headers())).json()
    assert result["last_dispatch"]["observed_at_utc"] < result["observed_at_utc"]
    from hermes_constants import get_hermes_home
    config = get_hermes_home() / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("model:\n  default: changed-on-disk\n")
    next_result = await (await client.get("/v1/atlas/runtime-evidence", headers=headers("fresh_nonce_234567"))).json()
    assert next_result["last_dispatch"] == result["last_dispatch"]
    assert next_result["adapter_routes"] == result["adapter_routes"]
    assert next_result["loaded_profile_sha256"] is None


@pytest.mark.asyncio
async def test_internal_exception_has_no_secret_response(server, monkeypatch):
    _, client = server
    def fail(*args):
        raise ValueError("sk-secret-exception-content")
    monkeypatch.setattr(evidence, "snapshot", fail)
    response = await client.get("/v1/atlas/runtime-evidence", headers=headers())
    assert response.status == 503
    assert "secret" not in await response.text()


@pytest.mark.parametrize("enabled,host", [("false", "127.0.0.1"), ("true", "0.0.0.0"), ("true", "localhost")])
def test_adapter_gating(enabled, host, monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_ENABLED", enabled)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"host": host, "key": KEY}))
    try:
        assert adapter._atlas_evidence_enabled is False
    finally:
        adapter._response_store.close()


def test_code_fingerprint_uses_loaded_objects_not_changed_disk(tmp_path):
    module = types.ModuleType("owner_test_module")
    path = tmp_path / "module.py"
    path.write_text("def answer(): return 1\n")
    exec(compile("def answer(): return 1\n", str(path), "exec"), vars(module))
    first = evidence.loaded_code(module)
    assert module.answer() == 1
    path.write_text("def answer(): return 2\n")
    assert evidence.loaded_code(module) == first
    exec(compile("def answer(): return 2\n", str(path), "exec"), vars(module))
    assert module.answer() == 2
    assert evidence.loaded_code(module)["sha256"] != first["sha256"]
    assert evidence.loaded_code(None) == {"state": "not_loaded", "sha256": None}


def test_loaded_code_tracks_invoked_closure_not_replaceable_wrapped_metadata():
    module = types.ModuleType("owner_wrapped_module")
    secret = "synthetic-secret-not-exported"

    def make_wrapper():
        def implementation():
            return 1

        @wraps(implementation)
        def wrapper():
            return implementation(), secret

        return wrapper, implementation

    module.answer, implementation = make_wrapper()
    module.answer.__module__ = module.__name__
    first = evidence.loaded_code(module)
    assert module.answer()[0] == 1
    module.answer.__wrapped__ = lambda: 99
    assert evidence.loaded_code(module) == first

    def changed():
        return 2

    implementation.__code__ = changed.__code__
    assert module.answer()[0] == 2
    second = evidence.loaded_code(module)
    assert second["sha256"] != first["sha256"]
    assert secret not in json.dumps(second)


def test_loaded_code_handles_cyclic_function_closures():
    module = types.ModuleType("owner_cycle_module")
    captured = None

    def wrapper():
        return captured

    captured = wrapper
    module.wrapper = wrapper
    wrapper.__module__ = module.__name__
    first = evidence.loaded_code(module)
    assert first["state"] == "loaded_python_code"
    assert first["code_object_count"] == 1
    assert evidence.loaded_code(module) == first


def test_canonical_hash_and_unknown_fields_do_not_export_secrets():
    assert evidence.digest({"b": 2, "a": 1}) == evidence.digest({"a": 1, "b": 2})
    secret = "sk-secret-never-export"
    evidence.record_dispatch(agent(model=secret, enabled_toolsets=[secret]), {
        "model": secret, "input": secret, "api_key": secret,
        "extra_body": {"provider": {"order": [secret], "api_key": secret}},
    })
    output = next(iter(evidence._last_dispatch.values()))
    assert secret not in json.dumps(output)
    assert output["effective_agent_profile"]["model"] is None
    assert output["effective_agent_profile"]["enabled_toolsets"] is None
    assert output["request_policy"]["provider_order"] is None


@pytest.mark.parametrize("denied", [False, True])
def test_actual_admission_dispatch_snapshot_including_sol_fallback(denied, monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_ENABLED", "true")
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_TRANSPORT", "openrouter")
    monkeypatch.delenv("ATLAS_SOL_ACCOUNTING_SOCKET", raising=False)
    instance = agent(model="openai/gpt-6.1-sol" if denied else "openai/gpt-6-luna")
    payload = {"model": instance.model, "max_output_tokens": 512,
               "reasoning": {"effort": "low" if denied else "xhigh"}, "input": "PRIVATE_PROMPT"}
    actual = []
    def perform(outbound):
        actual.append(outbound)
        return "fixture-response"
    assert budget.admitted_call(instance, payload, perform) == "fixture-response"
    observed = next(iter(evidence._last_dispatch.values()))
    assert observed["request_policy"] == evidence.request_policy(actual[0])
    assert observed["request_policy"]["model"] == "openai/gpt-6-luna"
    assert observed["request_policy"]["provider_allow_fallbacks"] is False
    assert observed["request_policy"]["provider_require_parameters"] is True
    assert observed["request_policy_sha256"] == evidence.digest(observed["request_policy"])
    assert observed["effective_agent_profile_sha256"] == evidence.digest(observed["effective_agent_profile"])
    assert observed["admission_code"]["state"] == "loaded_python_code"
    assert "PRIVATE_PROMPT" not in json.dumps(observed)
    assert "DO_NOT_EXPORT" not in json.dumps(observed)
    assert payload["model"] == ("openai/gpt-6.1-sol" if denied else "openai/gpt-6-luna")


def test_observer_failure_preserves_dispatch_and_non_atlas_is_unobserved(monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_ENABLED", "true")
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_TRANSPORT", "openrouter")
    monkeypatch.setattr(evidence, "_profile", lambda _: (_ for _ in ()).throw(ValueError("SECRET")))
    assert budget.admitted_call(agent(), {"model": "openai/gpt-6-luna"}, lambda _: "ok") == "ok"
    assert evidence._last_dispatch == {}
    monkeypatch.delenv("ATLAS_MODEL_ROUTING_ENABLED", raising=False)
    monkeypatch.delenv("ATLAS_SOL_ACCOUNTING_SOCKET", raising=False)
    monkeypatch.delenv("ATLAS_SOL_ACCOUNTING_TOKEN_FILE", raising=False)
    payload = {"model": "openai/gpt-6-luna"}
    assert budget.admitted_call(agent(), payload, lambda out: out is payload)
    assert evidence._last_dispatch == {}


@pytest.mark.parametrize("failure", ["import", "call"])
def test_optional_observer_failure_does_not_skip_provider_dispatch(failure, monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_ENABLED", "true")
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_TRANSPORT", "openrouter")
    if failure == "import":
        monkeypatch.setitem(sys.modules, "gateway.atlas_runtime_evidence", None)
    else:
        def broken(*args):
            raise RuntimeError("observation_failure")
        monkeypatch.setattr(evidence, "record_dispatch", broken)
    seen = []
    assert budget.admitted_call(agent(), {"model": "openai/gpt-6-luna"}, lambda out: seen.append(out) or "ok") == "ok"
    assert len(seen) == 1
    assert seen[0]["extra_body"]["provider"]["allow_fallbacks"] is False


def test_process_identity_parses_parentheses_and_unavailable_platform(monkeypatch):
    def read(path, *, encoding):
        assert encoding == "utf-8"
        if str(path) == "/proc/self/stat":
            return "123 (python (worker)) " + " ".join(["S"] + ["0"] * 18 + ["54321"])
        return "12345678-1234-1234-1234-123456789abc\n"
    monkeypatch.setattr(evidence.Path, "read_text", read)
    assert evidence.process_identity()["start_ticks"] == 54321
    def unavailable(_, *, encoding):
        assert encoding == "utf-8"
        raise FileNotFoundError
    monkeypatch.setattr(evidence.Path, "read_text", unavailable)
    assert evidence.process_identity()["state"] == "start_identity_unavailable"
    assert evidence.process_identity()["start_ticks"] is None
