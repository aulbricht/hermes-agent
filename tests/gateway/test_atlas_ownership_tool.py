"""Synthetic ownership transport, real gating/dispatch and executor isolation."""

import asyncio
import copy
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB
from tools import atlas_import_tool as import_tool
from tools import atlas_ownership_tool as tool
from tools.thread_context import propagate_context_to_thread


FIXTURE_BYTES = (Path(__file__).parents[1] / "fixtures/ownership_tool_contract.v1.json").read_bytes()
CONTRACT = json.loads(FIXTURE_BYTES)
TOKEN = "atown_" + "a" * 32
OTHER_TOKEN = "atown_" + "b" * 32
ERROR = {"error": "Ownership operation unavailable"}
OWNERSHIP = {"atlas_ownership", "atlas_ownership_proposal"}
IMPORT = {"atlas_import", "atlas_import_proposal"}


def names(toolsets=None, quiet=True):
    from model_tools import get_tool_definitions
    return {item["function"]["name"] for item in get_tool_definitions(
        enabled_toolsets=toolsets if toolsets is not None else ["atlas_ownership", "atlas_import"],
        quiet_mode=quiet, skip_tool_search_assembly=True)}


def invoke(request):
    if request["operation"] in {"catalog", "evidence"}:
        return tool._handle_atlas_ownership({"operation": request["operation"], "arguments": request["arguments"]})
    return tool._handle_atlas_ownership_proposal({"operation": request["operation"], **request["arguments"]})


def success(operation="catalog"):
    return {"protocol": CONTRACT["protocol"], "ok": True, "operation": operation, "result": {}}


@pytest.fixture
def callback(monkeypatch):
    observed = []
    response = [lambda request: httpx.Response(200, json=success(json.loads(request.content)["operation"]))]
    real_client = httpx.Client

    def receive(request):
        observed.append(request)
        return response[0](request)

    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["timeout"].read == 30
        assert kwargs["timeout"].connect == 2
        return real_client(transport=httpx.MockTransport(receive), **kwargs)

    monkeypatch.setattr(tool.httpx, "Client", factory)
    return observed, response


@pytest.fixture
def adapter(tmp_path):
    db = SessionDB(tmp_path / "sessions.db")
    value = APIServerAdapter(PlatformConfig(enabled=True))
    value._session_db = db
    yield value
    db.close()


def app_for(adapter):
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream)
    return app


def test_fixture_identity():
    assert hashlib.sha256(FIXTURE_BYTES).hexdigest() == "b9611becd2de2a8b655ffc608ff414519f92893575dcf83a0cd1183ff90543cf"


@pytest.mark.parametrize("quiet", [True, False])
def test_visibility_and_both_cache_transitions(quiet):
    assert not (names(quiet=quiet) & (OWNERSHIP | IMPORT))
    for _ in range(2):
        bound = tool.bind_ownership_capability(TOKEN)
        try:
            assert names(quiet=quiet) & (OWNERSHIP | IMPORT) == OWNERSHIP
            assert not names(["atlas_import"], quiet) & OWNERSHIP
        finally:
            tool.clear_ownership_capability(bound)
        assert not names(quiet=quiet) & (OWNERSHIP | IMPORT)
        bound = import_tool.bind_import_capability("atimp_" + "c" * 32)
        try:
            assert names(quiet=quiet) & (OWNERSHIP | IMPORT) == IMPORT
        finally:
            import_tool.clear_import_capability(bound)


def test_create_agent_subtracts_configured_ownership_unless_authorized(adapter):
    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={
            "api_key": "synthetic", "base_url": None, "provider": None,
            "api_mode": None, "command": None, "args": []}), \
         patch("gateway.run._resolve_gateway_model", return_value="test/model"), \
         patch("gateway.run._load_gateway_config", return_value={}), \
         patch("hermes_cli.tools_config._get_platform_tools", return_value={"web", "atlas_import", "atlas_ownership"}), \
         patch("run_agent.AIAgent") as agent:
        adapter._create_agent()
        assert agent.call_args.kwargs["enabled_toolsets"] == ["web"]
        adapter._create_agent(atlas_ownership_authorized=True, toolset_scope=[])
        assert agent.call_args.kwargs["enabled_toolsets"] == ["atlas_ownership"]
        adapter._create_agent(atlas_import_authorized=True)
        assert agent.call_args.kwargs["enabled_toolsets"] == ["web", "atlas_import"]


