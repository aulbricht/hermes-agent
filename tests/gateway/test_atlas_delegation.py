"""Authenticated policy capability and fixed scope selection."""
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, _atlas_delegation_context

HEADERS = {"X-Atlas-Delegation-Policy": "view-as-v1", "X-Atlas-Delegated-Actor": "actor", "X-Atlas-Delegated-Subject": "subject", "X-Atlas-Delegated-Session": "session"}


@pytest.mark.parametrize("headers,key,status", [(HEADERS, "", 401), ({**HEADERS, "X-Atlas-Delegation-Policy": "future"}, "configured", 400), ({k: v for k,v in HEADERS.items() if k != "X-Atlas-Delegated-Session"}, "configured", 400)])
def test_scope_header_cannot_fall_back_to_ordinary_agent(headers, key, status):
    context, error = _atlas_delegation_context(SimpleNamespace(_api_key=key), SimpleNamespace(headers=headers))
    assert context is None and error.status == status


def test_scope_identity_is_fixed_and_ordinary_requests_unchanged():
    context, error = _atlas_delegation_context(SimpleNamespace(_api_key="configured"), SimpleNamespace(headers=HEADERS))
    assert error is None
    assert context == {"actor_user_id": "actor", "subject_user_id": "subject", "view_as_session_id": "session"}
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
        assert runner.await_args.kwargs["atlas_delegation_context"] == {"actor_user_id": "actor", "subject_user_id": "subject", "view_as_session_id": "session"}
    finally:
        db.close()
