"""Atlas Queries tool: capability-gated visibility, fixed callback, bounded results."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from tools import atlas_query_tool as tool

TOKEN = "atqry_" + "a" * 32
UNAVAILABLE = {"error": "Query operation unavailable"}
QUERY = {"atlas_query"}


def names(toolsets=None, quiet=True):
    from model_tools import get_tool_definitions
    return {item["function"]["name"] for item in get_tool_definitions(
        enabled_toolsets=toolsets if toolsets is not None else ["atlas_query"],
        quiet_mode=quiet, skip_tool_search_assembly=True)}


def call(operation, arguments=None):
    return json.loads(tool._handle_atlas_query({"operation": operation, "arguments": arguments or {}}))


@pytest.fixture
def callback(monkeypatch):
    observed = []
    response = [lambda request: httpx.Response(200, json={"status": "ready"})]
    real_client = httpx.Client

    def receive(request):
        observed.append(request)
        return response[0](request)

    def factory(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return real_client(transport=httpx.MockTransport(receive), **kwargs)

    monkeypatch.setattr(tool, "httpx", SimpleNamespace(
        Client=factory, Timeout=httpx.Timeout, HTTPError=httpx.HTTPError))
    bound = tool.bind_query_capability(TOKEN)
    yield observed, response
    tool.clear_query_capability(bound)


@pytest.mark.parametrize("quiet", [True, False])
def test_visible_only_while_bound_and_only_in_its_toolset(quiet):
    assert not names(quiet=quiet) & QUERY
    for _ in range(2):
        bound = tool.bind_query_capability(TOKEN)
        try:
            assert names(quiet=quiet) == QUERY
            assert not names(["web"], quiet) & QUERY
        finally:
            tool.clear_query_capability(bound)
        assert not names(quiet=quiet) & QUERY


def test_invalid_capability_never_binds():
    for value in ("", "bad", "atown_" + "a" * 32, 42, None):
        bound = tool.bind_query_capability(value)
        try:
            assert not tool.query_capability_available()
        finally:
            tool.clear_query_capability(bound)


def test_no_capability_means_no_dispatch(monkeypatch):
    monkeypatch.setattr(tool, "httpx", SimpleNamespace(Client=lambda **_: pytest.fail("dispatched")))
    assert call("catalog") == UNAVAILABLE


@pytest.mark.parametrize("operation,arguments", [
    ("catalog", {}), ("catalog", {"entities": ["lease"]}),
    ("validate", {"reply": {"schema_version": 3, "status": "ready"}}), ("execute", {}),
])
def test_valid_operations_reach_the_fixed_callback_unchanged(callback, operation, arguments):
    observed, _ = callback
    assert call(operation, arguments) == {"status": "ready"}
    request = observed[0]
    assert str(request.url) == "http://127.0.0.1:8243/api/v1/internal/query-tool"
    assert request.headers["Authorization"] == "Bearer " + TOKEN
    assert json.loads(request.content) == {"operation": operation, "arguments": arguments}


@pytest.mark.parametrize("args", [
    {"operation": "sql", "arguments": {}}, {"operation": "execute", "arguments": {"plan": {}}},
    {"operation": "execute", "arguments": {}, "url": "http://x"}, {"operation": "catalog"},
    {"operation": "catalog", "arguments": {"entities": "lease"}},
    {"operation": "catalog", "arguments": {"entities": ["a"] * 9}},
    {"operation": "catalog", "arguments": {"entities": [3]}},
    {"operation": "catalog", "arguments": {"other": 1}},
    {"operation": "validate", "arguments": {}}, {"operation": "validate", "arguments": {"reply": []}},
    {"operation": "validate", "arguments": {"reply": {}, "extra": 1}},
    {"operation": "validate", "arguments": {"reply": {"a": "x" * 61000}}},
    {"operation": "validate", "arguments": {"reply": {"a": TOKEN}}},
    {"operation": "validate", "arguments": {"reply": {TOKEN: 1}}},
    {"operation": "validate", "arguments": {"reply": {"a": ["atown_" + "b" * 32]}}},
    {"operation": "validate", "arguments": {"reply": {"a": float("nan")}}},
    [], None,
])
def test_invalid_requests_never_dispatch(callback, args):
    observed, _ = callback
    assert json.loads(tool._handle_atlas_query(args)) == UNAVAILABLE
    assert observed == []


def test_deeply_nested_reply_is_refused(callback):
    observed, _ = callback
    reply = current = {}
    for _ in range(30):
        current["a"] = {}
        current = current["a"]
    assert call("validate", {"reply": reply}) == UNAVAILABLE and observed == []


@pytest.mark.parametrize("status,expected", [
    (429, {"error": "Query tool limit reached"}),
    (409, {"error": "Validate a plan before executing it"}),
    (403, UNAVAILABLE), (404, UNAVAILABLE), (422, UNAVAILABLE), (500, UNAVAILABLE), (302, UNAVAILABLE),
])
def test_http_failures_become_fixed_text(callback, status, expected):
    _, response = callback
    response[0] = lambda request: httpx.Response(status, json={"detail": "secret " + TOKEN})
    assert call("execute") == expected


@pytest.mark.parametrize("make", [
    lambda: httpx.Response(200, content=b"not json", headers={"content-type": "application/json"}),
    lambda: httpx.Response(200, content=b"{}", headers={"content-type": "text/plain"}),
    lambda: httpx.Response(200, json=["status"]),
    lambda: httpx.Response(200, json={"echo": TOKEN}),
    lambda: httpx.Response(200, content=json.dumps({"echo": TOKEN}).encode().replace(b"atqry_", b"\\u0061tqry_"),
                           headers={"content-type": "application/json"}),
    lambda: httpx.Response(200, json={"rows": "x" * 40000}),
])
def test_unusable_or_leaking_responses_are_refused(callback, make):
    _, response = callback
    response[0] = lambda request: make()
    assert call("catalog") == UNAVAILABLE


def test_transport_error_does_not_leak_authorization(callback, caplog):
    _, response = callback

    def boom(request):
        raise httpx.ConnectError("Bearer " + TOKEN)

    response[0] = boom
    assert call("catalog") == UNAVAILABLE
    assert TOKEN not in caplog.text


def test_create_agent_scopes_query_turns_to_the_query_tool_four_iterations_and_an_output_cap():
    pytest.importorskip("aiohttp")
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={
            "api_key": "synthetic", "base_url": None, "provider": None,
            "api_mode": None, "command": None, "args": []}), \
         patch("gateway.run._resolve_gateway_model", return_value="test/model"), \
         patch("gateway.run._load_gateway_config", return_value={}), \
         patch("hermes_cli.tools_config._get_platform_tools",
               return_value={"web", "terminal", "atlas_import", "atlas_ownership", "atlas_query"}), \
         patch("gateway.run._current_max_iterations", return_value=90), \
         patch("run_agent.AIAgent") as agent:
        adapter._create_agent()
        assert sorted(agent.call_args.kwargs["enabled_toolsets"]) == ["terminal", "web"]
        assert agent.call_args.kwargs["max_iterations"] == 90
        assert agent.call_args.kwargs["max_tokens"] is None
        assert agent.return_value.no_iteration_summary is False
        adapter._create_agent(atlas_query_authorized=True, toolset_scope=["web", "terminal"])
        assert agent.call_args.kwargs["enabled_toolsets"] == ["atlas_query"]
        assert agent.call_args.kwargs["max_iterations"] == 4
        assert agent.call_args.kwargs["max_tokens"] == 4096
        assert agent.return_value.no_iteration_summary is True


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [
    {"atlas_query_capability": "bad"}, {"atlas_query_capability": 42},
    {"atlas_query_capability": "atown_" + "a" * 32},
    {"atlas_query_capability": TOKEN, "atlas_import_capability": "atimp_" + "a" * 32},
    {"atlas_query_capability": TOKEN, "atlas_ownership_capability": "atown_" + "a" * 32},
])
async def test_stream_refuses_malformed_or_combined_capabilities(tmp_path, fields):
    aiohttp = pytest.importorskip("aiohttp")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / "sessions.db")
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._session_db = db
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream)
    sid = db.create_session("bad-capability", "api_server")
    try:
        with patch.object(adapter, "_run_agent") as run:
            async with TestClient(TestServer(app)) as client:
                response = await client.post(f"/api/sessions/{sid}/chat/stream", json={"message": "test", **fields})
                assert response.status == 400
                assert TOKEN not in await response.text()
        run.assert_not_called()
    finally:
        db.close()
