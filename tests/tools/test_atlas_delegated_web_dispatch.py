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
    import sys
    from plugins.web.firecrawl import provider as firecrawl
    from tools import web_tools
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fixture")
    cached = object()
    monkeypatch.setattr(web_tools, "_firecrawl_client", cached)
    constructor = MagicMock()
    monkeypatch.setitem(sys.modules, "firecrawl", SimpleNamespace(Firecrawl=constructor))
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


@pytest.mark.parametrize("backend,method,key,module,constructor", [
    ("firecrawl", "_get_firecrawl_client", "FIRECRAWL_API_KEY", "firecrawl", "Firecrawl"),
    ("parallel", "_get_sync_client", "PARALLEL_API_KEY", "parallel", "Parallel"),
    ("parallel", "_get_async_client", "PARALLEL_API_KEY", "parallel", "AsyncParallel"),
    ("exa", "_get_exa_client", "EXA_API_KEY", "exa_py", "Exa"),
])
def test_scoped_sdk_setup_never_installs_or_resolves_managed_auth(monkeypatch, backend, method, key, module, constructor):
    import importlib
    import sys
    provider = importlib.import_module("plugins.web." + backend + ".provider")
    create = MagicMock(return_value=SimpleNamespace(headers={}, _v2_client=SimpleNamespace(http_client=SimpleNamespace())))
    monkeypatch.setitem(sys.modules, module, SimpleNamespace(**{constructor: create}))
    monkeypatch.setenv(key, "fixture")
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION, _atlas_paid_dispatch_lock=threading.Lock())
    with patch("tools.lazy_deps.ensure") as install, patch("tools.web_tools.resolve_managed_tool_gateway") as managed, patch("tools.web_tools._read_nous_access_token") as oauth:
        with policy.dispatch_context(agent):
            getattr(provider, method)()
        create.assert_called_once()
        agent._atlas_admission_closed = True
        with policy.dispatch_context(agent), pytest.raises(policy.DelegationDenied, match="closed"):
            getattr(provider, method)()
        create.assert_called_once()
    install.assert_not_called()
    managed.assert_not_called()
    oauth.assert_not_called()


def test_scoped_firecrawl_without_direct_key_rejects_managed_oauth(monkeypatch):
    from plugins.web.firecrawl import provider as firecrawl
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    with patch("tools.web_tools.resolve_managed_tool_gateway") as managed, patch("tools.lazy_deps.ensure") as install:
        agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION, _atlas_paid_dispatch_lock=threading.Lock())
        with policy.dispatch_context(agent), pytest.raises(ValueError, match="direct process credential"):
            firecrawl._get_firecrawl_client()
    managed.assert_not_called()
    install.assert_not_called()


def test_queued_closed_firecrawl_worker_never_constructs_sdk_or_resolves_oauth(monkeypatch):
    from plugins.web.firecrawl import provider as firecrawl
    monkeypatch.setattr(firecrawl, "check_website_access", lambda url: None)
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION, _atlas_paid_dispatch_lock=threading.Lock())
    async def worker(func, *args, **kwargs):
        agent._atlas_admission_closed = True
        return func(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", worker)
    with patch.object(firecrawl, "_get_firecrawl_client") as setup, patch("tools.web_tools.resolve_managed_tool_gateway") as oauth, patch("tools.lazy_deps.ensure") as install:
        with policy.dispatch_context(agent):
            result = asyncio.run(firecrawl.FirecrawlWebSearchProvider().extract(["https://example.com/"]))
    assert "closed" in result[0]["error"]
    setup.assert_not_called()
    oauth.assert_not_called()
    install.assert_not_called()


def test_live_firecrawl_setup_is_followed_by_fresh_send_guard(monkeypatch):
    from plugins.web.firecrawl import provider as firecrawl
    monkeypatch.setattr(firecrawl, "check_website_access", lambda url: None)
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION, _atlas_paid_dispatch_lock=threading.Lock())
    client = SimpleNamespace(scrape=MagicMock())
    def setup():
        agent._atlas_admission_closed = True
        return client
    monkeypatch.setattr(firecrawl, "_get_firecrawl_client", setup)
    with policy.dispatch_context(agent):
        result = asyncio.run(firecrawl.FirecrawlWebSearchProvider().extract(["https://example.com/"]))
    assert "closed" in result[0]["error"]
    client.scrape.assert_not_called()


