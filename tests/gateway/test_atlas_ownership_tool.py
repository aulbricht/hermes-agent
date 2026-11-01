"""Synthetic ownership transport, real gating/dispatch and executor isolation."""

import asyncio
import copy
import hashlib
import json
import re
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
WIRE_REQUESTS = (CONTRACT["requests"] + CONTRACT["execution_case"]["requests"]
                 + CONTRACT["transformation_case"]["requests"] + CONTRACT["generic_case"]["requests"])
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


def assert_proposal_refusal(result):
    # Rejection/zero-dispatch assertions stay intact; schema errors now identify
    # a fixed field path, while semantic/transport failures remain opaque.
    assert set(result) == {"error"}
    assert result == ERROR or re.fullmatch(
        r"arguments(?:\.[a-z_]+(?:\[\])?)*: not accepted", result["error"])
    assert TOKEN not in result["error"]


@pytest.mark.parametrize("operation", ["propose", "revise"])
@pytest.mark.parametrize("role", ["Legal owner", "x" * 240])
def test_owner_role_with_execution_plan_and_transformations_forwards(operation, role, callback):
    from model_tools import handle_function_call

    request = copy.deepcopy(CONTRACT["transformation_case"]["requests"][0 if operation == "propose" else 1])
    proposal = request["arguments"]["proposal"]
    assert proposal["execution_plan"] and proposal["transformations"]
    proposal["associations"][0]["role"] = role
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        assert json.loads(handle_function_call("atlas_ownership_proposal", {
            "operation": operation, **request["arguments"]})) == success(operation)
    finally:
        tool.clear_ownership_capability(bound)
    assert len(callback[0]) == 1
    assert json.loads(callback[0][0].content) == request


@pytest.mark.parametrize("role", ["", "   ", "private-value" * 21, None, True])
def test_invalid_role_names_only_failed_field(role, callback):
    request = copy.deepcopy(CONTRACT["transformation_case"]["requests"][0])
    request["arguments"]["proposal"]["associations"][0]["role"] = role
    assert json.loads(invoke(request)) == {
        "error": "arguments.proposal.associations[].role: not accepted"}
    assert not callback[0]


@pytest.mark.parametrize("mutation,field", [
    ("missing", "arguments.proposal.evidence_hash"),
    ("extra", "arguments.proposal.associations[]"),
    ("dynamic", "arguments.proposal.execution_plan[].fields"),
    ("type", "arguments.expected_version"),
])
def test_schema_errors_do_not_echo_customer_values_or_dynamic_keys(mutation, field, callback):
    request = copy.deepcopy(CONTRACT["transformation_case"]["requests"][0])
    proposal = request["arguments"]["proposal"]
    if mutation == "missing":
        del proposal["evidence_hash"]
    elif mutation == "extra":
        proposal["associations"][0]["private-customer-key"] = "private-customer-value"
    elif mutation == "dynamic":
        proposal["execution_plan"][0]["fields"]["private-customer-key"] = True
    else:
        request["arguments"]["expected_version"] = "private-customer-value"
    assert json.loads(invoke(request)) == {"error": field + ": not accepted"}
    assert not callback[0]


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

    # Patch only this tool's namespace. OpenAI subclasses the real httpx.Client
    # on first import; replacing that global class makes test order observable.
    monkeypatch.setattr(tool, "httpx", SimpleNamespace(
        Client=factory, Timeout=httpx.Timeout, HTTPError=httpx.HTTPError))
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
    assert hashlib.sha256(FIXTURE_BYTES).hexdigest() == "df58c7504823e871f2be2bab50f8fa7244155e70bbe2a22d2869f99ad5da858f"


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


