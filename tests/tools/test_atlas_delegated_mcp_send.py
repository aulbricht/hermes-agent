"""Exercise actual MCP coroutine send after lock acquisition and recovery."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agent import atlas_delegation as policy
from tools import mcp_tool as mcp


@pytest.mark.parametrize("change", ["expiry", "transport", "replacement", "reconnect"])
def test_actual_rpc_revalidates_after_lock_and_reconnect(monkeypatch, change):
    monkeypatch.setitem(mcp._server_error_counts, "fixture", 0)
    authorized = [True]
    fingerprint = ["frozen"]
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION,
                            _atlas_delegation_fingerprints={"reader": "frozen"})
    calls = AsyncMock(return_value=SimpleNamespace(content=[SimpleNamespace(text="ok")], isError=False))
    server = mcp.MCPServerTask("fixture")
    server.session = SimpleNamespace(call_tool=calls)
    monkeypatch.setitem(mcp._servers, "fixture", server)
    monkeypatch.setattr(policy, "_live_reader_fingerprints", lambda: {"reader": fingerprint[0]})
    def validate(agent):
        if not authorized[0]:
            raise policy.DelegationDenied("expired")
    monkeypatch.setattr(policy, "validate_dispatch", validate)
    def mutate():
        if change in {"expiry", "reconnect"}:
            authorized[0] = False
        elif change == "transport":
            fingerprint[0] = "changed"
        else:
            mcp._servers["fixture"] = SimpleNamespace(session=server.session)
    class WaitingLock:
        async def __aenter__(self):
            mutate()
        async def __aexit__(self, *args):
            return False
    if change != "reconnect":
        server._rpc_lock = WaitingLock()
    else:
        server._rpc_lock = asyncio.Lock()
        calls.side_effect = RuntimeError("session expired")
    monkeypatch.setattr(mcp, "_run_on_mcp_loop", lambda factory, **kwargs: asyncio.run(factory()))
    monkeypatch.setattr(mcp, "_handle_auth_error_and_retry", lambda *args: None)
    def retry(name, error, retry_call, description):
        if change == "reconnect":
            mutate()
            return retry_call()
        return None
    monkeypatch.setattr(mcp, "_handle_session_expired_and_retry", retry)
    handler = mcp._make_tool_handler("fixture", "reader", 1)
    with policy.dispatch_context(agent):
        if change == "reconnect":
            with pytest.raises(policy.DelegationDenied):
                handler({})
        else:
            assert "error" in json.loads(handler({}))
    assert calls.await_count == (1 if change == "reconnect" else 0)
