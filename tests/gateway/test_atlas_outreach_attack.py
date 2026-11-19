"""SNO-3851: proposed outreach contract; synthetic data and offline transports."""
import asyncio
import copy
import json
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB
from model_tools import get_tool_definitions, handle_function_call
from tools import atlas_outreach_tool as tool
from tools.registry import registry

TOKEN = "atout_" + "a" * 48
OTHER = "atout_" + "b" * 48
PROTOCOL = "atlas-outreach-drafts-v1"
ERROR = {"error": "Outreach operation unavailable"}
# Owner ID/name copied from Atlas lightbox_contacts.synthetic.json:31,34.
# All draft text/property references below are labelled synthetic mutations.
PAYLOAD = {
    "subject_property_id": "PRO-00000000-0000-0000-0000-000000000001",
    "owner_id": "1204AAAAAAAAAAAAAAAAAA",
    "email_subject": "Synthetic outreach",
    "email_body": "Synthetic draft for review.",
    "call_script": "Synthetic script for review.",
    "facts": [{"source_ref": "lightbox_contacts.synthetic.json",
               "path": "/data/0/contacts/1/firstName", "value": "ALEX"}],
}
SUCCESS = {"protocol": PROTOCOL, "ok": True,
           "result": {"id": "draft-offline", "run_id": "run-offline"}}
FORBIDDEN = [
    ("web_search", {"query": "synthetic"}),
    ("terminal", {"command": ":"}),
    ("atlas_email", {}),
    ("atlas_ownership_proposal", {}),
    ("atlas_query", {}),
    ("tool_call", {"name": "web_search", "arguments": {"query": "synthetic"}}),
]


def names(scopes):
    return {v["function"]["name"] for v in get_tool_definitions(
        enabled_toolsets=scopes, quiet_mode=True, skip_tool_search_assembly=True)}


def save():
    return json.loads(handle_function_call(
        "atlas_outreach_save", copy.deepcopy(PAYLOAD),
        enabled_tools=["atlas_outreach_save"], enabled_toolsets=["atlas_outreach"]))



@pytest.fixture(autouse=True)
def no_external_network_or_process(monkeypatch):
    attempts = []
    real_connect = socket.socket.connect

    def connect(sock, address):
        if (isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}
                and address[1] not in {8243, 8093}):
            return real_connect(sock, address)
        attempts.append("socket")
        raise AssertionError("Unexpected non-test-server network")

    def process(*args, **kwargs):
        attempts.append("process")
        raise AssertionError("Unexpected subprocess")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(subprocess, "Popen", process)
    yield
    assert attempts == []

@pytest.fixture
def callback(monkeypatch):
    observed = []
    status = [200]
    client_type = httpx.Client

    def receive(request):
        assert request.method == "POST"
        assert str(request.url) == "http://127.0.0.1:8243/api/v1/internal/outreach-draft-tool"
        observed.append(request)
        return httpx.Response(status[0], json=SUCCESS if status[0] == 200 else
                              {"detail": "Outreach capability unavailable"})

    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["timeout"].connect == 2
        assert kwargs["timeout"].read == 30
        return client_type(transport=httpx.MockTransport(receive), **kwargs)

    monkeypatch.setattr(tool, "httpx", SimpleNamespace(
        Client=factory, Timeout=httpx.Timeout, HTTPError=httpx.HTTPError))
    return observed, status


@pytest.fixture
def adapter(tmp_path):
    db = SessionDB(tmp_path / "sessions.db")
    value = APIServerAdapter(PlatformConfig(enabled=True))
    value._session_db = db
    yield value
    db.close()


def app_for(value):
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/chat/stream",
                        value._handle_session_chat_stream)
    return app


def test_definition_cache_transitions_and_fixed_callback(callback):
    observed, _ = callback
    for _ in range(2):
        assert "atlas_outreach_save" not in names(["atlas_outreach"])
        bound = tool.bind_outreach_capability(TOKEN)
        try:
            assert names(["atlas_outreach"]) == {"atlas_outreach_save"}
            assert "atlas_outreach_save" not in names(["web"])
            assert save() == SUCCESS
        finally:
            tool.clear_outreach_capability(bound)
        assert not tool.outreach_capability_available()
        assert "atlas_outreach_save" not in names(["atlas_outreach"])
        assert save() == ERROR
    assert len(observed) == 2
    assert all(r.headers["Authorization"] == "Bearer " + TOKEN for r in observed)
    assert all(json.loads(r.content) == {
        "protocol": PROTOCOL, "operation": "save", "arguments": PAYLOAD} for r in observed)