@pytest.mark.parametrize("scope", [None, [], ["hermes-api-server", "web", "browser", "terminal", "memory", "cronjob", "delegation"]])
def test_ownership_authority_restricts_real_configured_tool_assembly(adapter, scope):
    from model_tools import get_tool_definitions

    def assemble(**kwargs):
        assert kwargs["enabled_toolsets"] == ["atlas_ownership"]
        return get_tool_definitions(enabled_toolsets=kwargs["enabled_toolsets"], quiet_mode=True)

    bound = tool.bind_ownership_capability(TOKEN)
    try:
        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={}), \
             patch("gateway.run._resolve_gateway_model", return_value="test/model"), \
             patch("gateway.run._load_gateway_config", return_value={
                 "platform_toolsets": {"api_server": ["hermes-api-server", "delegation", "atlas_import"]}}), \
             patch("run_agent.AIAgent", side_effect=assemble):
            definitions = adapter._create_agent(atlas_ownership_authorized=True, toolset_scope=scope)
        assert {item["function"]["name"] for item in definitions} == OWNERSHIP
    finally:
        tool.clear_ownership_capability(bound)
    assert not tool.ownership_capability_available()


@pytest.mark.parametrize("wire_request", WIRE_REQUESTS)
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
@pytest.mark.parametrize("location", ["top", "proposal", "association", "display", "change", "address", "action", "dependency", "fields", "probe"])
def test_authority_injection_refused_at_every_object(field, location, callback):
    observed, _ = callback
    request = copy.deepcopy(CONTRACT["requests"][2])
    proposal = request["arguments"]["proposal"]
    target = {"top": request["arguments"], "proposal": proposal,
              "association": proposal["associations"][0],
              "display": proposal["associations"][0]["display_values"],
              "change": proposal["changes"][0],
              "address": proposal["changes"][2]["proposed_value"],
              "action": proposal["execution_plan"][0],
              "dependency": proposal["execution_plan"][1]["dependencies"][0],
              "fields": proposal["execution_plan"][0]["fields"],
              "probe": proposal["execution_plan"][0]["duplicate_probe"]}[location]
    target[field] = "untrusted"
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        assert_proposal_refusal(json.loads(invoke(request)))
    finally:
        tool.clear_ownership_capability(bound)
    assert observed == []


@pytest.mark.parametrize("operation", ["propose", "revise"])
def test_execution_plan_schema_and_real_dispatch_preserve_agent_choices(operation, callback):
    from model_tools import get_tool_definitions, handle_function_call

    request = copy.deepcopy(CONTRACT["requests"][2 if operation == "propose" else 3])
    # A previously unseen field name passes only inside the two bounded maps.
    # Hermes transports agent choices; it does not maintain a field allowlist.
    action = request["arguments"]["proposal"]["execution_plan"][0]
    action["fields"]["agentChosenField"] = "Agent chosen value"
    action["duplicate_probe"]["agentChosenField"] = "Agent chosen value"
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        definitions = get_tool_definitions(enabled_toolsets=["atlas_ownership"], quiet_mode=True)
        parameters = next(d["function"]["parameters"] for d in definitions
                          if d["function"]["name"] == "atlas_ownership_proposal")
        args = {"operation": operation, **request["arguments"]}
        # Definitions are provider-sanitized (nullable unions become hints).
        # Validate raw arguments against the handler's schema, and check the
        # emitted schema still offers plans/maps and nullable business IDs.
        assert tool._matches(args, tool._PROPOSAL_TOOL_SCHEMA)
        emitted = parameters["properties"]["proposal"]["properties"]
        action_schema = emitted["execution_plan"]["items"]
        for slot in ("fields", "duplicate_probe"):
            assert action_schema["properties"][slot]["additionalProperties"]["type"] == "string"
            assert action_schema["properties"][slot]["properties"] == {}
        assert emitted["associations"]["items"]["properties"]["contact_id"]["nullable"] is True
        assert action_schema["properties"]["target_id"]["nullable"] is True
        assert json.loads(handle_function_call("atlas_ownership_proposal", args)) == success(operation)
    finally:
        tool.clear_ownership_capability(bound)
    observed, _ = callback
    assert len(observed) == 1
    assert json.loads(observed[0].content) == request
    assert not tool.ownership_capability_available()


@pytest.mark.parametrize("field", ["id", "owner_id", "principal_id", "capability",
                                    "atlas_ownership_capability", "atlas_import_capability"])
