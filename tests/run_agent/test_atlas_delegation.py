"""Runtime View As policy tests: dispatch authority, provenance and isolation."""
import base64
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from agent import atlas_delegation as policy
from tools.registry import registry
from tests.run_agent.test_tool_call_guardrail_runtime import _make_agent, _mock_tool_call


def scoped_agent(monkeypatch):
    agent = _make_agent("web_search", "terminal")
    schema = {"name": "web_search", "parameters": {"type": "object"}}
    handler = MagicMock(return_value=json.dumps({"ok": True}))
    entry = SimpleNamespace(handler=handler, schema=schema, max_result_size_chars=None, is_async=False)
    monkeypatch.setitem(registry._tools, "web_search", entry)
    agent._atlas_delegation_policy = policy.POLICY_VERSION
    agent._atlas_delegation_allowed_tools = policy.ALLOWED_TOOLS
    agent._atlas_delegation_entries = {"web_search": (handler, json.dumps(schema, sort_keys=True), False)}
    agent._atlas_delegation_fingerprints = {"reader": "protected"}
    agent._atlas_delegation_identities = {"actor_user_id": "actor", "subject_user_id": "subject", "view_as_session_id": "session", "resource_type": "chat", "resource_id": "turn_fixture", "receipt_limit": "256"}
    import threading
    agent._atlas_paid_dispatch_lock = threading.Lock()
    agent._atlas_paid_dispatch_count = 0
    agent._atlas_auxiliary_usage_calls = []
    agent._atlas_primary_usage_calls = []
    monkeypatch.setattr(policy, "_live_reader_fingerprints", lambda: {"reader": "protected"})
    return agent, handler


@pytest.mark.parametrize("concurrent", [False, True])
def test_both_executors_reject_fabricated_and_out_of_scope_tools(monkeypatch, concurrent):
    agent, handler = scoped_agent(monkeypatch)
    monkeypatch.setattr(policy, "validate_dispatch", lambda agent: None)
    msg = SimpleNamespace(content="", tool_calls=[_mock_tool_call(name) for name in ["terminal", "invented_reader", "web_search"]])
    messages = []
    with patch("run_agent.handle_function_call") as broad_dispatch, patch("hermes_cli.plugins.resolve_pre_tool_block") as plugin:
        executor = agent._execute_tool_calls_concurrent if concurrent else agent._execute_tool_calls_sequential
        executor(msg, messages, "task")
    assert len(messages) == 3
    assert all("unavailable" in m["content"] for m in messages[:2])
    assert json.loads(messages[2]["content"]) == {"ok": True}
    handler.assert_called_once()
    broad_dispatch.assert_not_called()
    plugin.assert_not_called()


@pytest.mark.parametrize("mutation", ["handler", "schema", "transport", "expired"])
def test_dispatch_fails_closed_after_hot_refresh_or_revocation(monkeypatch, mutation):
    agent, handler = scoped_agent(monkeypatch)
    monkeypatch.setattr(policy, "validate_dispatch", lambda agent: None)
    if mutation == "handler":
        monkeypatch.setitem(registry._tools, "web_search", SimpleNamespace(handler=MagicMock(), schema=registry._tools["web_search"].schema))
    elif mutation == "schema":
        registry._tools["web_search"].schema = {"name": "web_search", "parameters": {"type": "object", "dangerous": True}}
    elif mutation == "transport":
        monkeypatch.setattr(policy, "_live_reader_fingerprints", lambda: {"reader": "changed"})
    else:
        monkeypatch.setattr(policy, "validate_dispatch", MagicMock(side_effect=policy.DelegationDenied("expired")))
    assert "unavailable" in policy.dispatch_tool(agent, "web_search", {}, "task")
    handler.assert_not_called()


def test_normal_requests_retain_existing_dispatch_behavior():
    assert policy.tool_allowed(SimpleNamespace(), "terminal") is True
    policy.validate_dispatch(SimpleNamespace())


def test_scoped_mcp_refresh_cannot_expand_grants(monkeypatch):
    from tools import mcp_tool
    agent, _ = scoped_agent(monkeypatch)
    tools_before = agent.tools
    with patch("model_tools.get_tool_definitions") as refresh:
        mcp_tool.refresh_agent_mcp_tools(agent, enabled_override=["untrusted"])
    refresh.assert_not_called()
    assert agent.tools is tools_before