@pytest.mark.parametrize("backend,method,key,module", [
    ("firecrawl", "_get_firecrawl_client", "FIRECRAWL_API_KEY", "firecrawl"),
    ("parallel", "_get_sync_client", "PARALLEL_API_KEY", "parallel"),
    ("parallel", "_get_async_client", "PARALLEL_API_KEY", "parallel"),
    ("exa", "_get_exa_client", "EXA_API_KEY", "exa_py"),
])
def test_scoped_missing_sdk_fails_without_lazy_install(monkeypatch, backend, method, key, module):
    import importlib
    import sys
    provider = importlib.import_module("plugins.web." + backend + ".provider")
    monkeypatch.setenv(key, "fixture")
    monkeypatch.setitem(sys.modules, module, None)
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION, _atlas_paid_dispatch_lock=threading.Lock())
    with patch("tools.lazy_deps.ensure") as install, policy.dispatch_context(agent), pytest.raises(ImportError):
        getattr(provider, method)()
    install.assert_not_called()


def test_closed_tavily_never_reads_credentials_or_sends(monkeypatch):
    from plugins.web.tavily import provider as tavily
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION, _atlas_paid_dispatch_lock=threading.Lock(), _atlas_admission_closed=True)
    with patch("hermes_cli.config.get_env_value") as lookup, patch("httpx.post") as send, policy.dispatch_context(agent), pytest.raises(policy.DelegationDenied, match="closed"):
        tavily._tavily_request("extract", {})
    lookup.assert_not_called()
    send.assert_not_called()


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_scoped_firecrawl_redirect_is_rejected_before_second_send(monkeypatch, status):
    import sys
    import requests
    from plugins.web.firecrawl import provider as firecrawl
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION, _atlas_paid_dispatch_lock=threading.Lock())
    checks = []
    monkeypatch.setattr(policy, "tool_allowed", lambda agent, name: checks.append(name) or True)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fixture")
    transport = SimpleNamespace(_build_url=lambda endpoint: "https://api.firecrawl.dev" + endpoint, _prepare_headers=lambda: {"Authorization": "Bearer fixture"})
    sdk = SimpleNamespace(_v2_client=SimpleNamespace(http_client=transport))
    sdk.search = lambda **kwargs: transport.post("/v2/search", kwargs)
    monkeypatch.setitem(sys.modules, "firecrawl", SimpleNamespace(Firecrawl=lambda **kwargs: sdk))
    response = requests.Response()
    response.status_code = status
    response.headers["Location"] = "https://api.firecrawl.dev/second"
    def send(self, prepared, **kwargs):
        assert self.trust_env is False
        assert prepared.headers["Authorization"] == "Bearer fixture"
        assert kwargs["allow_redirects"] is False
        agent._atlas_admission_closed = True
        return response
    with patch("requests.Session.send", autospec=True, side_effect=send) as actual, policy.dispatch_context(agent):
        result = firecrawl.FirecrawlWebSearchProvider().search("read")
    assert result["success"] is False and "redirect" in result["error"]
    actual.assert_called_once()
    assert checks == ["web_search", "web_search", "web_search"]


@pytest.mark.parametrize("async_mode", [False, True])
def test_scoped_parallel_transport_does_not_follow_redirects(monkeypatch, async_mode):
    import sys
    import httpx
    from plugins.web.parallel import provider as parallel
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION, _atlas_paid_dispatch_lock=threading.Lock())
    monkeypatch.setenv("PARALLEL_API_KEY", "fixture")
    checks, sends = [], []
    monkeypatch.setattr(policy, "tool_allowed", lambda agent, name: checks.append(name) or True)
    def sdk(**kwargs):
        kwargs["http_client"].headers["x-api-key"] = kwargs["api_key"]
        return kwargs["http_client"]
    monkeypatch.setitem(sys.modules, "parallel", SimpleNamespace(Parallel=sdk, AsyncParallel=sdk))
    def send(request):
        sends.append(request.url.path)
        agent._atlas_admission_closed = True
        return httpx.Response(307, headers={"location": "/second"})
    if async_mode:
        async def run():
            client = parallel._get_async_client()
            client._transport = httpx.MockTransport(send)
            try:
                return await client.post("https://api.parallel.ai/first")
            finally:
                await client.aclose()
        with policy.dispatch_context(agent):
            response = asyncio.run(run())
    else:
        with policy.dispatch_context(agent):
            client = parallel._get_sync_client()
            client._transport = httpx.MockTransport(send)
            try:
                response = client.post("https://api.parallel.ai/first")
            finally:
                client.close()
    assert response.status_code == 307 and sends == ["/first"]
    assert checks == ["web_extract" if async_mode else "web_search"]


