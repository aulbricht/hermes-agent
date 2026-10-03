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


@pytest.mark.parametrize('routed', [False, True])
def test_denied_scoped_gateway_validates_before_any_constructor_or_config(monkeypatch, routed):
    from agent import atlas_delegation as policy
    monkeypatch.setattr(policy, 'resolve_web_readers', lambda: {})
    def deny(agent):
        assert policy.credential_scope_active()
        raise policy.DelegationDenied('revoked')
    monkeypatch.setattr(policy, 'validate_dispatch', deny)
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    with patch('run_agent.AIAgent') as constructor, patch('gateway.run._load_gateway_config') as config, patch('gateway.run._resolve_runtime_agent_kwargs') as credentials, patch('tools.env_probe.warm_environment_probe_async') as probe:
        with pytest.raises(policy.DelegationDenied, match='revoked'):
            adapter._create_agent(atlas_delegation_context={'actor_user_id': 'actor'}, route={'provider': 'openrouter', 'model': 'gpt-6-luna'} if routed else None)
        constructor.assert_not_called()
        config.assert_not_called()
        credentials.assert_not_called()
        probe.assert_not_called()
    assert not policy.credential_scope_active()


@pytest.mark.parametrize('routed', [False, True])
def test_gateway_keeps_credential_boundary_across_actual_constructor_entry(monkeypatch, routed):
    from agent import atlas_delegation as policy
    from agent.credential_pool import load_pool
    monkeypatch.setenv('OPENROUTER_API_KEY', 'synthetic-process-key')
    monkeypatch.setattr(policy, 'resolve_web_readers', lambda: {})
    monkeypatch.setattr(policy, 'validate_dispatch', lambda agent: None)
    monkeypatch.setattr(policy, 'bind_policy', lambda *args: None)
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    monkeypatch.setattr(adapter, '_ensure_session_db', lambda: None)
    config = {'model': {'provider': 'openrouter', 'default': 'openai/gpt-6-luna'}}
    def construct(**kwargs):
        assert policy.credential_scope_active()
        with pytest.raises(policy.DelegationDenied, match='shared credential'):
            load_pool('openrouter')
        assert kwargs['api_key'] == 'synthetic-process-key'
        return SimpleNamespace()
    with patch('gateway.run._load_gateway_config', return_value=config), patch('run_agent.AIAgent', side_effect=construct), patch('hermes_cli.auth._save_auth_store') as write:
        adapter._create_agent(atlas_delegation_context={'actor_user_id': 'actor'}, route={'provider': 'openrouter', 'model': 'openai/gpt-6-luna'} if routed else None)
        write.assert_not_called()
    assert not policy.credential_scope_active()


def _title_control_headers(policy, native_id, raw, *, nonce=None, updates=None):
    import base64, hashlib, hmac, json, secrets, time
    from urllib.parse import quote
    envelope = {'actor_user_id': 'actor', 'subject_user_id': 'subject', 'view_as_session_id': 'session',
        'method': 'PATCH', 'path': '/api/sessions/' + quote(native_id, safe=''),
        'body_sha256': hashlib.sha256(raw).hexdigest(), 'issued_at': int(time.time()),
        'nonce': nonce or secrets.token_hex(16), 'resource_type': 'chat_session', 'resource_id': native_id}
    envelope.update(updates or {})
    encoded = base64.urlsafe_b64encode(json.dumps(envelope, sort_keys=True, separators=(',', ':')).encode()).decode()
    return {'Authorization': 'Bearer fixture-only-key', 'X-Atlas-Delegation-Policy': policy.CONTROL_POLICY_VERSION,
        'X-Atlas-User-Key': 'subject', 'X-Atlas-Resource-Type': 'chat_session', 'X-Atlas-Resource-Id': native_id,
        'X-Atlas-Delegation': encoded, 'X-Atlas-Delegation-Signature': hmac.new(b'synthetic-control-key', encoded.encode(), hashlib.sha256).hexdigest()}