@pytest.mark.parametrize("wire_request", CONTRACT["requests"])
def test_valid_fixture_references_reach_fixed_callback_unchanged(wire_request, callback, caplog):
    request = wire_request
    observed, _ = callback
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        assert json.loads(invoke(request)) == success(request["operation"])
    finally:
        tool.clear_ownership_capability(bound)
    assert len(observed) == 1
    assert str(observed[0].url) == "http://127.0.0.1:8243/api/v1/internal/ownership-tool"
    assert observed[0].headers["Authorization"] == "Bearer " + TOKEN
    assert observed[0].content == json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("field", ["work_id", "session_id", "turn_id", "principal", "user_id", "token", "callback_url", "database"])
@pytest.mark.parametrize("location", ["top", "proposal", "association", "display", "change", "address"])
def test_authority_injection_refused_at_every_object(field, location, callback):
    observed, _ = callback
    request = copy.deepcopy(CONTRACT["requests"][2])
    proposal = request["arguments"]["proposal"]
    target = {"top": request["arguments"], "proposal": proposal,
              "association": proposal["associations"][0],
              "display": proposal["associations"][0]["display_values"],
              "change": proposal["changes"][0],
              "address": proposal["changes"][2]["proposed_value"]}[location]
    target[field] = "untrusted"
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        assert json.loads(invoke(request)) == ERROR
    finally:
        tool.clear_ownership_capability(bound)
    assert observed == []


@pytest.mark.parametrize("payload", [
    {"operation": "execute", "arguments": {}},
    {"operation": "approve", "arguments": {}},
    {"operation": "lookup", "arguments": {}},
    {"operation": "catalog", "arguments": {"offset": 0}},
    {"operation": "evidence", "arguments": {"offset": True}},
    {"operation": "evidence", "arguments": {"limit": 51}},
    {"operation": "evidence", "arguments": {}, "token": TOKEN},
    {"operation": "evidence", "arguments": {"url": "http://example.invalid"}},
])
def test_invalid_read_arguments_never_dispatch(payload, callback):
    observed, _ = callback
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        assert json.loads(tool._handle_atlas_ownership(payload)) == ERROR
    finally:
        tool.clear_ownership_capability(bound)
    assert not observed


def test_token_in_proposal_and_oversized_valid_shape_are_refused(callback):
    observed, _ = callback
    request = copy.deepcopy(CONTRACT["requests"][2])
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        request["arguments"]["proposal"]["associations"][0]["rationale"] = TOKEN
        assert json.loads(invoke(request)) == ERROR
        request["arguments"]["proposal"]["associations"][0]["rationale"] = "x" * 2000
        request["arguments"]["proposal"]["associations"] *= 100
        assert json.loads(invoke(request)) == ERROR
    finally:
        tool.clear_ownership_capability(bound)
    assert not observed


BAD_RESPONSES = [
    (302, {}, {"Location": "http://example.invalid"}), (403, {"detail": TOKEN}, {}),
    (500, {"detail": TOKEN}, {}), (200, [], {}), (200, {"ok": True}, {}),
    (200, {**success(), "extra": True}, {}), (200, {**success(), "ok": 1}, {}),
    (200, success("evidence"), {}),
    (200, {"protocol": CONTRACT["protocol"], "ok": False, "error": {"code": "OWNERSHIP_UNKNOWN"}}, {}),
    (200, {"protocol": CONTRACT["protocol"], "ok": False, "error": {"code": "OWNERSHIP_STATE_CHANGED", "detail": TOKEN}}, {}),
    (200, {**success(), "result": {"echo": TOKEN}}, {}),
    (200, {**success(), "result": []}, {}),
    (200, success(), {"Content-Type": "text/plain"}),
]