@pytest.mark.parametrize("payload,status", [({"allowed": True}, 200), ({"allowed": 1}, 200), ({"valid": True}, 200), ({"allowed": True}, 403)])
def test_fresh_signed_callback_strict_allow_response(monkeypatch, payload, status):
    agent, _ = scoped_agent(monkeypatch)
    key = b"fixture-only-credential-1234567890"
    monkeypatch.setattr(policy, "_read_dispatch_key", lambda: key)
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = status
    response.read.return_value = json.dumps(payload).encode()
    opener = MagicMock()
    opener.open.return_value = response
    with patch("urllib.request.build_opener", return_value=opener):
        if payload.get("allowed") is True and status == 200:
            policy.validate_dispatch(agent)
            policy.validate_dispatch(agent)
        else:
            with pytest.raises(policy.DelegationDenied):
                policy.validate_dispatch(agent)
    request = opener.open.call_args.args[0]
    encoded = request.get_header("X-atlas-delegation")
    envelope = json.loads(base64.urlsafe_b64decode(encoded))
    assert envelope["subject_user_id"] == "subject"
    assert envelope["method"] == "GET"
    assert envelope["body_sha256"] == hashlib.sha256(b"").hexdigest()
    assert request.get_header("X-atlas-delegation-signature") == hmac.new(key, encoded.encode(), hashlib.sha256).hexdigest()
    assert opener.open.call_args.kwargs == {"timeout": 3}
    if opener.open.call_count == 2:
        first = json.loads(base64.urlsafe_b64decode(opener.open.call_args_list[0].args[0].get_header("X-atlas-delegation")))
        assert first["nonce"] != envelope["nonce"]


@pytest.mark.parametrize("url", ["https://127.0.0.1:8243/api/v1/internal/view-as/validate", "http://evil:8243/api/v1/internal/view-as/validate", "http://127.0.0.1:8243/api/v1/internal/view-as/validate?override=1", "http://127.0.0.1:9999/api/v1/internal/view-as/validate"])
def test_callback_url_cannot_escape_loopback_validator(monkeypatch, url):
    agent, _ = scoped_agent(monkeypatch)
    monkeypatch.setattr(policy, "_read_dispatch_key", lambda: b"fixture-key")
    monkeypatch.setenv("ATLAS_VIEW_AS_AUTHORIZATION_URL", url)
    with patch("urllib.request.build_opener") as opener, pytest.raises(policy.DelegationDenied):
        policy.validate_dispatch(agent)
    opener.assert_not_called()


def test_auxiliary_context_expires_before_model_dispatch(monkeypatch):
    agent, _ = scoped_agent(monkeypatch)
    with patch("agent.atlas_auxiliary_accounting.atlas_auxiliary_enabled", return_value=True), patch.object(policy, "validate_dispatch", side_effect=policy.DelegationDenied("expired")) as check:
        with policy.dispatch_context(agent), pytest.raises(policy.DelegationDenied):
            policy.validate_auxiliary_dispatch()
        check.assert_called_once_with(agent)
        policy.validate_auxiliary_dispatch()  # context cleaned after failed turn
        assert check.call_count == 1


def test_scope_binding_removes_memory_and_freezes_only_approved_schemas(monkeypatch):
    agent = SimpleNamespace(context_compressor=SimpleNamespace(), tools=[{"unsafe": True}], _memory_store=object(), _memory_manager=object())
    schemas = [{"function": {"name": name}} for name in policy.ALLOWED_TOOLS]
    monkeypatch.setattr(policy, "resolve_policy", lambda: (schemas, {}, {}))
    policy.bind_policy(agent, {"subject_user_id": "subject"})
    assert agent.tools == schemas
    assert agent.valid_tool_names == policy.ALLOWED_TOOLS
    assert agent._memory_store is None and agent._memory_manager is None
    assert agent._memory_enabled is False and agent._skill_nudge_interval == 0
    with pytest.raises(TypeError):
        agent._atlas_delegation_entries["terminal"] = object()