@pytest.mark.asyncio
@pytest.mark.parametrize('revoked', [False, True])
async def test_actual_title_patch_rechecks_after_body_read_before_write(monkeypatch, tmp_path, revoked):
    import json
    from agent import atlas_delegation as policy
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / 'title-control.db')
    native_id = 'native_title_fixture'
    db.create_session(native_id, 'api_server')
    db.set_session_title(native_id, 'before')
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={'key': 'fixture-only-key'}))
    adapter._session_db = db
    read_complete = [False]
    original = adapter._read_json_body
    async def read_body(request):
        result = await original(request)
        read_complete[0] = True
        return result
    monkeypatch.setattr(adapter, '_read_json_body', read_body)
    monkeypatch.setattr(policy, '_read_dispatch_key', lambda: b'synthetic-control-key')
    callbacks = []
    def live(context):
        assert read_complete[0]
        assert context['resource_type'] == 'chat_session'
        assert context['resource_id'] == native_id and context['subject_user_id'] == 'subject'
        callbacks.append(context)
        if revoked:
            raise policy.DelegationDenied('revoked while body was parsed')
    monkeypatch.setattr(policy, '_request_authorization', live)
    raw = json.dumps({'title': 'Résumé'}, ensure_ascii=False, separators=(',', ':')).encode()
    app = web.Application()
    app.router.add_patch('/api/sessions/{session_id}', adapter._handle_patch_session)
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.patch('/api/sessions/' + native_id, data=raw,
                headers=_title_control_headers(policy, native_id, raw))
            assert response.status == (403 if revoked else 200)
        assert len(callbacks) == 1
        assert db.get_session(native_id)['title'] == ('before' if revoked else 'Résumé')
    finally:
        db.close()


@pytest.mark.parametrize('mismatch', ['body', 'path', 'subject', 'resource', 'expired', 'future', 'method'])
def test_nonpaid_control_signature_binds_actual_write_and_identity(monkeypatch, mismatch):
    import time
    from agent import atlas_delegation as policy
    monkeypatch.setattr(policy, '_read_dispatch_key', lambda: b'synthetic-control-key')
    raw = b'{"title":"after"}'
    updates = {'path': '/api/sessions/other'} if mismatch == 'path' else {'subject_user_id': 'other'} if mismatch == 'subject' else {'resource_id': 'other'} if mismatch == 'resource' else {'issued_at': int(time.time()) - 61} if mismatch == 'expired' else {'issued_at': int(time.time()) + 6} if mismatch == 'future' else {'method': 'DELETE'} if mismatch == 'method' else {}
    headers = _title_control_headers(policy, 'native_fixture', raw, updates=updates)
    with pytest.raises(policy.DelegationDenied):
        policy.verify_session_title_control(headers, method='PATCH', path='/api/sessions/native_fixture',
            raw_body=raw + b' ' if mismatch == 'body' else raw, native_id='native_fixture', body={'title': 'after'})


def test_control_replay_is_rejected_and_paid_resource_validation_stays_separate(monkeypatch):
    from agent import atlas_delegation as policy
    monkeypatch.setattr(policy, '_read_dispatch_key', lambda: b'synthetic-control-key')
    raw = b'{"title":"after"}'
    headers = _title_control_headers(policy, 'native_fixture', raw)
    kwargs = dict(method='PATCH', path='/api/sessions/native_fixture', raw_body=raw,
                  native_id='native_fixture', body={'title': 'after'})
    context = policy.verify_session_title_control(headers, **kwargs)
    assert context['resource_type'] == 'chat_session'
    assert not policy.valid_resource('chat_session', 'native_fixture')
    with pytest.raises(policy.DelegationDenied, match='Replayed'):
        policy.verify_session_title_control(headers, **kwargs)
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION,
                            _atlas_delegation_identities=context)
    import threading
    agent._atlas_paid_dispatch_lock = threading.Lock()
    with pytest.raises(policy.DelegationDenied, match='scoped resource'):
        policy.validate_dispatch(agent)
    for method, path in [('DELETE', '/api/sessions/native_fixture'), ('POST', '/api/sessions/native_fixture/chat'), ('POST', '/api/sessions/native_fixture/chat/stream')]:
        from gateway.platforms.api_server import _atlas_delegation_route_error
        assert _atlas_delegation_route_error(SimpleNamespace(method=method, path=path, headers=headers)).status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [{'title': 'after', 'end_reason': 'user'}, {'end_reason': 'user'}, {'title': 'after', 'archived': True}, {'title': {'unsafe': True}}])