@pytest.mark.parametrize("status,body,headers", BAD_RESPONSES)
def test_opaque_transport_and_shape_failures(status, body, headers, callback, caplog):
    observed, response = callback
    response[0] = lambda request: httpx.Response(status, json=body, headers=headers)
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        result = invoke(CONTRACT["requests"][1])
        assert json.loads(result) == ERROR
    finally:
        tool.clear_ownership_capability(bound)
    assert len(observed) == 1  # including redirects: no follow/retry
    assert TOKEN not in result + caplog.text


@pytest.mark.parametrize("mode", ["timeout", "oversized", "invalid_json", "escaped_token"])
def test_fixed_error_for_bounded_failures(mode, callback, caplog):
    observed, response = callback

    def fail(request):
        if mode == "timeout":
            raise httpx.ReadTimeout(TOKEN, request=request)
        raw = {"oversized": b"x" * 32769, "invalid_json": b"{",
               "escaped_token": json.dumps({**success(), "result": {"echo": TOKEN}}).replace("a", "\\u0061").encode()}[mode]
        return httpx.Response(200, content=raw, headers={"Content-Type": "application/json"})

    response[0] = fail
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        result = invoke(CONTRACT["requests"][1])
        assert json.loads(result) == ERROR
    finally:
        tool.clear_ownership_capability(bound)
    assert len(observed) == 1
    assert TOKEN not in result + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [(TOKEN, None, TOKEN), (None, TOKEN, None)])
async def test_reused_executor_binds_resets_and_denies_effective_authority(adapter, callback, order):
    observed, _ = callback
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(pool)
    seen = []

    class Agent:
        def run_conversation(self, **kwargs):
            seen.append((names(), json.loads(invoke(CONTRACT["requests"][1]))))
            return {"final_response": "done"}

    with patch.object(adapter, "_create_agent", return_value=Agent()):
        for token in order:
            await adapter._run_agent("test", [], atlas_ownership_capability=token)
            assert await loop.run_in_executor(pool, tool.ownership_capability_available) is False
            assert await loop.run_in_executor(pool, lambda: json.loads(invoke(CONTRACT["requests"][1]))) == ERROR
    for token, (visible, result) in zip(order, seen):
        assert visible & OWNERSHIP == (OWNERSHIP if token else set())
        assert result == (success() if token else ERROR)
    assert [r.headers["Authorization"] for r in observed] == ["Bearer " + t for t in order if t]
    pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_concurrent_runs_parallel_workers_and_exception_cleanup(adapter, callback):
    observed, _ = callback
    barrier = threading.Barrier(2)
    pool = ThreadPoolExecutor(max_workers=2)
    loop = asyncio.get_running_loop()
    loop.set_default_executor(pool)

    class Agent:
        def run_conversation(self, user_message, **kwargs):
            assert names() & OWNERSHIP == OWNERSHIP
            barrier.wait(timeout=5)
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = [workers.submit(propagate_context_to_thread(
                    lambda: json.loads(invoke(CONTRACT["requests"][1])))) for _ in range(2)]
                assert [f.result(timeout=5) for f in futures] == [success(), success()]
                assert workers.submit(tool.ownership_capability_available).result() is False
            if user_message == "raise":
                raise RuntimeError("synthetic failure")
            return {"final_response": "done"}

    with patch.object(adapter, "_create_agent", return_value=Agent()):
        results = await asyncio.gather(
            adapter._run_agent("normal", [], atlas_ownership_capability=TOKEN),
            adapter._run_agent("raise", [], atlas_ownership_capability=OTHER_TOKEN), return_exceptions=True)
    assert isinstance(results[1], RuntimeError)
    assert sorted(r.headers["Authorization"] for r in observed) == sorted(["Bearer " + TOKEN] * 2 + ["Bearer " + OTHER_TOKEN] * 2)
    # Occupy both workers so neither cleanup probe can merely reuse the clean one.
    probe_barrier = threading.Barrier(2)

    def probe():
        probe_barrier.wait(timeout=5)
        return tool.ownership_capability_available(), json.loads(invoke(CONTRACT["requests"][1])), names() & OWNERSHIP

    assert await asyncio.gather(*[loop.run_in_executor(pool, probe) for _ in range(2)]) == [(False, ERROR, set())] * 2
    assert not tool.ownership_capability_available()
    pool.shutdown(wait=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [
    {"atlas_ownership_capability": "bad"}, {"atlas_ownership_capability": 42},
    {"atlas_ownership_capability": "atimp_" + "a" * 32},
    {"atlas_ownership_capability": TOKEN, "atlas_import_capability": "atimp_" + "a" * 32},
])
async def test_stream_refuses_malformed_or_combined_capabilities(adapter, fields):
    sid = adapter._session_db.create_session("bad-capability", "api_server")
    with patch.object(adapter, "_run_agent") as run:
        async with TestClient(TestServer(app_for(adapter))) as client:
            response = await client.post(f"/api/sessions/{sid}/chat/stream", json={"message": "test", **fields})
            assert response.status == 400
            assert TOKEN not in await response.text()
    run.assert_not_called()


