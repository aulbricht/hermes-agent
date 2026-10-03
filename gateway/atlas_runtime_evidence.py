"""Atlas owner metadata only: no clients, config reads, inference or telemetry.

Code-object fingerprints are deliberately NOT source-byte attestations. Request
snapshots describe the last dispatch attempt, not the effective next turn.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import types

PROTOCOL = "atlas.hermes-runtime-evidence.v1"
MODULES = ("gateway.platforms.api_server", "gateway.run", "hermes_cli.runtime_provider",
           "agent.atlas_sol_budget", "gateway.atlas_runtime_evidence")
MODELS = {"openai/gpt-6-luna", "openai/gpt-6.1-sol", "gpt-6-luna", "gpt-6.1-sol"}
EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}
TOOLSETS = {"web", "acre-filemaker", "atlas_vault"}
_lock = threading.Lock()
_last_dispatch = {}


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _choice(value, allowed):
    return value if type(value) is str and value in allowed else None


def _integer(value):
    return value if type(value) is int and 0 < value <= 1000000 else None


def _tools(value):
    if type(value) not in (list, tuple, set) or any(type(x) is not str or x not in TOOLSETS for x in value):
        return None
    return sorted(set(value))


def _profile(agent):
    # Read data attributes only, never a client's repr/property or raw kwargs.
    fields = vars(agent)
    reasoning = fields.get("reasoning_config")
    effort = reasoning.get("effort") if type(reasoning) is dict else None
    return {
        "model": _choice(fields.get("model"), MODELS),
        "provider": _choice(fields.get("provider"), {"openrouter", "openai"}),
        "base_url": _choice(fields.get("base_url"), {"https://openrouter.ai/api/v1", "https://api.openai.com/v1"}),
        "reasoning_effort": _choice(effort, EFFORTS),
        "max_tokens": _integer(fields.get("max_tokens")),
        "context_length": _integer(fields.get("_config_context_length")),
        "max_iterations": _integer(fields.get("max_iterations")),
        "enabled_toolsets": _tools(fields.get("enabled_toolsets")),
    }


def request_policy(payload):
    """Project only actual outbound routing fields; unknowns remain null."""
    extra = payload.get("extra_body")
    provider = extra.get("provider") if type(extra) is dict else None
    provider = provider if type(provider) is dict else {}
    reasoning = payload.get("reasoning")
    effort = reasoning.get("effort") if type(reasoning) is dict else payload.get("reasoning_effort")
    return {
        "model": _choice(payload.get("model"), MODELS),
        "reasoning_effort": _choice(effort, EFFORTS),
        "max_output_tokens": _integer(payload.get("max_output_tokens")),
        "provider_order": ["openai"] if provider.get("order") == ["openai"] else None,
        "provider_only": ["openai"] if provider.get("only") == ["openai"] else None,
        "provider_allow_fallbacks": provider.get("allow_fallbacks") if type(provider.get("allow_fallbacks")) is bool else None,
        "provider_require_parameters": provider.get("require_parameters") if type(provider.get("require_parameters")) is bool else None,
        "service_tier": _choice(payload.get("service_tier"), {"auto", "default", "flex", "priority"}),
    }


def record_dispatch(agent, payload):
    """Bounded, best-effort observation immediately before existing dispatch.

    Failure must not change admission, retries, accounting or provider behavior.
    Store no prompts, tokens, keys, session identifiers or provider responses.
    """
    try:
        from hermes_constants import get_hermes_home
        profile, policy = _profile(agent), request_policy(payload)
        value = {"observed_at_utc": _now(), "effective_agent_profile": profile,
                 "effective_agent_profile_sha256": digest(profile),
                 "request_policy": policy, "request_policy_sha256": digest(policy),
                 "admission_code": loaded_code(sys.modules.get("agent.atlas_sol_budget"))}
        with _lock:
            # Atlas uses one fixed profile per process. Bound retention even if
            # this helper is accidentally used in a multiplexed process.
            _last_dispatch.clear()
            _last_dispatch[str(get_hermes_home())] = value
    except Exception:
        pass


def loaded_code(module):
    """Fingerprint currently installed Python functions/methods, not disk."""
    if module is None:
        return {"state": "not_loaded", "sha256": None}
    codes = {}
    def add(name, value):
        if isinstance(value, (staticmethod, classmethod)):
            value = value.__func__
        if isinstance(value, types.FunctionType):
            codes[name] = digest(_code_value(value.__code__))
        elif isinstance(value, property):
            for part in ("fget", "fset", "fdel"):
                fn = getattr(value, part)
                if fn is not None:
                    add(name + "." + part, fn)
    for name, value in list(vars(module).items()):
        if isinstance(value, types.FunctionType) and value.__module__ == module.__name__:
            add(name, value)
        elif isinstance(value, type) and value.__module__ == module.__name__:
            for member, fn in list(vars(value).items()):
                add(name + "." + member, fn)
    return {"state": "loaded_python_code" if codes else "no_python_code",
            "sha256": digest(codes) if codes else None, "code_object_count": len(codes)}


def _code_value(code):
    # marshal's reference flags can change with transient reference counts.
    # Serialize immutable public code data canonically instead, including nested
    # code constants. Never return or persist these raw constants to the caller.
    def constant(value):
        if value is None or type(value) in (bool, int, str):
            return [type(value).__name__, value]
        if value is Ellipsis:
            return ["ellipsis"]
        if type(value) is bytes:
            return ["bytes", value.hex()]
        if type(value) is float:
            return ["float", value.hex()]
        if type(value) is complex:
            return ["complex", value.real.hex(), value.imag.hex()]
        if type(value) is types.CodeType:
            return ["code", _code_value(value)]
        if type(value) is tuple:
            return ["tuple", [constant(x) for x in value]]
        if type(value) is frozenset:
            return ["frozenset", sorted((constant(x) for x in value), key=lambda x: json.dumps(x, sort_keys=True))]
        raise TypeError("unsupported_code_constant")
    return {"name": code.co_name, "qualname": code.co_qualname, "filename": code.co_filename,
            "firstlineno": code.co_firstlineno, "argcount": code.co_argcount,
            "posonlyargcount": code.co_posonlyargcount, "kwonlyargcount": code.co_kwonlyargcount,
            "nlocals": code.co_nlocals, "stacksize": code.co_stacksize, "flags": code.co_flags,
            "code": code.co_code.hex(), "linetable": code.co_linetable.hex(),
            "exceptiontable": code.co_exceptiontable.hex(), "names": code.co_names,
            "varnames": code.co_varnames, "freevars": code.co_freevars, "cellvars": code.co_cellvars,
            "constants": [constant(x) for x in code.co_consts]}


def process_identity():
    result = {"pid": os.getpid(), "start_ticks": None, "boot_id": None,
              "state": "start_identity_unavailable"}
    try:
        # Linux proc stat's command field can contain spaces and parentheses.
        fields = Path("/proc/self/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        ticks = int(fields[19])  # field 22, tail starts at field 3
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        import uuid
        if ticks > 0 and str(uuid.UUID(boot)) == boot:
            result.update(start_ticks=ticks, boot_id=boot, state="linux_proc_identity")
    except (OSError, ValueError, IndexError):
        pass
    return result


def snapshot(adapter, nonce):
    from hermes_constants import get_hermes_home
    started = _now()
    # These routes are cached in the actual adapter; no fresh config loader.
    routes = {}
    for alias in ("atlas-luna", "atlas-sol"):
        raw = adapter._model_routes.get(alias)
        if type(raw) is dict:
            bound = raw.get("max_iterations")
            if type(bound) is str and bound.isascii() and bound.isdecimal() and len(bound) <= 6:
                bound = int(bound)
            routes[alias] = {"model": _choice(raw.get("model"), MODELS),
                             "provider": _choice(raw.get("provider"), {"openrouter", "openai"}),
                             "reasoning_effort": _choice(raw.get("reasoning_effort"), EFFORTS),
                             "max_iterations": _integer(bound),
                             "credential_override_present": bool(raw.get("api_key")),
                             "base_url": _choice(raw.get("base_url"), {"https://openrouter.ai/api/v1", "https://api.openai.com/v1"})}
    with _lock:
        last = copy.deepcopy(_last_dispatch.get(str(get_hermes_home())))
    result = {
        "protocol": PROTOCOL, "nonce": nonce, "collection_started_at_utc": started,
        "process": process_identity(),
        "python_cache_tag": sys.implementation.cache_tag,
        "loaded_code": {name: loaded_code(sys.modules.get(name)) for name in MODULES},
        "adapter_routes": routes, "adapter_routes_sha256": digest(routes),
        "last_dispatch": last, "last_dispatch_state": "observed_dispatch_attempt" if last else "not_observed",
        "loaded_source_sha256": None, "loaded_profile_sha256": None,
        "external_byok_state": "not_observed",
    }
    result["observed_at_utc"] = _now()
    return result