def test_transport_attestation_rejects_writable_or_write_capable_server(monkeypatch):
    protected = MagicMock(side_effect=lambda path: path)
    monkeypatch.setattr(policy, "_protected", protected)
    config = {"enabled": True, "command": "/usr/bin/sudo", "args": ["-n", "-H", "-u", "acrefm", "--", "/usr/local/libexec/acre-filemaker-mcp"], "sampling": {"enabled": False}, "elicitation": {"enabled": False}, "tools": {"resources": False, "prompts": False}}
    assert policy._transport_fingerprint("acre-filemaker", config)
    for changed in [{**config, "args": ["node", "writable.js"]}, {**config, "env": {"NODE_OPTIONS": "--require=evil"}}, {**config, "sampling": {"enabled": True}}]:
        with pytest.raises(policy.DelegationDenied):
            policy._transport_fingerprint("acre-filemaker", changed)
    protected.side_effect = policy.DelegationDenied("unprotected")
    with pytest.raises(policy.DelegationDenied):
        policy._transport_fingerprint("acre-filemaker", config)


def test_capability_requires_real_live_reader_resolution(monkeypatch):
    monkeypatch.setattr(policy, "resolve_policy", MagicMock(side_effect=policy.DelegationDenied("disconnected")))
    with pytest.raises(policy.DelegationDenied):
        policy.capability()


@pytest.mark.parametrize("streaming", [False, True])
def test_expired_session_blocks_primary_model_before_sdk_call(monkeypatch, streaming):
    agent, _ = scoped_agent(monkeypatch)
    monkeypatch.setattr(policy, "validate_dispatch", MagicMock(side_effect=policy.DelegationDenied("expired")))
    with patch("agent.chat_completion_helpers.interruptible_api_call") as ordinary, patch("agent.chat_completion_helpers.interruptible_streaming_api_call") as stream:
        with pytest.raises(policy.DelegationDenied):
            (agent._interruptible_streaming_api_call if streaming else agent._interruptible_api_call)({})
        ordinary.assert_not_called()
        stream.assert_not_called()


def test_expired_session_blocks_compressor_summary():
    from tests.agent.test_context_compressor_summary_continuity import _compressor
    compressor = _compressor()
    compressor._atlas_dispatch_validator = MagicMock(side_effect=policy.DelegationDenied("expired"))
    with patch("agent.context_compressor.call_llm") as call:
        compressor._generate_summary([{"role": "user", "content": "history"}])
        call.assert_not_called()
    assert compressor._atlas_dispatch_validator.call_count >= 1


def test_expiry_between_auxiliary_resolution_and_send_blocks_real_sdk_call(monkeypatch):
    from agent import auxiliary_client, atlas_auxiliary_accounting as accounting
    agent, _ = scoped_agent(monkeypatch)
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_ENABLED", "true")
    client = MagicMock()
    client.base_url = "https://api.openai.com/v1"
    monkeypatch.setattr(auxiliary_client, "_get_cached_client", lambda *a, **kw: (client, accounting.LUNA))
    checks = MagicMock(side_effect=[None, policy.DelegationDenied("expired")])
    monkeypatch.setattr(policy, "validate_dispatch", checks)
    with policy.dispatch_context(agent), pytest.raises(policy.DelegationDenied):
        auxiliary_client.call_llm(task="compression", messages=[{"role": "user", "content": "history"}])
    assert checks.call_count == 2
    client.chat.completions.create.assert_not_called()