@pytest.mark.parametrize("location", ["fields", "duplicate_probe"])
def test_execution_plan_field_maps_refuse_authority_keys(field, location, callback):
    request = copy.deepcopy(CONTRACT["requests"][2])
    request["arguments"]["proposal"]["execution_plan"][0][location][field] = "foreign"
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        assert_proposal_refusal(json.loads(invoke(request)))
    finally:
        tool.clear_ownership_capability(bound)
    assert not callback[0]


@pytest.mark.parametrize("operation", ["propose", "revise"])
@pytest.mark.parametrize("with_plan", [False, True])
def test_optional_plan_and_scoped_update_reference_forward_unchanged(operation, with_plan, callback):
    request = copy.deepcopy(CONTRACT["requests"][2 if operation == "propose" else 3])
    proposal = request["arguments"]["proposal"]
    if with_plan:
        # target_id is only an untrusted business reference, checked by Atlas.
        proposal["execution_plan"][0].update(action="update", target_id="existing-contact")
    else:
        del proposal["execution_plan"]
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        assert json.loads(invoke(request)) == success(operation)
    finally:
        tool.clear_ownership_capability(bound)
    assert json.loads(callback[0][0].content) == request


@pytest.mark.parametrize("location,value", [
    ("plan", []), ("plan", {}), ("plan", [None]),
    ("plan", [{"operation": "execute"}]),
    ("table", "API_User"), ("action", "delete"), ("target_id", True),
    ("fields", {}), ("fields", {"nested": {"owner_id": "foreign"}}),
    ("fields", {"numeric": 1}), ("fields", {"long": "x" * 2001}),
    ("fields", {str(i): "v" for i in range(101)}),
    ("fields", {"secret": TOKEN}), ("probe", {"secret": TOKEN}),
    ("probe", {"nested": []}), ("dependency", {"field": "owner_id", "action_id": "contact"}),
    ("dependency", {"field": "id__contact", "action_id": TOKEN}),
    ("holds", ["unresolved"]),
])
def test_invalid_execution_plan_never_dispatches(location, value, callback):
    request = copy.deepcopy(CONTRACT["requests"][2])
    proposal = request["arguments"]["proposal"]
    if location == "plan":
        proposal["execution_plan"] = value
    elif location == "dependency":
        proposal["execution_plan"][1]["dependencies"] = [value]
    else:
        key = "duplicate_probe" if location == "probe" else location
        proposal["execution_plan"][0][key] = value
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        assert_proposal_refusal(json.loads(invoke(request)))
    finally:
        tool.clear_ownership_capability(bound)
    assert not callback[0]


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
        assert_proposal_refusal(json.loads(invoke(request)))
        request["arguments"]["proposal"]["associations"][0]["rationale"] = "x" * 2000
        request["arguments"]["proposal"]["associations"] *= 100
        assert_proposal_refusal(json.loads(invoke(request)))
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


@pytest.mark.parametrize("operation,size,accepted", [
    ("catalog", 48387, True),
    ("catalog", 65536, True),
    ("catalog", 65537, False),
    ("evidence", 32768, True),
    ("evidence", 32769, False),
])
def test_catalog_headroom_and_other_operation_bounds(operation, size, accepted, callback):
    observed, response = callback
    body = {**success(operation), "result": {"synthetic": ""}}
    overhead = len(json.dumps(body, separators=(",", ":")).encode())
    body["result"]["synthetic"] = "x" * (size - overhead)
    raw = json.dumps(body, separators=(",", ":")).encode()
    assert len(raw) == size

    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            for start in range(0, len(raw), 4096):
                yield raw[start:start + 4096]

    response[0] = lambda request: httpx.Response(200, stream=Chunks(),
                                               headers={"Content-Type": "application/json"})
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        result = tool._handle_atlas_ownership({"operation": operation, "arguments": {}})
    finally:
        tool.clear_ownership_capability(bound)
    assert json.loads(result) == (body if accepted else ERROR)
    if accepted:
        # The agent must receive the whole accepted response, without truncation.
        assert len(result) <= tool.registry.get_max_result_size("atlas_ownership")
    assert len(observed) == 1


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