def test_long_scoped_extraction_has_no_persistent_cache_or_debug_write(monkeypatch):
    from tools import web_tools
    async def safe(url):
        return True
    monkeypatch.setattr(web_tools, "async_is_safe_url", safe)
    def extract(urls, **kwargs):
        return [{"url": urls[0], "content": "x" * 15001}]
    provider = SimpleNamespace(name="fixture")
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION, _atlas_paid_dispatch_lock=threading.Lock(), _atlas_web_readers={"web_extract": (provider, extract)})
    monkeypatch.setattr(policy, "tool_allowed", lambda *args: True)
    with patch.object(web_tools, "_store_full_text") as store, patch.object(web_tools._debug, "save") as debug, policy.dispatch_context(agent):
        result = json.loads(asyncio.run(web_tools.web_extract_tool(["https://example.com/"], char_limit=15000)))
    content = result["results"][0]["content"]
    assert "TRUNCATED" in content and "not stored" in content
    assert "read_file" not in content and "browser_navigate" not in content
    store.assert_not_called()
    debug.assert_not_called()



def test_scoped_exa_sdk_request_rejects_redirect_without_second_hop(monkeypatch):
    import sys
    from plugins.web.exa import provider as exa
    monkeypatch.setenv("EXA_API_KEY", "fixture")
    client = SimpleNamespace(headers={"x-api-key": "fixture"}, base_url="https://api.exa.ai")
    monkeypatch.setitem(sys.modules, "exa_py", SimpleNamespace(Exa=lambda **kwargs: client))
    monkeypatch.setitem(sys.modules, "exa_py.api", SimpleNamespace(ExaJSONEncoder=json.JSONEncoder))
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION, _atlas_paid_dispatch_lock=threading.Lock())
    monkeypatch.setattr(policy, "tool_allowed", lambda *args: True)
    response = SimpleNamespace(status_code=307)
    with policy.dispatch_context(agent), patch("requests.Session.send", return_value=response) as send:
        actual = exa._get_exa_client()
        with pytest.raises(policy.DelegationDenied, match="redirect"):
            actual.request("/search", {"query": "read"})
    send.assert_called_once()
    assert send.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize('backend', ['firecrawl', 'exa'])
