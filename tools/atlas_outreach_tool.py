"""Save-only neighbor outreach draft tool through Atlas's fixed loopback callback.

Contract source: SNO-3848 / SNO-3851 revision 5 / SNO-3974 (/api/v1/internal/outreach-draft-tool).
Operation: save (persists a local review-only outreach draft; never sends mail, writes CRM,
or creates an Atlas Approval). The model supplies no URL, run ID, session ID, or owner user ID;
Atlas derives run, user, session, and turn from the durable capability context.
"""

import json
import re
from contextvars import ContextVar

import httpx

from tools.registry import registry


_capability: ContextVar[str] = ContextVar("atlas_outreach_capability", default="")
_CALLBACK = "http://127.0.0.1:8243/api/v1/internal/outreach-draft-tool"
_PROTOCOL = "atlas-outreach-drafts-v1"
_MAX_PAYLOAD_BYTES = 65536
_MAX_REQUEST_BYTES = 131072
_MAX_RESPONSE_BYTES = 65536
_UNAVAILABLE = '{"error":"Outreach operation unavailable"}'
_CAPABILITY_RE = re.compile(r"atout_[A-Za-z0-9_-]{32,128}")
_TOKEN = re.compile(r"(?:atqry_|atown_|atimp_|atmail_|atout_)[A-Za-z0-9_-]{32,128}")

_TEXT_KEYS = (
    "subject_property_id",
    "owner_id",
    "email_subject",
    "email_body",
    "call_script",
)
_PAYLOAD_KEYS = frozenset((*_TEXT_KEYS, "facts"))
_FACT_KEYS = frozenset(("source_ref", "path", "value"))

_FACT_SCHEMA = {
    "type": "object",
    "properties": {
        "source_ref": {"type": "string", "minLength": 1},
        "path": {"type": "string", "minLength": 1},
        "value": {"type": "string", "minLength": 1},
    },
    "required": ["source_ref", "path", "value"],
    "additionalProperties": False,
}

_SCHEMA = {
    "type": "object",
    "properties": {
        "subject_property_id": {"type": "string", "minLength": 1},
        "owner_id": {"type": "string", "minLength": 1},
        "email_subject": {"type": "string", "minLength": 1},
        "email_body": {"type": "string", "minLength": 1},
        "call_script": {"type": "string", "minLength": 1},
        "facts": {
            "type": "array",
            "items": _FACT_SCHEMA,
        },
    },
    "required": [
        "subject_property_id",
        "owner_id",
        "email_subject",
        "email_body",
        "call_script",
        "facts",
    ],
    "additionalProperties": False,
}


def bind_outreach_capability(value: str):
    """Bind a syntactically valid per-turn outreach capability in the current context."""
    value = value if isinstance(value, str) and _CAPABILITY_RE.fullmatch(value) else ""
    return _capability.set(value)


def clear_outreach_capability(token) -> None:
    """Restore the outreach capability context variable to its prior state."""
    _capability.reset(token)


def outreach_capability_available() -> bool:
    """Return True when a valid outreach capability is bound in the current context."""
    return bool(_capability.get())


def _clean(value, depth: int = 0) -> bool:
    """True when the JSON value is plain data holding no capability token."""
    if depth > 24:
        return False
    if isinstance(value, str):
        return _TOKEN.search(value) is None
    if isinstance(value, dict):
        return all(
            isinstance(key, str)
            and _TOKEN.search(key) is None
            and _clean(item, depth + 1)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return all(_clean(item, depth + 1) for item in value)
    return value is None or type(value) in {int, float, bool}


def _arguments_valid(args) -> bool:
    if type(args) is not dict or set(args) != _PAYLOAD_KEYS:
        return False
    if not all(type(args[key]) is str and len(args[key]) > 0 for key in _TEXT_KEYS):
        return False
    facts = args["facts"]
    if type(facts) is not list:
        return False
    for fact in facts:
        if type(fact) is not dict or set(fact) != _FACT_KEYS:
            return False
        if not all(type(fact[key]) is str and len(fact[key]) > 0 for key in _FACT_KEYS):
            return False
    return True


def _response_valid(value) -> bool:
    if type(value) is not dict or set(value) != {"protocol", "ok", "result"}:
        return False
    if value.get("protocol") != _PROTOCOL or value.get("ok") is not True:
        return False
    result = value.get("result")
    return (
        type(result) is dict
        and set(result) == {"id", "run_id"}
        and type(result.get("id")) is str
        and bool(result["id"].strip())
        and type(result.get("run_id")) is str
        and bool(result["run_id"].strip())
    )


def _handle_atlas_outreach_save(args: dict, **_kwargs) -> str:
    token = _capability.get()
    if not token or not _arguments_valid(args) or not _clean(args):
        return _UNAVAILABLE
    try:
        payload_encoded = json.dumps(
            args,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(payload_encoded) > _MAX_PAYLOAD_BYTES:
            return _UNAVAILABLE
        encoded = json.dumps(
            {
                "protocol": _PROTOCOL,
                "operation": "save",
                "arguments": args,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BYTES:
            return _UNAVAILABLE
        with httpx.Client(
            timeout=httpx.Timeout(30, connect=2),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream(
                "POST",
                _CALLBACK,
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
                content=encoded,
            ) as response:
                if (
                    response.status_code != 200
                    or response.headers.get("content-type", "")
                    .split(";")[0]
                    .strip()
                    .lower()
                    != "application/json"
                ):
                    return _UNAVAILABLE
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > _MAX_RESPONSE_BYTES:
                        return _UNAVAILABLE
                    chunks.append(chunk)
        value = json.loads(b"".join(chunks).decode("utf-8"))
        result = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        if not _response_valid(value) or _TOKEN.search(result):
            return _UNAVAILABLE
        return result
    except (httpx.HTTPError, ValueError, TypeError, UnicodeError, RecursionError):
        return _UNAVAILABLE


registry.register(
    name="atlas_outreach_save",
    toolset="atlas_outreach",
    schema={
        "name": "atlas_outreach_save",
        "description": (
            "Save one review-only neighbor outreach draft (email subject/body, call script, "
            "and supporting facts) to the bound Atlas outreach run report. Never sends email, "
            "writes CRM records, or creates an Atlas Approval. Never takes URLs, run IDs, "
            "session IDs, or user IDs."
        ),
        "parameters": _SCHEMA,
    },
    handler=_handle_atlas_outreach_save,
    max_result_size_chars=_MAX_RESPONSE_BYTES,
)