@pytest.mark.parametrize("operation", ["propose", "revise"])
def test_transformations_real_definitions_and_dispatch(operation, callback):
    from model_tools import get_tool_definitions, handle_function_call

    request = copy.deepcopy(CONTRACT["transformation_case"]["requests"][0 if operation == "propose" else 1])
    # An unseen business field remains data; no semantic field allowlist.
    proposal = request["arguments"]["proposal"]
    transform = proposal["transformations"][0]
    value = proposal["execution_plan"][0]["fields"].pop(transform["field"])
    transform["field"] = "newAgentChosenField"
    proposal["execution_plan"][0]["fields"][transform["field"]] = value
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        definitions = get_tool_definitions(enabled_toolsets=["atlas_ownership"], quiet_mode=True)
        schema = next(d["function"]["parameters"] for d in definitions
                      if d["function"]["name"] == "atlas_ownership_proposal")
        entry = schema["properties"]["proposal"]["properties"]["transformations"]["items"]
        assert entry["additionalProperties"] is False
        assert entry["properties"]["source_inputs"]["items"]["additionalProperties"] is False
        assert set(entry["required"]) == set(transform)
        assert json.loads(handle_function_call("atlas_ownership_proposal", {
            "operation": operation, **request["arguments"]})) == success(operation)
    finally:
        tool.clear_ownership_capability(bound)
    assert json.loads(callback[0][0].content) == request


def invalid_transformation_request(location, value):
    request = copy.deepcopy(CONTRACT["transformation_case"]["requests"][0])
    proposal = request["arguments"]["proposal"]
    item = proposal["transformations"][0]
    if location == "transformations":
        proposal["transformations"] = value
    elif location == "missing":
        del item[value]
    elif location == "extra":
        item[value] = "forged"
    elif location == "input_extra":
        item["source_inputs"][0][value] = "forged"
    elif location == "path":
        item["source_inputs"][0]["path"] = value
    elif location == "duplicate":
        duplicate = copy.deepcopy(item)
        duplicate["id"] = value
        proposal["transformations"].append(duplicate)
    else:
        item[location] = value
        # Keep an exact action match: refusal must not rely on mismatch alone.
        if location == "proposed_value":
            proposal["execution_plan"][0]["fields"][item["field"]] = value
        if location == "field":
            proposal["execution_plan"][0]["fields"][value] = item["proposed_value"]
    return request


INVALID_TRANSFORMATIONS = [
    ("transformations", {}), ("transformations", [None]),
    ("missing", "rationale"), ("missing", "guidance_revision"), ("missing", "catalog_revision"),
    ("extra", "unknown"), ("extra", "target_id"), ("extra", "owner_id"),
    ("input_extra", "value"), ("input_extra", "work_id"), ("input_extra", "callback_url"),
    ("path", ""), ("path", "name"), ("path", "/bad~2escape"), ("path", "/bad~"),
    ("source_inputs", []), ("source_inputs", [{"source_ref": "saved:1", "path": "/name"}] * 11),
    ("source_ref", "foreign"), ("association_index", True), ("association_index", 1),
    ("action_id", "unknown"), ("table", "API_User"), ("table", "API_ContactDetail"),
    ("field", "target_id"), ("field", "id"), ("field", "id__contact"),
    ("field", "owner_id"), ("field", "a.b"), ("field", "a" * 102),
    ("proposed_value", ""), ("proposed_value", "   "), ("proposed_value", "x" * 2001),
    ("proposed_value", TOKEN),
    ("depends_on", []), ("depends_on", ["name", "name"]), ("depends_on", ["missing"]),
    ("depends_on", ["x" * 161]), ("uncertainty", ["x"] * 11),
    ("uncertainty", ["x" * 2001]), ("duplicate", "different-id"), ("duplicate", "contact-category"),
] + [("proposed_value", "visible" + char) for char in (
    "\x00", "\n", "\x7f", "\u0085", "\u202e", "\u200b", "\u200d", "\ufeff", "\ud800",
    "\u034f", "\u115f", "\u1160", "\u17b4", "\u17b5", "\u3164", "\uffa0",
    "\ufe00", "\ufe0f", "\U000e0100", "\U000e01ef")]