def test_live_resolution_filters_unknown_tools_and_accepts_actual_async_web_reader(monkeypatch):
    from tools import mcp_tool
    schemas = []
    for name in policy.ALLOWED_TOOLS:
        def reader(args, **kwargs):
            return {"ok": True}
        reader.__module__ = "tools.web_tools" if name.startswith("web_") else "tools.mcp_tool"
        schema = {"name": name, "parameters": {"type": "object"}}
        entry = SimpleNamespace(handler=reader, schema=schema, is_async=name == "web_extract")
        monkeypatch.setitem(registry._tools, name, entry)
        if name.startswith("mcp__"):
            monkeypatch.setitem(mcp_tool._mcp_tool_server_names, name, "acre_filemaker" if "acre_filemaker" in name else "atlas_vault")
        schemas.append({"type": "function", "function": schema})
    schemas.append({"function": {"name": "terminal"}})
    monkeypatch.setattr(policy, "_live_reader_fingerprints", lambda: {"reader": "protected"})
    with patch("model_tools.get_tool_definitions", return_value=schemas) as dynamic, patch("tools.registry.registry.get_definitions") as inventory:
        resolved, entries, fingerprints = policy.resolve_policy()
    dynamic.assert_not_called()
    inventory.assert_not_called()
    assert {s["function"]["name"] for s in resolved} == policy.ALLOWED_TOOLS
    assert entries["web_extract"][2] is True
    assert "terminal" not in entries
    monkeypatch.setitem(mcp_tool._mcp_tool_server_names, "mcp__atlas_vault__vault_read", "untrusted")
    with patch("model_tools.get_tool_definitions", return_value=schemas), pytest.raises(policy.DelegationDenied):
        policy.resolve_policy()


def test_frozen_async_reader_dispatches_through_existing_bridge(monkeypatch):
    agent, _ = scoped_agent(monkeypatch)
    async def reader(args, **kwargs):
        return json.dumps({"ok": args["value"]})
    schema = {"name": "web_extract"}
    monkeypatch.setitem(registry._tools, "web_extract", SimpleNamespace(handler=reader, schema=schema, is_async=True))
    agent._atlas_delegation_entries["web_extract"] = (reader, json.dumps(schema, sort_keys=True), True)
    monkeypatch.setattr(policy, "validate_dispatch", lambda agent: None)
    assert json.loads(policy.dispatch_tool(agent, "web_extract", {"value": "read"}, "task")) == {"ok": "read"}


def test_codex_connection_retry_rechecks_expiry_before_second_billable_request(monkeypatch):
    import httpx
    from agent.codex_runtime import run_codex_stream
    agent, _ = scoped_agent(monkeypatch)
    client = MagicMock()
    client.responses.create.side_effect = httpx.ConnectError("fixture connection reset")
    checks = MagicMock(side_effect=[None, policy.DelegationDenied("expired")])
    monkeypatch.setattr(policy, "validate_dispatch", checks)
    monkeypatch.delenv("ATLAS_SOL_ACCOUNTING_SOCKET", raising=False)
    with pytest.raises(policy.DelegationDenied):
        run_codex_stream(agent, {"model": "fixture"}, client=client)
    assert checks.call_count == 2
    client.responses.create.assert_called_once()


def test_callback_resource_is_signed_and_matches_headers(monkeypatch):
    agent, _ = scoped_agent(monkeypatch)
    monkeypatch.setattr(policy, "_read_dispatch_key", lambda: b"fixture-key")
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = 200
    response.read.return_value = b'{"allowed":true}'
    with patch("urllib.request.build_opener") as opener:
        opener.return_value.open.return_value = response
        policy.validate_dispatch(agent)
        request = opener.return_value.open.call_args.args[0]
        envelope = json.loads(base64.urlsafe_b64decode(request.get_header("X-atlas-delegation")))
        assert envelope["resource_type"] == request.get_header("X-atlas-resource-type") == "chat"
        assert envelope["resource_id"] == request.get_header("X-atlas-resource-id") == "turn_fixture"
        agent._atlas_delegation_identities["resource_id"] = ""
        with pytest.raises(policy.DelegationDenied):
            policy.validate_dispatch(agent)
        assert opener.return_value.open.call_count == 1


def test_paid_calls_share_finite_capacity_and_denial_never_reserves(monkeypatch):
    agent, _ = scoped_agent(monkeypatch)
    monkeypatch.setattr(policy, "validate_dispatch", lambda agent: None)
    for _ in range(256):
        policy.admit_paid_dispatch(agent)
    with pytest.raises(policy.DelegationDenied, match="exhausted"):
        policy.admit_paid_dispatch(agent)
    assert agent._atlas_paid_dispatch_count == 256
    assert len(agent._atlas_paid_attempts) == 256
    assert len({row["attempt_id"] for row in agent._atlas_paid_attempts}) == 256
    policy.admit_paid_dispatch(SimpleNamespace())


