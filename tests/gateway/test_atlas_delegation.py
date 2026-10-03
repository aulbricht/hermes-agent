"""Authenticated policy capability and fixed scope selection."""
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, _atlas_delegation_context

HEADERS = {"X-Atlas-Delegation-Policy": "view-as-v1", "X-Atlas-Delegated-Actor": "actor", "X-Atlas-Delegated-Subject": "subject", "X-Atlas-Delegated-Session": "session", "X-Atlas-Delegated-Resource-Type": "chat", "X-Atlas-Delegated-Resource-ID": "turn_fixture", "X-Atlas-Delegated-Receipt-Limit": "256"}


@pytest.mark.parametrize("headers,key,status", [(HEADERS, "", 401), ({**HEADERS, "X-Atlas-Delegation-Policy": "future"}, "configured", 400), ({k: v for k,v in HEADERS.items() if k != "X-Atlas-Delegated-Session"}, "configured", 400)])
def test_scope_header_cannot_fall_back_to_ordinary_agent(headers, key, status):
    context, error = _atlas_delegation_context(SimpleNamespace(_api_key=key), SimpleNamespace(headers=headers))
    assert context is None and error.status == status


def test_scope_identity_is_fixed_and_ordinary_requests_unchanged():
    context, error = _atlas_delegation_context(SimpleNamespace(_api_key="configured"), SimpleNamespace(headers=HEADERS))
    assert error is None
    assert context == {"actor_user_id": "actor", "subject_user_id": "subject", "view_as_session_id": "session", "resource_type": "chat", "resource_id": "turn_fixture", "receipt_limit": "256"}
    assert _atlas_delegation_context(SimpleNamespace(_api_key=""), SimpleNamespace(headers={})) == (None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization,status", [(None,401), ("Bearer wrong",401), ("Bearer fixture-only-key",200)])
async def test_capability_uses_existing_api_auth(authorization, status):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture-only-key"}))
    app = web.Application()
    app.router.add_get("/v1/atlas-delegation-policy", adapter._handle_atlas_delegation_policy)
    payload = {"policy_version": "view-as-v1", "executor_enforced": True, "allowed_tools": ["web_search"]}
    with patch("agent.atlas_delegation.capability", return_value=payload) as capability:
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/v1/atlas-delegation-policy", headers={"Authorization": authorization} if authorization else {})
            assert response.status == status
            if status == 200:
                assert await response.json() == payload
                assert response.headers["Cache-Control"] == "no-store"
                capability.assert_called_once()
            else:
                capability.assert_not_called()



@pytest.mark.asyncio
async def test_unavailable_readers_never_advertise_executor_capability():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture-only-key"}))
    app = web.Application()
    app.router.add_get("/v1/atlas-delegation-policy", adapter._handle_atlas_delegation_policy)
    with patch("agent.atlas_delegation.capability", side_effect=RuntimeError("disconnected")):
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/v1/atlas-delegation-policy", headers={"Authorization": "Bearer fixture-only-key"})
            assert response.status == 503
            assert "executor_enforced" not in await response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_session_routes_propagate_authenticated_scope_to_runner(tmp_path, stream):
    from unittest.mock import AsyncMock
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / "state.db")
    try:
        session_id = db.create_session("scope-chat", "api_server")
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture-only-key"}))
        adapter._session_db = db
        app = web.Application()
        path = "/api/sessions/{session_id}/chat" + ("/stream" if stream else "")
        app.router.add_post(path, adapter._handle_session_chat_stream if stream else adapter._handle_session_chat)
        runner = AsyncMock(return_value=({"final_response": "ok", "session_id": session_id}, {"total_tokens": 1}))
        with patch.object(adapter, "_run_agent", runner):
            async with TestClient(TestServer(app)) as client:
                response = await client.post(path.replace("{session_id}", session_id), json={"message": "read"}, headers={**HEADERS, "Authorization": "Bearer fixture-only-key"})
                assert response.status == 200
                await response.read()
        assert runner.await_args.kwargs["atlas_delegation_context"] == {"actor_user_id": "actor", "subject_user_id": "subject", "view_as_session_id": "session", "resource_type": "chat", "resource_id": "turn_fixture", "receipt_limit": "256"}
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses", "/v1/runs", "/future"])
@pytest.mark.parametrize("headers", [HEADERS, {"X-Atlas-Delegated-Session": "expired"}, {"X-Atlas-Delegation": "signed"}])
async def test_every_alternate_route_rejects_any_delegation_header(path, headers):
    from gateway.platforms.api_server import atlas_delegation_route_middleware
    from unittest.mock import AsyncMock
    called = AsyncMock(return_value=web.json_response({"ordinary": True}))
    app = web.Application(middlewares=[atlas_delegation_route_middleware])
    app.router.add_post(path, called)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(path, headers=headers)
        assert response.status == 400
        assert (await response.json())["error"]["code"] == "unsupported_atlas_delegation_route"
    called.assert_not_awaited()