@pytest.mark.parametrize("location,value", INVALID_TRANSFORMATIONS)
def test_invalid_transformations_real_dispatch_refuses_before_callback(location, value, callback):
    from model_tools import handle_function_call

    request = invalid_transformation_request(location, value)
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        assert_proposal_refusal(json.loads(handle_function_call("atlas_ownership_proposal", {
            "operation": "propose", **request["arguments"]})))
    finally:
        tool.clear_ownership_capability(bound)
    assert not callback[0]


@pytest.mark.parametrize("mutation", ["value_mismatch", "field_reuse", "dependency_source",
                                      "dependency_association", "duplicate_action", "duplicate_change"])
def test_transformation_relationships_refused_without_authority(mutation, callback):
    request = copy.deepcopy(CONTRACT["transformation_case"]["requests"][0])
    proposal = request["arguments"]["proposal"]
    item = proposal["transformations"][0]
    if mutation == "value_mismatch":
        proposal["execution_plan"][0]["fields"][item["field"]] = "different"
    elif mutation == "field_reuse":
        item["field"] = "organizationName"  # Value elsewhere grants no field reuse.
    elif mutation == "dependency_source":
        proposal["changes"][0]["source_ref"] = "other"
    elif mutation == "dependency_association":
        proposal["changes"][0]["association_index"] = 1
    elif mutation == "duplicate_action":
        proposal["execution_plan"].append(copy.deepcopy(proposal["execution_plan"][0]))
    else:
        proposal["changes"].append(copy.deepcopy(proposal["changes"][0]))
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        assert_proposal_refusal(json.loads(invoke(request)))
    finally:
        tool.clear_ownership_capability(bound)
    assert not callback[0]


@pytest.mark.parametrize("value", ["é" * 2000, "😀" * 2000, '<script>alert("proposal")</script>'])
def test_transformation_value_codepoint_bound_and_text_preserved(value, callback):
    request = copy.deepcopy(CONTRACT["transformation_case"]["requests"][0])
    proposal = request["arguments"]["proposal"]
    item = proposal["transformations"][0]
    item["proposed_value"] = value
    proposal["execution_plan"][0]["fields"][item["field"]] = value
    # Selectors only: escaped JSON Pointer keys remain literal for Atlas to resolve.
    item["source_inputs"][0]["path"] = "/a~1b/~0name"
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        assert json.loads(invoke(request)) == success("propose")
    finally:
        tool.clear_ownership_capability(bound)
    assert json.loads(callback[0][0].content) == request


def test_transformation_count_and_envelope_byte_caps(callback):
    request = copy.deepcopy(CONTRACT["transformation_case"]["requests"][0])
    proposal = request["arguments"]["proposal"]
    original = proposal["transformations"][0]
    proposal["transformations"] = []
    for i in range(100):
        item = copy.deepcopy(original)
        item.update(id=f"transform{i}", field=f"field{i}", proposed_value="😀" * 2000)
        proposal["execution_plan"][0]["fields"][item["field"]] = item["proposed_value"]
        proposal["transformations"].append(item)
    # Keep maps within bounds while exceeding the enclosing byte cap.
    proposal["execution_plan"][0]["fields"].pop(original["field"])
    proposal["execution_plan"][0]["fields"].pop("organizationName")
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        assert tool._matches({"operation": "propose", **request["arguments"]}, tool._PROPOSAL_TOOL_SCHEMA)
        assert_proposal_refusal(json.loads(invoke(request)))
        proposal["transformations"].append(copy.deepcopy(original))
        assert_proposal_refusal(json.loads(invoke(request)))
    finally:
        tool.clear_ownership_capability(bound)
    assert not callback[0]