def test_actual_scoped_sdk_requests_never_load_netrc_and_keep_process_auth(monkeypatch, backend):
    import importlib
    import requests
    provider = importlib.import_module('plugins.web.' + backend + '.provider')
    key_env = 'FIRECRAWL_API_KEY' if backend == 'firecrawl' else 'EXA_API_KEY'
    monkeypatch.setenv(key_env, 'synthetic-process-key')
    monkeypatch.setenv('HTTP_PROXY', 'http://synthetic-ambient-proxy.invalid')
    monkeypatch.setenv('HTTPS_PROXY', 'http://synthetic-ambient-proxy.invalid')
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION, _atlas_paid_dispatch_lock=threading.Lock())
    monkeypatch.setattr(policy, 'tool_allowed', lambda *args: True)
    sends, preparation = [], []
    original = requests.Session.prepare_request
    def prepare(session, request):
        assert session.trust_env is False and session.auth is None and not session.cookies
        assert session.get_adapter(request.url).max_retries.total == 0
        preparation.append(True)
        return original(session, request)
    def send(adapter, prepared, **kwargs):
        assert kwargs['proxies'] == {} and kwargs['verify'] is True
        assert kwargs['stream'] is False
        expected = ('Authorization', 'Bearer synthetic-process-key') if backend == 'firecrawl' else ('x-api-key', 'synthetic-process-key')
        assert prepared.headers[expected[0]] == expected[1]
        assert not prepared.headers.get('Cookie')
        if backend == 'exa':
            assert not prepared.headers.get('Authorization')
        sends.append(prepared)
        response = requests.Response()
        response.status_code = 200
        response.request = prepared
        response.url = prepared.url
        response._content = json.dumps({'success': True, 'data': {'web': []}} if backend == 'firecrawl' else {'results': [], 'requestId': 'synthetic'}).encode()
        return response
    with policy.dispatch_context(agent), patch('requests.sessions.get_netrc_auth', side_effect=AssertionError('Forbidden netrc lookup')) as netrc, patch('requests.sessions.get_environ_proxies', side_effect=AssertionError('Forbidden ambient proxy lookup')) as proxies, patch.object(requests.Session, 'prepare_request', autospec=True, side_effect=prepare), patch.object(requests.adapters.HTTPAdapter, 'send', autospec=True, side_effect=send):
        # Actual installed frozen SDK constructors and their real header preparation.
        # Missing frozen extras are a test setup failure, never a silent skip.
        client = getattr(provider, '_get_firecrawl_client' if backend == 'firecrawl' else '_get_exa_client')()
        if backend == 'firecrawl':
            client.search('read')
        else:
            client.request('/search', {'query': 'read'})
        netrc.assert_not_called()
        proxies.assert_not_called()
    assert len(sends) == len(preparation) == 1


@pytest.mark.parametrize('change', ['revoked', 'auth', 'cookie'])
def test_requests_send_rechecks_prepared_auth_and_live_authority(monkeypatch, change):
    import requests
    monkeypatch.setenv('FIRECRAWL_API_KEY', 'synthetic-process-key')
    live = [True]
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION, _atlas_paid_dispatch_lock=threading.Lock())
    monkeypatch.setattr(policy, 'tool_allowed', lambda *args: live[0])
    original = requests.Session.prepare_request
    def prepare(session, request):
        prepared = original(session, request)
        if change == 'revoked':
            live[0] = False
        elif change == 'auth':
            prepared.headers['Authorization'] = 'Basic synthetic-stored-auth'
        else:
            prepared.headers['Cookie'] = 'synthetic-stored-cookie'
        return prepared
    with policy.dispatch_context(agent), patch.object(requests.Session, 'prepare_request', autospec=True, side_effect=prepare), patch.object(requests.adapters.HTTPAdapter, 'send') as send:
        with pytest.raises(policy.DelegationDenied):
            policy.scoped_requests_post('https://api.firecrawl.dev/v2/search', 'web_search',
                credential_env='FIRECRAWL_API_KEY', headers={'Authorization': 'Bearer synthetic-process-key'}, json={'query': 'read'})
        send.assert_not_called()


def test_scoped_tavily_has_isolated_prepared_transport(monkeypatch):
    import httpx
    from plugins.web.tavily import provider as tavily
    monkeypatch.setenv('TAVILY_API_KEY', 'synthetic-process-key')
    monkeypatch.setenv('HTTPS_PROXY', 'http://synthetic-ambient-proxy.invalid')
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION, _atlas_paid_dispatch_lock=threading.Lock())
    checks, sends = [], []
    monkeypatch.setattr(policy, 'tool_allowed', lambda agent, name: checks.append(name) or True)
    def send(request):
        assert json.loads(request.content)['api_key'] == 'synthetic-process-key'
        assert not request.headers.get('authorization') and not request.headers.get('cookie')
        sends.append(request)
        return httpx.Response(200, json={'results': []})
    original = httpx.Client
    class IsolatedClient(original):
        def __init__(self, **kwargs):
            assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
            super().__init__(transport=httpx.MockTransport(send), **kwargs)
    monkeypatch.setattr(httpx, 'Client', IsolatedClient)
    with policy.dispatch_context(agent), patch('httpx.post') as ordinary:
        assert tavily._tavily_request('search', {'api_key': 'synthetic-config-key'}) == {'results': []}
        ordinary.assert_not_called()
    assert len(sends) == 1 and checks == ['web_search', 'web_search']