async def test_actual_control_patch_rejects_other_session_mutations(monkeypatch, tmp_path, body):
    import json
    from agent import atlas_delegation as policy
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / 'forbidden-title-control.db')
    db.create_session('native_fixture', 'api_server')
    db.set_session_title('native_fixture', 'before')
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={'key': 'fixture-only-key'}))
    adapter._session_db = db
    monkeypatch.setattr(policy, '_read_dispatch_key', lambda: b'synthetic-control-key')
    raw = json.dumps(body).encode()
    app = web.Application()
    app.router.add_patch('/api/sessions/{session_id}', adapter._handle_patch_session)
    try:
        with patch.object(policy, '_request_authorization') as live:
            async with TestClient(TestServer(app)) as client:
                response = await client.patch('/api/sessions/native_fixture', data=raw,
                    headers=_title_control_headers(policy, 'native_fixture', raw))
                assert response.status in {400, 403}
            live.assert_not_called()
        assert db.get_session('native_fixture')['title'] == 'before'
        assert not db.get_session('native_fixture')['end_reason']
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('configured_key,authorization', [('', None), ('fixture-only-key', 'Bearer wrong')])
async def test_title_control_always_requires_native_api_auth(monkeypatch, tmp_path, configured_key, authorization):
    from agent import atlas_delegation as policy
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / 'auth-title-control.db')
    db.create_session('native_fixture', 'api_server')
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={'key': configured_key}))
    adapter._session_db = db
    adapter._api_key = configured_key
    raw = b'{"title":"after"}'
    headers = _title_control_headers(policy, 'native_fixture', raw)
    if authorization is None:
        headers.pop('Authorization')
    else:
        headers['Authorization'] = authorization
    app = web.Application()
    app.router.add_patch('/api/sessions/{session_id}', adapter._handle_patch_session)
    try:
        with patch.object(policy, '_request_authorization') as live:
            async with TestClient(TestServer(app)) as client:
                response = await client.patch('/api/sessions/native_fixture', data=raw, headers=headers)
                assert response.status == 401
            live.assert_not_called()
        assert not db.get_session('native_fixture')['title']
    finally:
        db.close()


def test_nonpaid_live_callback_signs_exact_resource_and_subject(monkeypatch):
    import base64, json
    from agent import atlas_delegation as policy
    from unittest.mock import MagicMock
    context = {'actor_user_id': 'actor', 'subject_user_id': 'subject', 'view_as_session_id': 'session', 'resource_type': 'chat_session', 'resource_id': 'native_fixture'}
    monkeypatch.setattr(policy, '_read_dispatch_key', lambda: b'synthetic-control-key')
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = 200
    response.read.return_value = b'{"allowed":true}'
    with patch('urllib.request.build_opener') as opener:
        opener.return_value.open.return_value = response
        policy.validate_session_title_control(context)
        request = opener.return_value.open.call_args.args[0]
        envelope = json.loads(base64.urlsafe_b64decode(request.get_header('X-atlas-delegation')))
        assert envelope['method'] == 'GET'
        assert envelope['resource_type'] == request.get_header('X-atlas-resource-type') == 'chat_session'
        assert envelope['resource_id'] == request.get_header('X-atlas-resource-id') == 'native_fixture'
        assert envelope['subject_user_id'] == request.get_header('X-atlas-user-key') == 'subject'
        assert opener.return_value.open.call_args.kwargs['timeout'] == 3