@pytest.mark.asyncio
async def test_disconnect_keeps_executor_alive_but_revoked_callback_denies(adapter, callback, caplog):
    observed, response = callback
    entered, disconnected, release, ended = [threading.Event() for _ in range(4)]
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(pool)
    seen = []

    class Agent:
        def __init__(self, stream_delta_callback):
            self.delta = stream_delta_callback

        def run_conversation(self, **kwargs):
            entered.set()
            assert disconnected.wait(timeout=5)
            self.delta("detect closed SSE socket")
            assert release.wait(timeout=5)
            try:
                seen.append((names() & OWNERSHIP, json.loads(invoke(CONTRACT["requests"][1]))))
                return {"final_response": "done"}
            finally:
                ended.set()

    response[0] = lambda request: httpx.Response(403, json={"detail": "Ownership capability unavailable"})
    sid = adapter._session_db.create_session("disconnect", "api_server")
    with patch.object(adapter, "_create_agent", side_effect=lambda **kw: Agent(kw["stream_delta_callback"])):
        async with TestClient(TestServer(app_for(adapter))) as client:
            resp = await client.post(f"/api/sessions/{sid}/chat/stream", json={
                "message": "test", "atlas_ownership_capability": TOKEN})
            assert resp.status == 200
            try:
                for _ in range(500):
                    if entered.is_set():
                        break
                    await asyncio.sleep(.01)
                assert entered.is_set()
                resp.close()
                await asyncio.sleep(.05)
                disconnected.set()
                for _ in range(500):
                    if adapter._inflight_agent_runs == 0:
                        break
                    await asyncio.sleep(.01)
                assert adapter._inflight_agent_runs == 0
                assert not ended.is_set()  # cancelling the await cannot interrupt this thread
            finally:
                disconnected.set()
                release.set()
            assert await loop.run_in_executor(pool, tool.ownership_capability_available) is False
    assert ended.is_set() and seen == [(OWNERSHIP, ERROR)]
    assert len(observed) == 1
    assert TOKEN not in caplog.text
    pool.shutdown(wait=True)


@pytest.mark.parametrize("requested", [None, ["atlas_ownership"], ["hermes-api-server", "atlas_ownership"]])
def test_delegated_child_has_neither_definitions_nor_callback_authority(requested, callback, monkeypatch):
    from tools import delegate_tool
    observed, _ = callback
    seen = []

    class Child:
        def __init__(self, **kwargs):
            self.enabled_toolsets = kwargs["enabled_toolsets"]
            self.tool_progress_callback = None
            seen.append(names(self.enabled_toolsets) & OWNERSHIP)

        def run_conversation(self, **kwargs):
            seen.append((names(self.enabled_toolsets) & OWNERSHIP,
                         json.loads(invoke(CONTRACT["requests"][1]))))
            return {"final_response": "done", "completed": True, "messages": []}

    parent = SimpleNamespace(enabled_toolsets=["atlas_ownership"], model="synthetic",
                             base_url="http://example.invalid/v1", provider="openai",
                             _client_kwargs={"api_key": "synthetic"}, _delegate_depth=0)
    monkeypatch.setattr("run_agent.AIAgent", Child)
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        child = delegate_tool._build_child_agent(0, "test", None, requested, None, 2, 1, parent)
        result = delegate_tool._run_single_child(0, "test", child, parent)
        assert result["status"] == "completed"
        assert tool.ownership_capability_available()  # child cleanup cannot revoke parent
    finally:
        tool.clear_ownership_capability(bound)
    assert seen == [set(), (set(), ERROR)]
    assert not observed


