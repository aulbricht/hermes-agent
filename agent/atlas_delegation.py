"""Authenticated Atlas View As lane: fixed readers and fresh dispatch authority.

This is a server policy, never a caller-supplied list of tool names. Transcript
persistence remains enabled; personal memory, skills and plugin writes do not.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import threading
from types import MappingProxyType
import urllib.request
from urllib.parse import urlsplit, quote

POLICY_VERSION = "view-as-v1"
ALLOWED_TOOLS = frozenset({
    "web_search", "web_extract",
    "mcp__acre_filemaker__find_records", "mcp__acre_filemaker__get_record",
    "mcp__acre_filemaker__find_properties_within_radius", "mcp__acre_filemaker__convert_temporal",
    "mcp__atlas_vault__vault_search", "mcp__atlas_vault__vault_read",
})
TOOLSETS = ["web", "acre-filemaker", "atlas_vault"]
_VALIDATION_PATH = "/api/v1/internal/view-as/validate"
_VALIDATION_URL = "http://127.0.0.1:8243" + _VALIDATION_PATH
_TOKEN_PATH = Path("/etc/acre-atlas/view-as-dispatch.token")


class DelegationDenied(RuntimeError):
    pass


def is_scoped(agent):
    return getattr(agent, "_atlas_delegation_policy", None) == POLICY_VERSION


def _protected(path: Path):
    # Resolve trusted release symlinks, then check code and every containing
    # directory. A root-owned file inside a user-writable directory is unsafe.
    resolved = path.resolve(strict=True)
    for item in (resolved, *resolved.parents):
        info = item.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise DelegationDenied("Atlas delegation transport is not protected")
    return resolved


def _transport_fingerprint(name, config):
    if not isinstance(config, dict) or config.get("enabled", True) is not True:
        raise DelegationDenied("Atlas delegation reader is unavailable")
    if any(config.get(key) for key in ("url", "headers", "auth", "cwd")):
        raise DelegationDenied("Atlas delegation reader transport is not approved")
    if (config.get("sampling", {}).get("enabled") is not False
            or config.get("elicitation", {}).get("enabled") is not False
            or config.get("tools", {}).get("resources") is not False
            or config.get("tools", {}).get("prompts") is not False):
        raise DelegationDenied("Atlas delegation reader side channels are enabled")
    command, args, env = config.get("command"), config.get("args"), config.get("env", {})
    if name == "acre-filemaker":
        expected = ["-n", "-H", "-u", "acrefm", "--", "/usr/local/libexec/acre-filemaker-mcp"]
        if command != "/usr/bin/sudo" or args != expected or env:
            raise DelegationDenied("Atlas FileMaker transport is not approved")
        _protected(Path(command))
        _protected(Path(expected[-1]))
        _protected(Path("/opt/acre-filemaker-mcp/current/src/server.js"))
    elif name == "atlas_vault":
        if not isinstance(command, str) or not re.fullmatch(r"/opt/acre-atlas/releases/[0-9a-f]{16}/backend/\.venv/bin/python", command):
            raise DelegationDenied("Atlas vault transport is not pinned")
        if args != ["-P", "-m", "atlas.vault_mcp"] or env != {"ATLAS_VAULT_ROOT": "/var/lib/hermes-acre/vaults/acre-commercial", "PYTHONPATH": str(Path(command).parents[2])}:
            raise DelegationDenied("Atlas vault transport is not approved")
        _protected(Path(command))
        backend = Path(command).parents[2]
        _protected(backend / "atlas/vault_mcp.py")
    else:
        raise DelegationDenied("Atlas delegation reader is unknown")
    return json.dumps({"command": command, "args": args, "env": env}, sort_keys=True, separators=(",", ":"))


def _live_reader_fingerprints():
    from tools import mcp_tool
    result = {}
    with mcp_tool._lock:
        for name in ("acre-filemaker", "atlas_vault"):
            server = mcp_tool._servers.get(name)
            if server is None or server.session is None:
                raise DelegationDenied("Atlas delegation reader is disconnected")
            result[name] = _transport_fingerprint(name, server._config)
    return result


def resolve_policy():
    # General inventory invokes dynamic provider availability/credential probes.
    # The scoped inventory uses only these captured, provenance-checked entries.
    resolve_web_readers()
    from tools.registry import registry
    from tools import mcp_tool
    selected = {name: {"type": "function", "function": json.loads(json.dumps(registry._tools[name].schema))}
        for name in ALLOWED_TOOLS if name in registry._tools}
    if set(selected) != ALLOWED_TOOLS:
        raise DelegationDenied("Atlas delegation reader schemas are unavailable")
    fingerprints = _live_reader_fingerprints()
    entries = {}
    with mcp_tool._lock:
        for name in ALLOWED_TOOLS:
            entry = registry._tools.get(name)
            if entry is None or not callable(entry.handler):
                raise DelegationDenied("Atlas delegation reader handler is unavailable")
            if name.startswith("mcp__"):
                expected = "acre_filemaker" if name.startswith("mcp__acre_filemaker__") else "atlas_vault"
                if entry.handler.__module__ != "tools.mcp_tool" or mcp_tool._mcp_tool_server_names.get(name) != expected:
                    raise DelegationDenied("Atlas delegation reader provenance is invalid")
            elif entry.handler.__module__ != "tools.web_tools":
                raise DelegationDenied("Atlas web reader provenance is invalid")
            if bool(entry.is_async) != (name == "web_extract"):
                raise DelegationDenied("Atlas delegation reader execution mode is invalid")
            entries[name] = (entry.handler, json.dumps(entry.schema, sort_keys=True), bool(entry.is_async))
    return [selected[name] for name in sorted(selected)], entries, fingerprints


def capability():
    resolve_web_readers()
    resolve_policy()
    _read_dispatch_key()
    return {"policy_version": POLICY_VERSION, "executor_enforced": True, "allowed_tools": sorted(ALLOWED_TOOLS)}


def bind_policy(agent, identities):
    if getattr(agent, "api_mode", "codex_responses") != "codex_responses":
        raise DelegationDenied("Atlas delegation requires the governed Responses lane")
    if getattr(agent, "provider", None) == "moa":
        raise DelegationDenied("Atlas delegation does not permit model delegation")
    readers = resolve_web_readers()
    schemas, entries, fingerprints = resolve_policy()
    agent._atlas_delegation_policy = POLICY_VERSION
    agent._atlas_delegation_allowed_tools = ALLOWED_TOOLS
    agent._atlas_delegation_entries = MappingProxyType(entries)
    agent._atlas_delegation_fingerprints = MappingProxyType(fingerprints)
    agent._atlas_delegation_identities = MappingProxyType(dict(identities))
    agent._atlas_paid_dispatch_lock = threading.Lock()
    agent._atlas_paid_dispatch_count = 0
    agent._atlas_auxiliary_usage_calls = []
    agent._atlas_primary_usage_calls = []
    agent._atlas_paid_attempts = []
    agent._atlas_paid_uncertain = False
    agent._atlas_admission_closed = False
    agent._atlas_paid_active = set()
    agent._atlas_paid_condition = threading.Condition(agent._atlas_paid_dispatch_lock)
    agent._atlas_web_readers = MappingProxyType(readers)
    agent.tools = schemas
    agent.valid_tool_names = ALLOWED_TOOLS
    agent._skip_mcp_refresh = True
    agent._memory_store = agent._memory_manager = None
    agent._memory_enabled = agent._user_profile_enabled = False
    agent._memory_nudge_interval = agent._skill_nudge_interval = 0
    agent.context_compressor._atlas_dispatch_validator = lambda: validate_dispatch(agent)


def resolve_web_readers():
    """Capture approved implementations without plugin discovery/credential probes."""
    from tools import web_tools
    import importlib
    classes = {"firecrawl": "FirecrawlWebSearchProvider", "exa": "ExaWebSearchProvider",
        "parallel": "ParallelWebSearchProvider", "tavily": "TavilyWebSearchProvider"}
    config = web_tools._load_web_config()
    readers = {}
    for capability in ("search", "extract"):
        backend = str(config.get(capability + "_backend") or config.get("backend") or "").lower().strip()
        if not backend:
            backend = next((name for name, key in (("tavily", "TAVILY_API_KEY"), ("exa", "EXA_API_KEY"), ("parallel", "PARALLEL_API_KEY"), ("firecrawl", "FIRECRAWL_API_KEY")) if web_tools._has_env(key)), "firecrawl")
        if backend not in classes:
            raise DelegationDenied("Atlas delegation web provider is not governed")
        module = importlib.import_module("plugins.web." + backend + ".provider")
        implementation = getattr(module, classes[backend])
        instance = implementation()
        method = getattr(implementation, capability)
        if method.__module__ != module.__name__:
            raise DelegationDenied("Atlas delegation web provider implementation changed")
        readers["web_" + capability] = (instance, method.__get__(instance, implementation))
    return readers


def validate_prepared_web_auth(headers, credential_env, *, body=None):
    """Explicit process credential only, checked after HTTP preparation."""
    bindings = {"FIRECRAWL_API_KEY": ("authorization", "Bearer "),
        "EXA_API_KEY": ("x-api-key", ""), "PARALLEL_API_KEY": ("x-api-key", ""),
        "TAVILY_API_KEY": (None, "")}
    if credential_env not in bindings:
        raise DelegationDenied("Atlas web credential binding is unavailable")
    key = readonly_web_env(credential_env)
    name, prefix = bindings[credential_env]
    normalized = {str(k).lower(): v for k, v in headers.items()}
    if not key or "cookie" in normalized:
        raise DelegationDenied("Atlas web authentication changed")
    forbidden = _AUTH_HEADERS - ({name} if name else set())
    if any(header in normalized for header in forbidden):
        raise DelegationDenied("Atlas web authentication changed")
    if name:
        if normalized.get(name) != prefix + key:
            raise DelegationDenied("Atlas web authentication changed")
    else:
        try:
            payload = json.loads(body)
        except Exception as error:
            raise DelegationDenied("Atlas web authentication changed") from error
        if not isinstance(payload, dict) or payload.get("api_key") != key:
            raise DelegationDenied("Atlas web authentication changed")


def scoped_requests_post(url, name, *, credential_env, **kwargs):
    """Fresh isolated Requests send; no netrc, proxy, auth, cookies or retries."""
    import requests
    validate_web_send(name)
    if set(kwargs) - {"headers", "json", "data", "timeout"}:
        raise DelegationDenied("Atlas web transport options are unavailable")
    with requests.Session() as session:
        session.trust_env = False
        session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        session.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))
        prepared = session.prepare_request(requests.Request("POST", url,
            headers=kwargs.get("headers"), json=kwargs.get("json"), data=kwargs.get("data")))
        validate_prepared_web_auth(prepared.headers, credential_env)
        validate_web_send(name)  # Preparation can outlive revocation/terminal close.
        response = session.send(prepared, timeout=kwargs.get("timeout", 60),
            allow_redirects=False, proxies={}, verify=True, stream=False)
    if 300 <= response.status_code < 400:
        raise DelegationDenied("Atlas delegation web redirects are unavailable")
    return response


def scoped_runtime_kwargs(config, requested=None):
    """Process credentials plus read-only config; no auth pools/recovery/OAuth."""
    def check_headers(value):
        if isinstance(value, dict):
            for name, child in value.items():
                if name in ("default_headers", "extra_headers"):
                    reject_auth_headers(child)
                elif isinstance(child, (dict, list)):
                    check_headers(child)
        elif isinstance(value, list):
            for child in value:
                check_headers(child)
    check_headers(config)
    model = config.get("model", {})
    model = model if isinstance(model, dict) else {}
    provider = requested or model.get("provider") or "openai-api"
    routes = {"openai-api": ("OPENAI_API_KEY", "https://api.openai.com/v1"),
        "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1")}
    if provider not in routes:
        raise DelegationDenied("Atlas delegation model provider is not governed")
    key_name, base_url = routes[provider]
    key = os.environ.get(key_name, "").strip()
    if not key:
        raise DelegationDenied("Atlas delegation requires a process model credential")
    return {"provider": provider, "api_key": key, "base_url": base_url,
        "api_mode": "codex_responses", "credential_pool": None,
        "max_tokens": model.get("max_tokens") if isinstance(model.get("max_tokens"), int) else None}


def readonly_web_env(name):
    """Scoped credentials are process-only; never resolve OAuth or modify auth."""
    check_admission_open(current_dispatch_agent())
    return os.environ.get(name, "").strip()


def scoped_web_reader(name):
    agent = current_dispatch_agent()
    check_admission_open(agent)
    reader = getattr(agent, "_atlas_web_readers", {}).get(name)
    if reader is None:
        raise DelegationDenied("Atlas delegation web provider is unavailable")
    return reader


def _check_admission_open_locked(agent):
    if getattr(agent, "_atlas_admission_closed", False) or getattr(agent, "_interrupt_requested", False):
        raise DelegationDenied("Atlas delegation dispatch is closed")


def check_admission_open(agent):
    if is_scoped(agent):
        with agent._atlas_paid_dispatch_lock:
            _check_admission_open_locked(agent)


def _read_dispatch_key():
    import grp
    info = _TOKEN_PATH.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o640 or info.st_gid != grp.getgrnam("atlaschat").gr_gid:
        raise DelegationDenied("Atlas delegation validator credential is unavailable")
    _protected(_TOKEN_PATH.parent)
    key = _TOKEN_PATH.read_bytes().strip()
    if len(key) < 32:
        raise DelegationDenied("Atlas delegation validator credential is invalid")
    return key


def validate_dispatch(agent):
    if not is_scoped(agent):
        return
    check_admission_open(agent)
    context = agent._atlas_delegation_identities
    if not valid_resource(context.get("resource_type"), context.get("resource_id")):
        raise DelegationDenied("Atlas delegation requires a scoped resource")
    _request_authorization(context)
    check_admission_open(agent)  # The callback can outlive terminal collection.


def _request_authorization(context):
    """Shared signed live callback; caller must validate its distinct resource lane."""
    envelope = {**{key: context[key] for key in ("actor_user_id", "subject_user_id", "view_as_session_id", "resource_type", "resource_id")}, "method": "GET", "path": _VALIDATION_PATH,
        "body_sha256": hashlib.sha256(b"").hexdigest(), "issued_at": int(time.time()),
        "nonce": secrets.token_hex(16)}
    encoded = base64.urlsafe_b64encode(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()).decode()
    signature = hmac.new(_read_dispatch_key(), encoded.encode(), hashlib.sha256).hexdigest()
    url = os.environ.get("ATLAS_VIEW_AS_AUTHORIZATION_URL", _VALIDATION_URL)
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port not in (8243, 8244)
            or parsed.path != _VALIDATION_PATH or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise DelegationDenied("Atlas delegation validator URL is invalid")
    request = urllib.request.Request(url, method="GET", headers={
        "X-Atlas-Delegation": encoded, "X-Atlas-Delegation-Signature": signature,
        "X-Atlas-User-Key": context["subject_user_id"],
        "X-Atlas-Resource-Type": context["resource_type"],
        "X-Atlas-Resource-Id": context["resource_id"],
    })
    try:
        # Ignore ambient HTTP proxy settings and never follow redirects carrying
        # the signed envelope to another service.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=3) as response:
            payload = json.loads(response.read(4097))
            if response.status != 200 or not isinstance(payload, dict) or payload.get("allowed") is not True:
                raise DelegationDenied("Atlas delegation is no longer authorized")
    except Exception as error:
        raise DelegationDenied("Atlas delegation is no longer authorized") from error


CONTROL_POLICY_VERSION = "view-as-control-v1"
_control_nonce_lock = threading.Lock()
_control_nonces = {}


def verify_session_title_control(headers, *, method, path, raw_body, native_id, body):
    """Authenticated nonpaid title-only lane; never admitted as an agent resource."""
    fields = {"actor_user_id", "subject_user_id", "view_as_session_id", "method", "path",
        "body_sha256", "issued_at", "nonce", "resource_type", "resource_id"}
    if (headers.get("X-Atlas-Delegation-Policy") != CONTROL_POLICY_VERSION
            or method != "PATCH" or path != "/api/sessions/" + quote(native_id, safe="")
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", native_id)
            or len(raw_body) > 4096 or not isinstance(body, dict) or set(body) != {"title"}
            or not isinstance(body["title"], str) or len(body["title"]) > 100):
        raise DelegationDenied("Invalid Atlas session title control")
    encoded, signature = headers.get("X-Atlas-Delegation", ""), headers.get("X-Atlas-Delegation-Signature", "")
    if len(encoded) > 4096 or not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise DelegationDenied("Invalid Atlas control signature")
    expected = hmac.new(_read_dispatch_key(), encoded.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise DelegationDenied("Invalid Atlas control signature")
    try:
        context = json.loads(base64.b64decode(encoded, altchars=b"-_", validate=True))
        canonical = base64.urlsafe_b64encode(json.dumps(context, sort_keys=True, separators=(",", ":")).encode()).decode()
    except Exception as error:
        raise DelegationDenied("Invalid Atlas control envelope") from error
    if (not isinstance(context, dict) or set(context) != fields or encoded != canonical
            or context["method"] != method or context["path"] != path
            or context["body_sha256"] != hashlib.sha256(raw_body).hexdigest()
            or context["resource_type"] != "chat_session" or context["resource_id"] != native_id
            or headers.get("X-Atlas-Resource-Type") != "chat_session"
            or headers.get("X-Atlas-Resource-Id") != native_id
            or headers.get("X-Atlas-User-Key") != context["subject_user_id"]
            or any(not isinstance(context[key], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", context[key])
                   for key in ("actor_user_id", "subject_user_id", "view_as_session_id"))
            or type(context["issued_at"]) is not int
            or not isinstance(context["nonce"], str) or not re.fullmatch(r"[0-9a-f]{32}", context["nonce"])):
        raise DelegationDenied("Invalid Atlas control envelope")
    now = time.time()
    if not -5 <= now - context["issued_at"] <= 60:
        raise DelegationDenied("Expired Atlas control envelope")
    with _control_nonce_lock:
        for nonce, timestamp in list(_control_nonces.items()):
            if now - timestamp > 65:
                del _control_nonces[nonce]
        if context["nonce"] in _control_nonces or len(_control_nonces) >= 4096:
            raise DelegationDenied("Replayed or unavailable Atlas control envelope")
        _control_nonces[context["nonce"]] = now
    return context


def validate_session_title_control(context):
    if context.get("resource_type") != "chat_session":
        raise DelegationDenied("Invalid Atlas control resource")
    _request_authorization(context)


def tool_allowed(agent, name):
    if not is_scoped(agent):
        return True
    if name not in getattr(agent, "_atlas_delegation_allowed_tools", frozenset()):
        return False
    from tools.registry import registry
    try:
        entry = registry._tools.get(name)
        frozen = agent._atlas_delegation_entries[name]
        if entry is None or entry.handler is not frozen[0] or json.dumps(entry.schema, sort_keys=True) != frozen[1] or bool(entry.is_async) != frozen[2]:
            return False
        if _live_reader_fingerprints() != dict(agent._atlas_delegation_fingerprints):
            return False
        validate_dispatch(agent)
        check_admission_open(agent)
        return True
    except Exception:
        return False


def dispatch_tool(agent, name, args, task_id):
    """Execute the captured handler, never a replacement registry entry."""
    if not tool_allowed(agent, name):
        return json.dumps({"error": "Tool is unavailable in this Atlas View As session"})
    from tools.registry import registry
    handler, _, asynchronous = agent._atlas_delegation_entries[name]
    with dispatch_context(agent):
        result = handler(args, task_id=task_id)
        if asynchronous:
            from model_tools import _run_async
            result = _run_async(result)
    return registry._normalize_handler_result(name, result)


_credential_scope = ContextVar("atlas_delegation_credential_scope", default=False)


def credential_scope_active():
    return _credential_scope.get() or current_dispatch_agent() is not None


def deny_shared_credentials():
    if credential_scope_active():
        raise DelegationDenied("Atlas delegation forbids shared credential recovery or writes")


@contextmanager
def credential_scope():
    token = _credential_scope.set(True)
    try:
        yield
    finally:
        _credential_scope.reset(token)


def scoped_construction(function):
    """Trusted constructor boundary, before resolver/constructor side effects."""
    import inspect
    from functools import wraps
    signature = inspect.signature(function)
    @wraps(function)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        identities = bound.arguments.get("atlas_delegation_context")
        policy = bound.arguments.get("atlas_delegation_policy")
        if identities is None and policy is None:
            return function(*args, **kwargs)
        if policy not in (None, POLICY_VERSION):
            raise DelegationDenied("Unknown Atlas delegation policy")
        with credential_scope():
            resolve_web_readers()  # Fixed implementation inventory, never ordinary probes.
            if policy is not None:
                runtime = scoped_runtime_kwargs({}, bound.arguments.get("provider") or "openai-api")
                for key in ("provider", "api_key", "base_url", "api_mode", "credential_pool"):
                    if key in signature.parameters:
                        bound.arguments[key] = runtime[key]
                if "quiet_mode" in signature.parameters:
                    bound.arguments["quiet_mode"] = True
                if "fallback_model" in signature.parameters:
                    bound.arguments["fallback_model"] = None
                args, kwargs = bound.args, bound.kwargs
            if identities is not None:
                from types import SimpleNamespace
                bootstrap = SimpleNamespace(_atlas_delegation_policy=POLICY_VERSION,
                    _atlas_delegation_identities=identities,
                    _atlas_paid_dispatch_lock=threading.Lock())
                # Live authority precedes config, SDK construction, and background work.
                with dispatch_context(bootstrap):
                    validate_dispatch(bootstrap)
                    return function(*args, **kwargs)
            return function(*args, **kwargs)
    return wrapped


_AUTH_HEADERS = frozenset({"authorization", "proxy-authorization", "api-key", "x-api-key",
    "openai-organization", "openai-project", "x-openai-api-key", "x-goog-api-key"})


def reject_auth_headers(headers):
    if any(str(key).lower() in _AUTH_HEADERS for key in (headers or {})):
        raise DelegationDenied("Atlas delegation forbids configured authentication headers")


def scoped_model_client(provider, *, agent=None, headers=None, timeout=60):
    """Isolated process-key SDK client; never user headers, pools, caches or proxies."""
    setup_agent = agent if agent is not None and hasattr(agent, "_atlas_delegation_identities") else current_dispatch_agent()
    if setup_agent is not None:
        validate_dispatch(setup_agent)
    import httpx
    from openai import OpenAI
    reject_auth_headers(headers)
    runtime = scoped_runtime_kwargs({}, provider)
    key, base_url = runtime["api_key"], runtime["base_url"]
    def before_send(request):
        if agent is not None and hasattr(agent, "_atlas_delegation_identities"):
            validate_dispatch(agent)
        if (str(request.url).split("?")[0].startswith(base_url + "/") is not True
                or request.headers.get("authorization") != "Bearer " + key):
            raise DelegationDenied("Atlas delegation model authentication changed")
        if any(request.headers.get(name) for name in _AUTH_HEADERS - {"authorization"}):
            raise DelegationDenied("Atlas delegation model authentication changed")
    return OpenAI(api_key=key, base_url=base_url, max_retries=0,
        organization="", project="", default_headers={}, timeout=timeout,
        http_client=httpx.Client(trust_env=False, follow_redirects=False,
            event_hooks={"request": [before_send]}))


_dispatch_agent = ContextVar("atlas_delegation_dispatch_agent", default=None)


@contextmanager
def dispatch_context(agent):
    token = _dispatch_agent.set(agent if is_scoped(agent) else None)
    try:
        if is_scoped(agent):
            with credential_scope():
                yield
        else:
            yield
    finally:
        _dispatch_agent.reset(token)


def run_dispatch_worker(agent, function):
    with dispatch_context(agent):
        return function()


def validate_auxiliary_dispatch():
    agent = _dispatch_agent.get()
    if agent is not None:
        from agent.atlas_auxiliary_accounting import atlas_auxiliary_enabled
        if not atlas_auxiliary_enabled():
            raise DelegationDenied("Atlas delegation requires the governed auxiliary lane")
        validate_dispatch(agent)


def valid_resource(kind, identifier):
    prefixes = {"chat": "turn_", "query": "qry_", "query_plan": "qpl_"}
    return (kind in prefixes and isinstance(identifier, str) and len(identifier) <= 180
            and re.fullmatch(re.escape(prefixes[kind]) + r"[A-Za-z0-9_.:-]{1,180}", identifier) is not None)


def current_dispatch_agent():
    return _dispatch_agent.get()


def admit_paid_dispatch(agent, *, model=None, auxiliary=False):
    """Reserve one terminal-receipt slot immediately before a paid SDK call."""
    if not is_scoped(agent):
        return
    validate_dispatch(agent)
    limit = agent._atlas_delegation_identities.get("receipt_limit")
    if limit != "256":
        raise DelegationDenied("Atlas delegation receipt capacity is invalid")
    with agent._atlas_paid_dispatch_lock:
        _check_admission_open_locked(agent)
        if agent._atlas_paid_dispatch_count >= int(limit):
            raise DelegationDenied("Atlas delegation receipt capacity is exhausted")
        if getattr(agent, "_atlas_paid_uncertain", False):
            raise DelegationDenied("Atlas delegation has an unsettled paid attempt")
        from agent.atlas_sol_budget import _transport
        call = {"attempt_id": "attempt_" + secrets.token_hex(16),
            "dispatch_status": "uncertain", "usage_available": False,
            "generation_id": "", "model": model or getattr(agent, "model", ""),
            "provider": "openrouter" if _transport() == "openrouter" else "openai",
            "auxiliary": auxiliary}
        if not hasattr(agent, "_atlas_paid_attempts"):
            agent._atlas_paid_attempts = []
        agent._atlas_paid_attempts.append(call)
        if not hasattr(agent, "_atlas_paid_active"):
            agent._atlas_paid_active = set()
        agent._atlas_paid_active.add(call["attempt_id"])
        agent._atlas_paid_dispatch_count += 1
        return call


def validate_web_send(name="web_extract"):
    agent = current_dispatch_agent()
    check_admission_open(agent)
    if agent is not None and not tool_allowed(agent, name):
        raise DelegationDenied("Atlas delegation web reader is no longer authorized")
    check_admission_open(agent)


_paid_attempt = ContextVar("atlas_delegation_paid_attempt", default=None)


@contextmanager
def paid_attempt_context(attempt):
    token = _paid_attempt.set(attempt)
    try:
        yield
    finally:
        _paid_attempt.reset(token)


def observe_paid_event(event, attempt=None, agent=None):
    attempt = attempt if attempt is not None else _paid_attempt.get()
    if attempt is None:
        return
    from agent.atlas_sol_budget import _field
    response = _field(event, "response")
    if response is not None:
        observed = {}
        for name, field in (("generation_id", "id"), ("model", "model"), ("service_tier", "service_tier")):
            value = _field(response, field)
            if isinstance(value, str) and value:
                observed[name] = value
        owner = agent or current_dispatch_agent()
        if owner is not None:
            with owner._atlas_paid_dispatch_lock:
                attempt.update(observed)
        else:
            attempt.update(observed)


def finish_paid_attempt(agent, attempt, fields=None):
    if attempt is None:
        return
    with agent._atlas_paid_dispatch_lock:
        getattr(agent, "_atlas_paid_active", set()).discard(attempt["attempt_id"])
        condition = getattr(agent, "_atlas_paid_condition", None)
        if condition is not None:
            condition.notify_all()
        if fields:
            # Unknown usage must never become fabricated zero-token/zero-cost evidence.
            if fields.get("usage_available") is True:
                attempt.update(fields)
                attempt["dispatch_status"] = "completed"
                return
            for key in ("generation_id", "model", "provider", "service_tier"):
                if fields.get(key):
                    attempt[key] = fields[key]
        agent._atlas_paid_uncertain = True


def validate_mcp_send(agent, server_name, server):
    """Check the actual transport after its RPC lock, including reconnect retries."""
    if not is_scoped(agent):
        return
    from tools import mcp_tool
    with mcp_tool._lock:
        if mcp_tool._servers.get(server_name) is not server or server.session is None:
            raise DelegationDenied("Atlas delegation MCP transport changed")
    if _live_reader_fingerprints() != dict(agent._atlas_delegation_fingerprints):
        raise DelegationDenied("Atlas delegation MCP transport changed")
    validate_dispatch(agent)
    check_admission_open(agent)


def collect_primary_response(agent, response, model, attempt=None):
    if not is_scoped(agent):
        return
    from agent.atlas_sol_budget import _field, openai_usage_fields, _transport
    usage = _field(response, "usage")
    # The ordinary normalizer clamps/coerces values for compatibility. Scoped
    # evidence must prove actual counts before that conversion can erase doubt.
    raw_counts = (_field(usage, "input_tokens"), _field(usage, "output_tokens"))
    known_counts = all(type(value) is int and value >= 0 for value in raw_counts)
    fields = openai_usage_fields(response) if known_counts else {"usage_available": False}
    call = {
        **fields,
        "generation_id": str(_field(response, "id", "") or ""),
        "model": str(_field(response, "model", model) or model),
        "provider": "openrouter" if _transport() == "openrouter" else "openai",
        "route_request_id": getattr(agent, "_atlas_route_request_id", None),
        "service_tier": _field(response, "service_tier"),
    }
    if attempt is not None:
        finish_paid_attempt(agent, attempt, call)
    else:
        with agent._atlas_paid_dispatch_lock:
            agent._atlas_primary_usage_calls.append(call)
            if fields.get("usage_available") is not True:
                agent._atlas_paid_uncertain = True


def terminal_usage_calls(agent):
    """Merge loop settlement into every completed dispatch, without reauthorizing."""
    recorded = list(getattr(agent, "session_usage_calls", []) or [])
    with agent._atlas_paid_dispatch_lock:
        agent._atlas_admission_closed = True
        condition = getattr(agent, "_atlas_paid_condition", None)
        if condition is None:
            condition = agent._atlas_paid_condition = threading.Condition(agent._atlas_paid_dispatch_lock)
        deadline = time.monotonic() + 3.0
        while getattr(agent, "_atlas_paid_active", set()):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            condition.wait(timeout=remaining)
        attempts = [dict(row) for row in getattr(agent, "_atlas_paid_attempts", []) or []]
        completed = attempts if attempts else [dict(row) for row in getattr(agent, "_atlas_primary_usage_calls", []) or []]
    if not completed:
        completed = recorded
    else:
        for call in completed:
            match = next((row for row in recorded if call.get("generation_id") and row.get("generation_id") == call["generation_id"]), None)
            if match and call.get("usage_available") is True:
                call.update(match)
    calls = completed if attempts else completed + list(getattr(agent, "_atlas_auxiliary_usage_calls", []) or [])
    context = agent._atlas_delegation_identities
    actor = context["actor_user_id"]
    for call in calls:
        call.update({key: context[key] for key in ("actor_user_id", "subject_user_id", "view_as_session_id", "resource_type", "resource_id")})
        call["user_id"] = actor
        call["provider_user_hash"] = "atlas-user-" + hashlib.sha256(actor.encode()).hexdigest()
        call["route_request_id"] = getattr(agent, "_atlas_route_request_id", None)
    return calls