@pytest.mark.parametrize("name,args", FORBIDDEN)
def test_bound_dispatch_denies_before_middleware_or_registry(callback, monkeypatch, name, args):
    effects = []
    real_dispatch = registry.dispatch

    def dispatch(selected, arguments, **kwargs):
        if selected != "atlas_outreach_save":
            effects.append(selected)
            return json.dumps({"error": "synthetic effect trap"})
        return real_dispatch(selected, arguments, **kwargs)

    def hook(selected, *_args, **_kwargs):
        effects.append("hook:" + selected)
        return None

    monkeypatch.setattr(registry, "dispatch", dispatch)
    monkeypatch.setattr("hermes_cli.plugins.resolve_pre_tool_block", hook)
    bound = tool.bind_outreach_capability(TOKEN)
    try:
        result = json.loads(handle_function_call(
            name, args, enabled_tools=[name], enabled_toolsets=["hermes-api-server"]))
        assert "error" in result
    finally:
        tool.clear_outreach_capability(bound)
    assert effects == []
    assert callback[0] == []


@pytest.mark.parametrize("name,args", FORBIDDEN)
def test_direct_registry_also_denies_before_entry_lookup(monkeypatch, name, args):
    looked_up = []
    real_get = registry.get_entry

    def get_entry(selected):
        looked_up.append(selected)
        return real_get(selected)

    monkeypatch.setattr(registry, "get_entry", get_entry)
    bound = tool.bind_outreach_capability(TOKEN)
    try:
        assert "error" in json.loads(registry.dispatch(name, args))
    finally:
        tool.clear_outreach_capability(bound)
    assert looked_up == []


def test_unbound_ordinary_dispatch_keeps_existing_path(monkeypatch):
    selected = []
    monkeypatch.setattr(registry, "dispatch", lambda name, args, **kwargs:
                        selected.append(name) or json.dumps({"synthetic": True}))
    assert not tool.outreach_capability_available()
    assert json.loads(handle_function_call("web_search", {"query": "synthetic"})) == {
        "synthetic": True}
    assert selected == ["web_search"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [None, [], ["web", "terminal", "atlas_email"]])
@pytest.mark.parametrize("requested,args", [("atlas_outreach_save", PAYLOAD)] + FORBIDDEN)
async def test_real_gateway_agent_and_dispatch(
        adapter, callback, monkeypatch, caplog, scope, requested, args):
    import run_agent
    from openai.types.chat import ChatCompletion
    from agent.model_metadata import MINIMUM_CONTEXT_LENGTH

    provider_calls, effects = [], []
    real_dispatch = registry.dispatch

    def dispatch(name, arguments, **kwargs):
        if name != "atlas_outreach_save":
            effects.append(name)
            return json.dumps({"error": "synthetic effect trap"})
        return real_dispatch(name, arguments, **kwargs)

    def complete(**kwargs):
        provider_calls.append(copy.deepcopy(kwargs))
        if len(provider_calls) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "synthetic-call", "type": "function", "function": {
                    "name": requested, "arguments": json.dumps(args)}}]}
            reason = "tool_calls"
        else:
            message, reason = {"role": "assistant", "content": "done"}, "stop"
        return ChatCompletion(id="synthetic", created=0, model="synthetic-model",
                              object="chat.completion", choices=[{
                                  "index": 0, "finish_reason": reason, "message": message}])

    class Client:
        def __init__(self, **kwargs):
            self._client = SimpleNamespace(is_closed=False)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=complete))
        def close(self):
            self._client.is_closed = True

    real_agent = run_agent.AIAgent

    class SyntheticAgent(real_agent):
        def __init__(self, **kwargs):
            super().__init__(**{**kwargs, "skip_memory": True,
                                "skip_context_files": True, "max_iterations": 4})
            self._disable_streaming = True

    monkeypatch.setattr(registry, "dispatch", dispatch)
    monkeypatch.setattr(run_agent, "OpenAI", Client)
    monkeypatch.setattr(run_agent, "AIAgent", SyntheticAgent)
    monkeypatch.setattr("agent.agent_init.fetch_model_metadata", lambda *a, **k: None)
    monkeypatch.setattr("agent.context_compressor.get_model_context_length",
                        lambda *a, **k: MINIMUM_CONTEXT_LENGTH * 2)
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {
        "api_key": "synthetic", "base_url": "http://example.invalid/v1",
        "provider": "openai", "api_mode": "chat_completions",
        "command": None, "args": []})
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "synthetic-model")
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {
        "platform_toolsets": {"api_server": ["web", "terminal", "atlas_email"]}})
    sid = adapter._session_db.create_session("synthetic-outreach", "api_server")
    body = {"message": "Save the synthetic fixture", "atlas_outreach_capability": TOKEN}
    if scope is not None:
        body["toolset_scope"] = scope
    async with TestClient(TestServer(app_for(adapter))) as client:
        response = await client.post(f"/api/sessions/{sid}/chat/stream", json=body)
        assert response.status == 200
        stream = await response.text()
    assert "event: run.completed" in stream
    assert len(provider_calls) >= 2
    assert all({v["function"]["name"] for v in call["tools"]} == {
        "atlas_outreach_save"} for call in provider_calls)
    assert effects == []
    observed, _ = callback
    if requested == "atlas_outreach_save":
        assert len(observed) == 1
        assert observed[0].headers["Authorization"] == "Bearer " + TOKEN
        assert json.loads(observed[0].content) == {
            "protocol": PROTOCOL, "operation": "save", "arguments": PAYLOAD}
        results = [m["content"] for m in provider_calls[-1]["messages"] if m["role"] == "tool"]
        assert any(json.loads(value) == SUCCESS for value in results)
    else:
        assert observed == []
    assert TOKEN not in stream + json.dumps(provider_calls) + caplog.text
    assert not tool.outreach_capability_available()


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"atlas_import_capability": "atimp_" + "x" * 48},
    {"atlas_ownership_capability": "atown_" + "x" * 48},
    {"atlas_query_capability": "atqry_" + "x" * 48},
    {"atlas_email_capability": "atmail_" + "x" * 48},
])
async def test_gateway_rejects_mixed_authority_before_execution(adapter, extra, caplog):
    sid = adapter._session_db.create_session("synthetic-mixed", "api_server")
    with patch.object(adapter, "_run_agent") as run:
        async with TestClient(TestServer(app_for(adapter))) as client:
            response = await client.post(f"/api/sessions/{sid}/chat/stream", json={
                "message": "synthetic", "atlas_outreach_capability": TOKEN, **extra})
            text = await response.text()
        assert response.status == 400
        run.assert_not_called()
    assert TOKEN not in text + caplog.text
    assert all(value not in text + caplog.text for value in extra.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "forged", 1, [], None])
