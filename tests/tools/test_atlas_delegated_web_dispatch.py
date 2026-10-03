"""Fresh authorization belongs at actual sends, after async checks/queues."""
import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from agent import atlas_delegation as policy


@pytest.mark.parametrize("queued", [False, True])
def test_extract_revoked_during_safety_or_worker_queue_never_sends(monkeypatch, queued):
    from tools import web_tools
    from agent import web_search_registry
    authorized = [True]
    monkeypatch.setattr(policy, "tool_allowed", lambda agent, name: authorized[0])
    async def safe(url):
        if not queued:
            authorized[0] = False
        return True
    monkeypatch.setattr(web_tools, "async_is_safe_url", safe)
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: "fixture")
    provider = SimpleNamespace(name="fixture", supports_extract=lambda: True, extract=MagicMock(return_value=[]))
    monkeypatch.setattr(web_search_registry, "get_provider", lambda name: provider)
    if queued:
        async def worker(func, *args, **kwargs):
            authorized[0] = False
            return func(*args, **kwargs)
        monkeypatch.setattr(asyncio, "to_thread", worker)
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION, _atlas_paid_dispatch_lock=threading.Lock(), _atlas_web_readers={"web_extract": (provider, provider.extract)})
    with policy.dispatch_context(agent):
        result = json.loads(asyncio.run(web_tools.web_extract_tool(["https://example.com/"])))
    provider.extract.assert_not_called()
    assert "authorized" in result["error"]


def test_firecrawl_checks_each_url_after_queue_and_prevents_second_send(monkeypatch):
    from plugins.web.firecrawl import provider as firecrawl
    authorized = [True]
    monkeypatch.setattr(policy, "tool_allowed", lambda agent, name: authorized[0])
    monkeypatch.setattr(firecrawl, "check_website_access", lambda url: None)
    monkeypatch.setattr(firecrawl, "is_safe_url", lambda url: True)
    def scrape(**kwargs):
        authorized[0] = False
        return {"markdown": "first page", "metadata": {}}
    client = SimpleNamespace(scrape=MagicMock(side_effect=scrape))
    monkeypatch.setattr(firecrawl, "_get_firecrawl_client", lambda: client)
    with policy.dispatch_context(SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION, _atlas_paid_dispatch_lock=threading.Lock())):
        result = asyncio.run(firecrawl.FirecrawlWebSearchProvider().extract(["https://example.com/one", "https://example.com/two"]))
    client.scrape.assert_called_once()
    assert "authorized" in result[1]["error"]


def test_parallel_scoped_sdk_disables_hidden_retries_and_rechecks_after_loading(monkeypatch):
    from plugins.web.parallel import provider as parallel
    authorized = [True]
    monkeypatch.setattr(policy, "tool_allowed", lambda agent, name: authorized[0])
    async def send(**kwargs):
        pytest.fail("revoked request sent")
    client = SimpleNamespace(beta=SimpleNamespace(extract=send))
    def options(**kwargs):
        assert kwargs == {"max_retries": 0}
        authorized[0] = False
        return client
    client.with_options = MagicMock(side_effect=options)
    monkeypatch.setattr(parallel, "_get_async_client", lambda: client)
    with policy.dispatch_context(SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION, _atlas_paid_dispatch_lock=threading.Lock())):
        rows = asyncio.run(parallel.ParallelWebSearchProvider().extract(["https://example.com/"]))
    client.with_options.assert_called_once()
    assert "authorized" in rows[0]["error"]


def test_scoped_firecrawl_client_has_no_hidden_sdk_retries_or_shared_cache(monkeypatch):
    from plugins.web.firecrawl import provider as firecrawl
    from tools import web_tools
    monkeypatch.setattr(firecrawl, "_get_direct_firecrawl_config", lambda: ({"api_key": "fixture"}, ("fixture",)))
    monkeypatch.setattr(web_tools, "prefers_gateway", lambda name: False)
    cached = object()
    monkeypatch.setattr(web_tools, "_firecrawl_client", cached)
    constructor = MagicMock()
    monkeypatch.setattr(web_tools, "Firecrawl", constructor)
    with policy.dispatch_context(SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION, _atlas_paid_dispatch_lock=threading.Lock())):
        firecrawl._get_firecrawl_client()
    constructor.assert_called_once_with(api_key="fixture", max_retries=0)
    assert web_tools._firecrawl_client is cached


@pytest.mark.parametrize("backend", ["xai", "brave-free", "searxng", "ddgs", "custom"])
def test_ungoverned_web_provider_rejected_before_any_credential_probe(monkeypatch, backend):
    from tools import web_tools
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"search_backend": backend})
    with patch("agent.web_search_registry.get_provider") as registry, patch("tools.web_tools._is_backend_available") as credential_probe:
        with pytest.raises(policy.DelegationDenied, match="not governed"):
            policy.resolve_web_readers()
    registry.assert_not_called()
    credential_probe.assert_not_called()


def test_scoped_web_dispatch_keeps_captured_implementation_after_registry_change(monkeypatch):
    from tools import web_tools
    from agent import web_search_registry
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "tavily"})
    from plugins.web.tavily import provider as tavily
    calls = []
    def captured(self, query, limit=5):
        calls.append(query)
        return {"success": True, "data": {"web": []}}
    captured.__module__ = tavily.__name__
    monkeypatch.setattr(tavily.TavilyWebSearchProvider, "search", captured)
    readers = policy.resolve_web_readers()
    monkeypatch.setattr(tavily.TavilyWebSearchProvider, "search", lambda *args, **kwargs: pytest.fail("replacement class method invoked"))
    monkeypatch.setattr(web_search_registry, "get_provider", lambda *args: pytest.fail("dynamic provider invoked"))
    monkeypatch.setattr(web_tools, "_get_search_backend", lambda: pytest.fail("dynamic credentials probed"))
    monkeypatch.setattr(policy, "tool_allowed", lambda agent, name: True)
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION, _atlas_paid_dispatch_lock=threading.Lock(), _atlas_web_readers=readers)
    with policy.dispatch_context(agent):
        assert json.loads(web_tools.web_search_tool("safe query"))["success"] is True
    assert calls == ["safe query"]


def test_queued_web_send_denied_after_terminal_closure(monkeypatch):
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION, _atlas_paid_dispatch_lock=threading.Lock(), _atlas_delegation_identities={"actor_user_id": "actor", "subject_user_id": "subject", "view_as_session_id": "session", "resource_type": "chat", "resource_id": "turn_fixture"})
    monkeypatch.setattr(policy, "tool_allowed", lambda *args: True)
    policy.terminal_usage_calls(agent)
    with policy.dispatch_context(agent), pytest.raises(policy.DelegationDenied, match="closed"):
        policy.validate_web_send()