@pytest.mark.parametrize("observed", [False, True])
def test_failed_paid_attempt_is_retained_and_blocks_another_send(monkeypatch, observed):
    import httpx
    from agent.codex_runtime import run_codex_stream
    agent, _ = scoped_agent(monkeypatch)
    monkeypatch.setattr(policy, "validate_dispatch", lambda agent: None)
    monkeypatch.delenv("ATLAS_SOL_ACCOUNTING_SOCKET", raising=False)
    def events():
        yield SimpleNamespace(type="response.created", response=SimpleNamespace(id="gen-failed", model="fixture"))
        yield SimpleNamespace(type="response.output_text.delta", delta="unsettled answer")
        raise httpx.ReadTimeout("fixture failed after provider admission")
    client = MagicMock()
    if observed:
        client.responses.create.return_value = events()
    else:
        client.responses.create.side_effect = httpx.ConnectError("uncertain provider admission")
    with pytest.raises(policy.DelegationDenied, match="unsettled"):
        run_codex_stream(agent, {"model": "fixture"}, client=client)
    client.responses.create.assert_called_once()
    with pytest.raises(policy.DelegationDenied, match="unsettled"):
        policy.admit_paid_dispatch(agent)
    assert agent._atlas_paid_dispatch_count == 1
    row, = policy.terminal_usage_calls(agent)
    assert row["generation_id"] == ("gen-failed" if observed else "")
    assert row["dispatch_status"] == "uncertain" and row["usage_available"] is False
    assert row["attempt_id"].startswith("attempt_") and row["actor_user_id"] == "actor"
    assert "cost_usd" not in row and "input_tokens" not in row and "output_tokens" not in row
    assert agent._atlas_paid_dispatch_count == 1
    with pytest.raises(policy.DelegationDenied, match="closed"):
        policy.admit_paid_dispatch(agent)
    assert agent._atlas_paid_dispatch_count == 1
    assert len(policy.terminal_usage_calls(agent)) == 1


def test_completed_attempts_keep_unique_slots_and_unknown_usage_never_becomes_zero(monkeypatch):
    agent, _ = scoped_agent(monkeypatch)
    monkeypatch.setattr(policy, "validate_dispatch", lambda agent: None)
    first = policy.admit_paid_dispatch(agent, model="fixture")
    policy.collect_primary_response(agent, SimpleNamespace(id="known", usage=SimpleNamespace(input_tokens=2, output_tokens=1)), "fixture", first)
    second = policy.admit_paid_dispatch(agent, model="fixture", auxiliary=True)
    policy.finish_paid_attempt(agent, second, {"generation_id": "unknown", "usage_available": False, "input_tokens": 0, "cost_usd": 0})
    rows = policy.terminal_usage_calls(agent)
    assert len(rows) == 2 and len({r["attempt_id"] for r in rows}) == 2
    assert rows[0]["dispatch_status"] == "completed" and rows[0]["input_tokens"] == 2
    assert rows[1]["dispatch_status"] == "uncertain" and rows[1]["generation_id"] == "unknown"
    assert "input_tokens" not in rows[1] and "cost_usd" not in rows[1]


def test_delegated_error_hook_never_even_looks_up_plugins(monkeypatch):
    agent, _ = scoped_agent(monkeypatch)
    with patch("hermes_cli.plugins.has_hook") as lookup, patch("hermes_cli.plugins.invoke_hook") as invoke:
        agent._invoke_api_request_error_hook(task_id="task", turn_id="turn", api_request_id="api", api_call_count=1, api_start_time=0, api_kwargs={}, error_type="Timeout", error_message="failed")
    lookup.assert_not_called()
    invoke.assert_not_called()



