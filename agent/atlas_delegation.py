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
from types import MappingProxyType
import urllib.request
from urllib.parse import urlsplit

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
    from model_tools import get_tool_definitions
    from tools.registry import registry
    from tools import mcp_tool
    schemas = get_tool_definitions(enabled_toolsets=TOOLSETS, quiet_mode=True, skip_tool_search_assembly=True) or []
    selected = {tool["function"]["name"]: tool for tool in schemas if tool.get("function", {}).get("name") in ALLOWED_TOOLS}
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
    resolve_policy()
    _read_dispatch_key()
    return {"policy_version": POLICY_VERSION, "executor_enforced": True, "allowed_tools": sorted(ALLOWED_TOOLS)}


def bind_policy(agent, identities):
    if getattr(agent, "provider", None) == "moa":
        raise DelegationDenied("Atlas delegation does not permit model delegation")
    schemas, entries, fingerprints = resolve_policy()
    agent._atlas_delegation_policy = POLICY_VERSION
    agent._atlas_delegation_allowed_tools = ALLOWED_TOOLS
    agent._atlas_delegation_entries = MappingProxyType(entries)
    agent._atlas_delegation_fingerprints = MappingProxyType(fingerprints)
    agent._atlas_delegation_identities = MappingProxyType(dict(identities))
    agent.tools = schemas
    agent.valid_tool_names = ALLOWED_TOOLS
    agent._skip_mcp_refresh = True
    agent._memory_store = agent._memory_manager = None
    agent._memory_enabled = agent._user_profile_enabled = False
    agent._memory_nudge_interval = agent._skill_nudge_interval = 0
    agent.context_compressor._atlas_dispatch_validator = lambda: validate_dispatch(agent)


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
    context = agent._atlas_delegation_identities
    envelope = {**context, "method": "GET", "path": _VALIDATION_PATH,
        "body_sha256": hashlib.sha256(b"").hexdigest(), "issued_at": int(time.time()),
        "nonce": secrets.token_hex(16), "resource_type": "", "resource_id": ""}
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
        return True
    except Exception:
        return False


def dispatch_tool(agent, name, args, task_id):
    """Execute the captured handler, never a replacement registry entry."""
    if not tool_allowed(agent, name):
        return json.dumps({"error": "Tool is unavailable in this Atlas View As session"})
    from tools.registry import registry
    handler, _, asynchronous = agent._atlas_delegation_entries[name]
    result = handler(args, task_id=task_id)
    if asynchronous:
        from model_tools import _run_async
        result = _run_async(result)
    return registry._normalize_handler_result(name, result)


_dispatch_agent = ContextVar("atlas_delegation_dispatch_agent", default=None)


@contextmanager
def dispatch_context(agent):
    token = _dispatch_agent.set(agent if is_scoped(agent) else None)
    try:
        yield
    finally:
        _dispatch_agent.reset(token)


def validate_auxiliary_dispatch():
    agent = _dispatch_agent.get()
    if agent is not None:
        from agent.atlas_auxiliary_accounting import atlas_auxiliary_enabled
        if not atlas_auxiliary_enabled():
            raise DelegationDenied("Atlas delegation requires the governed auxiliary lane")
        validate_dispatch(agent)