@pytest.mark.parametrize("operation", ["propose", "revise"])
def test_generic_real_definitions_and_dispatch_preserve_raw_and_held_candidate(operation, callback):
    from model_tools import get_tool_definitions, handle_function_call

    case = CONTRACT["generic_case"]
    request = copy.deepcopy(case["requests"][0 if operation == "propose" else 1])
    proposal = request["arguments"]["proposal"]
    phone, uncertain = proposal["changes"][-2:]
    assert phone["proposed_value"] == case["source_values"]["generic"]["phone"]
    assert uncertain["proposed_value"] is None and uncertain["uncertainty"]
    assert case["source_values"]["generic"]["phone_uncertain"].endswith("ext ?")
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        definitions = get_tool_definitions(enabled_toolsets=["atlas_ownership"], quiet_mode=True)
        schema = next(d["function"]["parameters"] for d in definitions
                      if d["function"]["name"] == "atlas_ownership_proposal")
        variants = schema["properties"]["proposal"]["properties"]["changes"]["items"]["anyOf"]
        generic = next(v for v in variants if v["properties"]["kind"]["enum"] == ["generic"])
        legacy = next(v for v in variants if "name" in v["properties"]["kind"]["enum"])
        assert generic["additionalProperties"] is False
        assert set(generic["required"]) == set(phone)
        assert "source_path" not in legacy["properties"]
        assert json.loads(handle_function_call("atlas_ownership_proposal", {
            "operation": operation, **request["arguments"]})) == success(operation)
    finally:
        tool.clear_ownership_capability(bound)
    assert json.loads(callback[0][0].content) == request
    assert not tool.ownership_capability_available()


def invalid_generic_request(location, value):
    request = copy.deepcopy(CONTRACT["generic_case"]["requests"][0])
    proposal = request["arguments"]["proposal"]
    item = proposal["changes"][-2]
    if location == "extra":
        item[value] = "untrusted"
    elif location == "missing":
        del item[value]
    elif location == "legacy_path":
        proposal["changes"][0]["source_path"] = value
    elif location == "selector_path":
        proposal["transformations"][0]["source_inputs"][0]["path"] = value
    elif location == "selector_source":
        proposal["transformations"][0]["source_inputs"][0]["source_ref"] = value
    elif location == "transform_depends_on":
        proposal["transformations"][0]["depends_on"] = value
    elif location == "duplicate_target":
        proposal["changes"][-1]["source_path"] = item["source_path"]
    else:
        item[location] = value
    return request


INVALID_GENERIC = [
    ("extra", key) for key in ("work_id", "session_id", "turn_id", "principal", "user_id",
                              "owner_id", "token", "callback_url", "database", "source_input", "value")
] + [
    ("missing", "source_path"), ("missing", "proposed_value"),
    ("missing", "possible_cleaned_value"), ("missing", "rationale"),
    ("source_ref", "foreign"), ("association_index", True), ("association_index", 1),
    ("id", "name"), ("duplicate_target", None),
    ("kind", "phone"), ("kind", "name"), ("kind", "email"), ("kind", "address"),
    ("legacy_path", "/generic/phone"),
    ("possible_cleaned_value", "2025550100"),
    ("proposed_value", ""), ("proposed_value", "   "), ("proposed_value", "x" * 2001),
    ("proposed_value", TOKEN), ("proposed_value", {"value": "untrusted"}),
    ("proposed_value", "phone\u202e"), ("proposed_value", "phone\u200b"),
    ("depends_on", ["missing"]), ("depends_on", ["phone"]),
    ("depends_on", ["uncertain-phone"]), ("depends_on", ["name", "name"]),
    ("depends_on", ["name"] * 101),
    ("uncertainty", ["x"] * 11), ("uncertainty", ["x" * 2001]),
    ("rationale", "x" * 2001),
    ("selector_path", "/generic/phone_uncertain"), ("selector_source", "foreign"),
    ("transform_depends_on", ["name"]), ("transform_depends_on", ["uncertain-phone"]),
] + [("source_path", path) for path in (
    "", "/generic/", "generic/phone", "/phone", "/email", "/name", "/address/line1",
    "/generic/bad~2", "/generic/bad~", "/generic/" + "x" * 232, TOKEN,
)]