STREAM_CASES = [(index, {"status": 200, "body": success(request["operation"])})
                for index, request in enumerate(CONTRACT["requests"])]
STREAM_CASES += [(1, response) for response in CONTRACT["responses"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("request_index,reply", STREAM_CASES)
async def test_scripted_provider_through_real_stream_agent_and_dispatch(
        request_index, reply, adapter, callback, monkeypatch, caplog):
    """H11: real endpoint, _create_agent, AIAgent loop and tool dispatcher.

    Provider, model metadata and callback HTTP are synthetic; no paid/network calls.
    """
    import run_agent
    from openai.types.chat import ChatCompletion

    wire_request = CONTRACT["requests"][request_index]
    observed, response = callback
    response[0] = lambda request: httpx.Response(reply["status"], json=reply["body"])
    provider_calls = []
    tool_name = "atlas_ownership" if request_index < 2 else "atlas_ownership_proposal"
    tool_args = ({"operation": wire_request["operation"], "arguments": wire_request["arguments"]}
                 if request_index < 2 else {"operation": wire_request["operation"], **wire_request["arguments"]})

    def complete(**kwargs):
        provider_calls.append(copy.deepcopy(kwargs))
        if len(provider_calls) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "synthetic-call", "type": "function", "function": {
                    "name": tool_name, "arguments": json.dumps(tool_args)}}]}
            reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": "done"}
            reason = "stop"
        return ChatCompletion(id="synthetic", created=0, model="synthetic-model", object="chat.completion",
                              choices=[{"index": 0, "finish_reason": reason, "message": message}])

    class Client:
        def __init__(self, **kwargs):
            self._client = SimpleNamespace(is_closed=False)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=complete))

        def close(self):
            self._client.is_closed = True

    real_agent = run_agent.AIAgent

    class SyntheticAgent(real_agent):
        def __init__(self, **kwargs):
            super().__init__(**{**kwargs, "skip_memory": True, "skip_context_files": True,
                                "max_iterations": 4})
            self._disable_streaming = True

    # Runtime helpers read AIAgent class constants; preserve its class identity
    # and inherited behavior while configuring this synthetic provider run.
    monkeypatch.setattr(run_agent, "OpenAI", Client)
    monkeypatch.setattr(run_agent, "AIAgent", SyntheticAgent)
    monkeypatch.setattr("agent.agent_init.fetch_model_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr("agent.context_compressor.get_model_context_length", lambda *args, **kwargs: 32000)
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {
        "api_key": "synthetic", "base_url": "http://example.invalid/v1", "provider": "openai",
        "api_mode": "chat_completions", "command": None, "args": []})
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "synthetic-model")
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *args: set())
    sid = adapter._session_db.create_session("fixture-stream", "api_server")
    async with TestClient(TestServer(app_for(adapter))) as client:
        resp = await client.post(f"/api/sessions/{sid}/chat/stream", json={
            "message": "Run the synthetic fixture", "atlas_ownership_capability": TOKEN})
        assert resp.status == 200
        stream = await resp.text()
    assert "event: run.completed" in stream
    assert len(observed) == 1
    assert observed[0].headers["Authorization"] == "Bearer " + TOKEN
    assert observed[0].content == json.dumps(wire_request, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(provider_calls) >= 2
    assert OWNERSHIP <= {item["function"]["name"] for item in provider_calls[0]["tools"]}
    tool_results = [message["content"] for message in provider_calls[-1]["messages"] if message["role"] == "tool"]
    expected = reply["body"] if reply["status"] == 200 else ERROR
    assert any(json.loads(value) == expected for value in tool_results)
    assert TOKEN not in json.dumps(provider_calls) + stream + caplog.text
    assert not tool.ownership_capability_available()