async def test_gateway_rejects_malformed_present_authority(adapter, bad):
    sid = adapter._session_db.create_session("synthetic-malformed", "api_server")
    with patch.object(adapter, "_run_agent") as run:
        async with TestClient(TestServer(app_for(adapter))) as client:
            response = await client.post(f"/api/sessions/{sid}/chat/stream", json={
                "message": "synthetic", "atlas_outreach_capability": bad})
            assert response.status == 400
        run.assert_not_called()


@pytest.mark.asyncio
async def test_real_executor_binding_reset_and_exception_cleanup(adapter, callback):
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(pool)
    seen = []

    class Agent:
        def run_conversation(self, **kwargs):
            seen.append((names(["atlas_outreach"]), save()))
            if kwargs["user_message"] == "raise":
                raise RuntimeError("synthetic failure")
            return {"final_response": "done"}

    with patch.object(adapter, "_create_agent", return_value=Agent()):
        for token, message in [(TOKEN, "ok"), (None, "ok"), (OTHER, "raise"), (None, "ok")]:
            if message == "raise":
                with pytest.raises(RuntimeError, match="synthetic failure"):
                    await adapter._run_agent(message, [], atlas_outreach_capability=token)
            else:
                await adapter._run_agent(message, [], atlas_outreach_capability=token)
            assert not await loop.run_in_executor(pool, tool.outreach_capability_available)
    assert [r.headers["Authorization"] for r in callback[0]] == [
        "Bearer " + TOKEN, "Bearer " + OTHER]
    assert [result for _, result in seen] == [SUCCESS, ERROR, SUCCESS, ERROR]
    assert [visible for visible, _ in seen] == [
        {"atlas_outreach_save"}, set(), {"atlas_outreach_save"}, set()]


@pytest.mark.asyncio
async def test_concurrent_executor_users_keep_their_own_token(adapter, callback):
    import threading
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
    barrier = threading.Barrier(2)

    class Agent:
        def run_conversation(self, **kwargs):
            barrier.wait(timeout=5)
            assert save() == SUCCESS
            return {"final_response": "done"}

    with patch.object(adapter, "_create_agent", return_value=Agent()):
        await asyncio.gather(
            adapter._run_agent("a", [], atlas_outreach_capability=TOKEN),
            adapter._run_agent("b", [], atlas_outreach_capability=OTHER))
    assert sorted(r.headers["Authorization"] for r in callback[0]) == sorted([
        "Bearer " + TOKEN, "Bearer " + OTHER])


@pytest.mark.asyncio
async def test_cancelled_await_leaves_late_callback_denied_and_then_cleans(adapter, callback):
    import threading
    started, release, ended = threading.Event(), threading.Event(), threading.Event()
    observed, status = callback
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=2)
    loop.set_default_executor(pool)
    results = []

    class Agent:
        def run_conversation(self, **kwargs):
            started.set()
            try:
                assert release.wait(timeout=5)
                results.append(save())
                return {"final_response": "done"}
            finally:
                ended.set()

    with patch.object(adapter, "_create_agent", return_value=Agent()):
        task = asyncio.create_task(adapter._run_agent(
            "late", [], atlas_outreach_capability=TOKEN))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not ended.is_set()
            # Transport-level control only: actual durable Stop/archive/expiry
            # rejection is asserted by the unchanged Atlas HTTP fence.
            status[0] = 403
        finally:
            release.set()
            assert await asyncio.to_thread(ended.wait, 3)
    assert results == [ERROR]
    assert len(observed) == 1
    assert not await loop.run_in_executor(pool, tool.outreach_capability_available)