@pytest.mark.parametrize("location,value", INVALID_GENERIC)
def test_generic_invalid_shape_and_local_references_refuse_before_callback(location, value, callback):
    from model_tools import handle_function_call

    request = invalid_generic_request(location, value)
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        assert_proposal_refusal(json.loads(handle_function_call("atlas_ownership_proposal", {
            "operation": "propose", **request["arguments"]})))
    finally:
        tool.clear_ownership_capability(bound)
    assert not callback[0]


@pytest.mark.parametrize("path", ["/generic/a~1b/c~0d", "/generic/" + "x" * 231])
def test_generic_pointer_escaping_and_bound_forward_without_local_resolution(path, callback):
    request = copy.deepcopy(CONTRACT["generic_case"]["requests"][0])
    proposal = request["arguments"]["proposal"]
    proposal["changes"][-2]["source_path"] = path
    for item in proposal["transformations"]:
        item["source_inputs"][0]["path"] = path
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        assert json.loads(invoke(request)) == success("propose")
    finally:
        tool.clear_ownership_capability(bound)
    # Only Atlas can establish whether a syntactically valid path exists.
    assert json.loads(callback[0][0].content) == request


def test_generic_dependencies_cannot_rebind_to_other_source_or_association(callback):
    request = copy.deepcopy(CONTRACT["generic_case"]["requests"][0])
    proposal = request["arguments"]["proposal"]
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        for key, value in [("source_ref", "foreign"), ("association_index", 1)]:
            prior = proposal["changes"][0]
            original = prior[key]
            prior[key] = value
            assert json.loads(invoke(request)) == ERROR
            prior[key] = original
    finally:
        tool.clear_ownership_capability(bound)
    assert not callback[0]


def test_generic_count_unicode_and_enclosing_byte_limits(callback):
    request = copy.deepcopy(CONTRACT["generic_case"]["requests"][0])
    proposal = request["arguments"]["proposal"]
    original = proposal["changes"][-2]
    proposal["changes"] = proposal["changes"][:3]
    proposal.pop("transformations")
    for i in range(97):
        item = {**original, "id": f"generic{i}", "source_path": f"/generic/{i}"}
        proposal["changes"].append(item)
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        assert json.loads(invoke(request)) == success("propose")
        proposal["changes"].append({**original, "id": "overflow", "source_path": "/generic/overflow"})
        assert_proposal_refusal(json.loads(invoke(request)))
        proposal["changes"].pop()
        for item in proposal["changes"][3:]:
            item["proposed_value"] = "😀" * 2000
        assert tool._matches({"operation": "propose", **request["arguments"]}, tool._PROPOSAL_TOOL_SCHEMA)
        assert_proposal_refusal(json.loads(invoke(request)))
    finally:
        tool.clear_ownership_capability(bound)
    assert len(callback[0]) == 1


STREAM_CASES = [(index, {"status": 200, "body": success(request["operation"])})
                for index, request in enumerate(WIRE_REQUESTS)]
STREAM_CASES += [(invalid_generic_request(location, value), {"local_refusal": True})
                 for location, value in [("extra", "owner_id"), ("source_path", "/email"),
                                         ("transform_depends_on", ["name"])]]
STREAM_CASES += [(1, response) for response in CONTRACT["responses"]]
STREAM_CASES += [(invalid_transformation_request(location, value), {"local_refusal": True})
                 for location, value in [("extra", "target_id"), ("field", "id__contact"),
                                         ("proposed_value", "bad\u202e"), ("duplicate", "duplicate")]]


