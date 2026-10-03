"""Fresh authorization belongs at actual sends, after async checks/queues."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

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
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION)
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
    with policy.dispatch_context(SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION)):
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
    with policy.dispatch_context(SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION)):
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
    with policy.dispatch_context(SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION)):
        firecrawl._get_firecrawl_client()
    constructor.assert_called_once_with(api_key="fixture", max_retries=0)
    assert web_tools._firecrawl_client is cached