@pytest.mark.parametrize("kind,identifier", [("chat", ""), ("chat", "qry_wrong"), ("query", "turn_wrong"), ("query_plan", "qry_wrong"), ("unknown", "turn_test")])
def test_native_resource_is_required_before_runner(kind, identifier):
    context, error = _atlas_delegation_context(SimpleNamespace(_api_key="configured"), SimpleNamespace(headers={**HEADERS, "X-Atlas-Delegated-Resource-Type": kind, "X-Atlas-Delegated-Resource-ID": identifier}))
    assert context is None and error.status == 400


@pytest.mark.asyncio
async def test_failed_native_runner_retains_completed_usage_and_actor_hash(monkeypatch):
    import hashlib
    import threading
    from agent import atlas_delegation as policy
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture-only-key"}))
    context, _ = _atlas_delegation_context(adapter, SimpleNamespace(headers=HEADERS))
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION,
        _atlas_delegation_identities=context, _atlas_paid_dispatch_lock=threading.Lock(),
        _atlas_primary_usage_calls=[{"generation_id": "primary", "model": "gpt-6-luna", "input_tokens": 10}],
        _atlas_auxiliary_usage_calls=[{"generation_id": "auxiliary", "model": "gpt-6-luna", "input_tokens": 5}], session_usage_calls=[])
    def failed(**kwargs):
        raise policy.DelegationDenied("expired after completed provider call")
    agent.run_conversation = failed
    monkeypatch.setattr(adapter, "_create_agent", lambda **kwargs: agent)
    result, usage = await adapter._run_agent(user_message="read", conversation_history=[], session_id="fixture", atlas_delegation_context=context, atlas_route_request_id="route_fixture", provider_user_hash="caller-supplied")
    assert result["failed"] is True and result["final_response"] == ""
    assert [call["generation_id"] for call in usage["calls"]] == ["primary", "auxiliary"]
    assert agent.request_overrides["user"] == "atlas-user-" + hashlib.sha256(b"actor").hexdigest()
    assert all(call["actor_user_id"] == "actor" and call["resource_id"] == "turn_fixture" and call["route_request_id"] == "route_fixture" for call in usage["calls"])


@pytest.mark.asyncio
async def test_uncertain_paid_attempt_withholds_answer_but_returns_durable_identity(monkeypatch):
    import threading
    from agent import atlas_delegation as policy
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture-only-key"}))
    context, _ = _atlas_delegation_context(adapter, SimpleNamespace(headers=HEADERS))
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION,
        _atlas_delegation_identities=context, _atlas_paid_dispatch_lock=threading.Lock(),
        _atlas_paid_attempts=[{"attempt_id": "attempt_fixture", "generation_id": "observed_failure", "dispatch_status": "uncertain", "usage_available": False}],
        session_usage_calls=[], run_conversation=lambda **kwargs: {"final_response": "must be withheld"})
    monkeypatch.setattr(adapter, "_create_agent", lambda **kwargs: agent)
    result, usage = await adapter._run_agent(user_message="read", conversation_history=[], session_id="fixture", atlas_delegation_context=context, atlas_route_request_id="route_fixture", provider_user_hash="caller-supplied")
    assert result["failed"] is True and result["final_response"] == ""
    row, = usage["calls"]
    assert row["attempt_id"] == "attempt_fixture" and row["generation_id"] == "observed_failure"
    assert row["actor_user_id"] == "actor" and row["resource_id"] == "turn_fixture"
    assert row["usage_available"] is False and "input_tokens" not in row and "cost_usd" not in row


@pytest.mark.parametrize("routed", [False, True])
def test_scoped_agent_runtime_never_enters_shared_auth_recovery(monkeypatch, routed):
    from types import SimpleNamespace
    from agent import atlas_delegation as policy
    monkeypatch.setenv("OPENROUTER_API_KEY", "process-only-fixture")
    monkeypatch.setattr(policy, "resolve_web_readers", lambda: {})
    monkeypatch.setattr(policy, "bind_policy", lambda *args: None)
    monkeypatch.setattr(policy, "validate_dispatch", lambda *args: None)
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    monkeypatch.setattr(adapter, "_ensure_session_db", lambda: None)
    config = {"model": {"provider": "openrouter", "default": "gpt-6.1-sol"}}
    with patch("gateway.run._load_gateway_config", return_value=config), patch("gateway.run._resolve_runtime_agent_kwargs") as ordinary, patch("gateway.run._resolve_runtime_agent_kwargs_for_provider") as routed_resolver, patch("agent.credential_pool._save_auth_store") as write, patch("run_agent.AIAgent", return_value=SimpleNamespace()) as constructor:
        adapter._create_agent(atlas_delegation_context={"actor_user_id": "actor"}, route={"provider": "openrouter", "model": "gpt-6.1-sol", "api_key": "config-key-must-not-override"} if routed else None)
    ordinary.assert_not_called()
    routed_resolver.assert_not_called()
    write.assert_not_called()
    assert constructor.call_args.kwargs["api_key"] == "process-only-fixture"
    assert constructor.call_args.kwargs["credential_pool"] is None