def test_terminal_closure_rejects_authorization_waiter_without_new_receipt(monkeypatch):
    import threading
    agent, _ = scoped_agent(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    sent, errors = [], []
    def authorize(agent):
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(policy, "validate_dispatch", authorize)
    def worker():
        try:
            policy.admit_paid_dispatch(agent)
            sent.append(True)
        except policy.DelegationDenied as error:
            errors.append(str(error))
    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(5)
    assert policy.terminal_usage_calls(agent) == []
    release.set()
    thread.join(5)
    assert not thread.is_alive() and not sent and errors == ["Atlas delegation dispatch is closed"]
    assert policy.terminal_usage_calls(agent) == [] and agent._atlas_paid_dispatch_count == 0


def test_terminal_drain_captures_finishing_attempt_without_sleep(monkeypatch):
    import threading
    agent, _ = scoped_agent(monkeypatch)
    monkeypatch.setattr(policy, "validate_dispatch", lambda agent: None)
    attempt = policy.admit_paid_dispatch(agent)
    condition = agent._atlas_paid_condition = threading.Condition(agent._atlas_paid_dispatch_lock)
    waiting = threading.Event()
    real_wait = condition.wait
    def wait(timeout=None):
        waiting.set()
        return real_wait(timeout)
    monkeypatch.setattr(condition, "wait", wait)
    def finish():
        assert waiting.wait(5)
        policy.finish_paid_attempt(agent, attempt, {"generation_id": "late-known", "usage_available": True, "input_tokens": 3, "output_tokens": 2})
    worker = threading.Thread(target=finish)
    worker.start()
    row, = policy.terminal_usage_calls(agent)
    worker.join(5)
    assert not worker.is_alive() and row["generation_id"] == "late-known" and row["dispatch_status"] == "completed"
    assert policy.terminal_usage_calls(agent) == [row]
    with pytest.raises(policy.DelegationDenied, match="closed"):
        policy.admit_paid_dispatch(agent)


def test_terminal_drain_timeout_keeps_active_uncertain_attempt(monkeypatch):
    agent, _ = scoped_agent(monkeypatch)
    monkeypatch.setattr(policy, "validate_dispatch", lambda agent: None)
    attempt = policy.admit_paid_dispatch(agent)
    policy.observe_paid_event(SimpleNamespace(response=SimpleNamespace(id="observed-pending")), attempt, agent)
    ticks = iter([0.0, 4.0])
    monkeypatch.setattr(policy.time, "monotonic", lambda: next(ticks))
    row, = policy.terminal_usage_calls(agent)
    assert row["generation_id"] == "observed-pending" and row["dispatch_status"] == "uncertain"
    assert row["usage_available"] is False and "input_tokens" not in row
    assert attempt["attempt_id"] in agent._atlas_paid_active


def test_callback_rechecks_closure_after_authorization(monkeypatch):
    agent, _ = scoped_agent(monkeypatch)
    monkeypatch.setattr(policy, "_read_dispatch_key", lambda: b"fixture-key")
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = 200
    def read(size):
        agent._atlas_admission_closed = True
        return b'{"allowed":true}'
    response.read.side_effect = read
    with patch("urllib.request.build_opener") as opener:
        opener.return_value.open.return_value = response
        with pytest.raises(policy.DelegationDenied):
            policy.validate_dispatch(agent)


@pytest.mark.parametrize("entrypoint", ["capability", "resolve_policy", "bind_policy", "agent_constructor", "gateway_constructor"])
def test_ungoverned_provider_rejected_before_real_inventory_and_initialization(monkeypatch, entrypoint):
    from tools import web_tools
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "xai"})
    with patch("tools.registry.registry.get_definitions") as inventory, patch("model_tools.get_tool_definitions") as dynamic, patch("run_agent.OpenAI") as sdk, patch("tools.xai_http.has_xai_credentials") as credentials, patch("gateway.run._resolve_runtime_agent_kwargs") as model_credentials:
        with pytest.raises(policy.DelegationDenied, match="not governed"):
            if entrypoint == "agent_constructor":
                from run_agent import AIAgent
                AIAgent(atlas_delegation_policy=policy.POLICY_VERSION, api_key="fixture", quiet_mode=True)
            elif entrypoint == "gateway_constructor":
                from gateway.platforms.api_server import APIServerAdapter
                from gateway.config import PlatformConfig
                APIServerAdapter(PlatformConfig(enabled=True))._create_agent(atlas_delegation_context={"actor_user_id": "actor"})
            elif entrypoint == "bind_policy":
                policy.bind_policy(SimpleNamespace(), {})
            else:
                getattr(policy, entrypoint)()
    inventory.assert_not_called()
    dynamic.assert_not_called()
    sdk.assert_not_called()
    credentials.assert_not_called()
    model_credentials.assert_not_called()