@pytest.mark.asyncio
@pytest.mark.parametrize("request_index,reply", STREAM_CASES)
async def test_scripted_provider_through_real_stream_agent_and_dispatch(
        request_index, reply, adapter, callback, monkeypatch, caplog):
    """H11: real endpoint, _create_agent, AIAgent loop and tool dispatcher.

    Provider, model metadata and callback HTTP are synthetic; no paid/network calls.
    """
    import run_agent
    from openai.types.chat import ChatCompletion
    from agent.model_metadata import MINIMUM_CONTEXT_LENGTH

    wire_request = request_index if isinstance(request_index, dict) else WIRE_REQUESTS[request_index]
    observed, response = callback
    local_refusal = reply.get("local_refusal", False)
    if not local_refusal:
        response[0] = lambda request: httpx.Response(reply["status"], json=reply["body"])
    provider_calls = []
    read = wire_request["operation"] in {"evidence", "catalog"}
    tool_name = "atlas_ownership" if read else "atlas_ownership_proposal"
    tool_args = ({"operation": wire_request["operation"], "arguments": wire_request["arguments"]}
                 if read else {"operation": wire_request["operation"], **wire_request["arguments"]})

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
    monkeypatch.setattr("agent.context_compressor.get_model_context_length", lambda *args, **kwargs: MINIMUM_CONTEXT_LENGTH * 2)
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {
        "api_key": "synthetic", "base_url": "http://example.invalid/v1", "provider": "openai",
        "api_mode": "chat_completions", "command": None, "args": []})
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "synthetic-model")
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {
        "platform_toolsets": {"api_server": ["hermes-api-server", "delegation", "atlas_import"]}})
    sid = adapter._session_db.create_session("fixture-stream", "api_server")
    async with TestClient(TestServer(app_for(adapter))) as client:
        resp = await client.post(f"/api/sessions/{sid}/chat/stream", json={
            "message": "Run the synthetic fixture", "atlas_ownership_capability": TOKEN})
        assert resp.status == 200
        stream = await resp.text()
    assert "event: run.completed" in stream
    if local_refusal:
        assert observed == []
    else:
        assert len(observed) == 1
        assert observed[0].headers["Authorization"] == "Bearer " + TOKEN
        assert observed[0].content == json.dumps(wire_request, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(provider_calls) >= 2
    for call in provider_calls:
        assert {item["function"]["name"] for item in call["tools"]} == OWNERSHIP
    tool_results = [message["content"] for message in provider_calls[-1]["messages"] if message["role"] == "tool"]
    expected = reply["body"] if not local_refusal and reply["status"] == 200 else ERROR
    if local_refusal:
        assert len(tool_results) == 1
        assert_proposal_refusal(json.loads(tool_results[0]))
    else:
        assert any(json.loads(value) == expected for value in tool_results)
    assert TOKEN not in json.dumps(provider_calls) + stream + caplog.text
    assert not tool.ownership_capability_available()


def test_mismatch_rule_survives_and_same_turn_retry_forwards(callback):
    request = copy.deepcopy(CONTRACT['transformation_case']['requests'][0])
    refusal = {'protocol': CONTRACT['protocol'], 'ok': False, 'error': {
        'code': 'OWNERSHIP_EVIDENCE_MISMATCH', 'rule': 'transformation_source_dependency'}}
    callback[1][0] = lambda req: httpx.Response(200, json=refusal if len(callback[0]) == 1 else success('propose'))
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        assert json.loads(invoke(request)) == refusal
        assert json.loads(invoke(request)) == success('propose')
    finally:
        tool.clear_ownership_capability(bound)
    assert len(callback[0]) == 2
    assert all(req.headers['Authorization'] == 'Bearer ' + TOKEN for req in callback[0])


@pytest.mark.parametrize('error', [
    {'code': 'OWNERSHIP_EVIDENCE_MISMATCH', 'rule': 'Private value!'},
    {'code': 'OWNERSHIP_EVIDENCE_MISMATCH', 'rule': 'x' * 97},
    {'code': 'OWNERSHIP_EVIDENCE_MISMATCH', 'rule': None},
    {'code': 'OWNERSHIP_EVIDENCE_MISMATCH', 'rule': 'valid_rule', 'value': 'private'},
    {'code': 'OWNERSHIP_UNAVAILABLE', 'rule': 'valid_rule'},
])
def test_invalid_rule_envelopes_remain_unavailable(error, callback):
    callback[1][0] = lambda req: httpx.Response(200, json={
        'protocol': CONTRACT['protocol'], 'ok': False, 'error': error})
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        assert json.loads(invoke(CONTRACT['requests'][0])) == ERROR
    finally:
        tool.clear_ownership_capability(bound)
