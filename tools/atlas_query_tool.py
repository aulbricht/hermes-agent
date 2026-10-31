"""One-operation Queries tool through Atlas's fixed, capability-bound callback.

Contract source: acre-atlas query_tool_capability.py (/api/v1/internal/query-tool).
Operations: catalog lookup, plan validation, execution of the last plan Atlas itself
validated. The model supplies no URL, SQL, plan to execute or identity; Atlas holds the
owner, question and ceilings. This gate only bounds shape and keeps the bearer token out
of requests and results.
"""

import json
import re
from contextvars import ContextVar

import httpx

from tools.registry import registry


_capability: ContextVar[str] = ContextVar("atlas_query_capability", default="")
_CALLBACK = "http://127.0.0.1:8243/api/v1/internal/query-tool"
_MAX_REQUEST_BYTES = 60000
_MAX_RESPONSE_BYTES = 32768
_MAX_ENTITIES = 8
_UNAVAILABLE = '{"error":"Query operation unavailable"}'
# Atlas answers 429 at its ceilings and 409 for execute without a validated plan.
_HTTP_ERRORS = {
    429: '{"error":"Query tool limit reached"}',
    409: '{"error":"Validate a plan before executing it"}',
}
_TOKEN = re.compile(r"(?:atqry_|atown_|atimp_)[A-Za-z0-9_-]{32,128}")

_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {"type": "string", "enum": ["catalog", "validate", "execute"]},
        "arguments": {"type": "object"},
    },
    "required": ["operation", "arguments"],
    "additionalProperties": False,
}


def bind_query_capability(value: str):
    # Invalid callers cannot enable the definition or put arbitrary text in headers.
    value = value if isinstance(value, str) and re.fullmatch(r"atqry_[A-Za-z0-9_-]{32,128}", value) else ""
    return _capability.set(value)


def clear_query_capability(token) -> None:
    _capability.reset(token)


def query_capability_available() -> bool:
    return bool(_capability.get())


def _clean(value, depth=0):
    """True when the JSON value is plain data holding no capability token."""
    if depth > 24:
        return False
    if isinstance(value, str):
        return _TOKEN.search(value) is None
    if isinstance(value, dict):
        return all(isinstance(key, str) and _TOKEN.search(key) is None and _clean(item, depth + 1)
                   for key, item in value.items())
    if isinstance(value, list):
        return all(_clean(item, depth + 1) for item in value)
    return value is None or type(value) in {int, float, bool}


def _arguments_valid(operation, arguments):
    if operation == "catalog":
        entities = arguments.get("entities")
        return (set(arguments) <= {"entities"} and (entities is None or (
            type(entities) is list and len(entities) <= _MAX_ENTITIES
            and all(type(name) is str and 0 < len(name) <= 40 for name in entities))))
    if operation == "validate":
        return set(arguments) == {"reply"} and type(arguments["reply"]) is dict
    return operation == "execute" and not arguments


def _handle_atlas_query(args: dict, **_kwargs) -> str:
    token = _capability.get()
    if (not token or type(args) is not dict or set(args) != {"operation", "arguments"}
            or args["operation"] not in {"catalog", "validate", "execute"}
            or type(args["arguments"]) is not dict
            or not _arguments_valid(args["operation"], args["arguments"])
            or not _clean(args["arguments"])):
        return _UNAVAILABLE
    try:
        encoded = json.dumps({"operation": args["operation"], "arguments": args["arguments"]},
                             ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BYTES:
            return _UNAVAILABLE
        with httpx.Client(timeout=httpx.Timeout(60, connect=2), follow_redirects=False,
                          trust_env=False) as client:
            with client.stream("POST", _CALLBACK, headers={
                "Authorization": "Bearer " + token, "Content-Type": "application/json",
            }, content=encoded) as response:
                if response.status_code in _HTTP_ERRORS:
                    return _HTTP_ERRORS[response.status_code]
                if (response.status_code != 200
                        or response.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json"):
                    return _UNAVAILABLE
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > _MAX_RESPONSE_BYTES:
                        return _UNAVAILABLE
                    chunks.append(chunk)
        value = json.loads(b"".join(chunks).decode("utf-8"))
        # Check decoded strings too, so JSON unicode escapes cannot echo a token.
        result = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if type(value) is not dict or _TOKEN.search(result):
            return _UNAVAILABLE
        return result
    except (httpx.HTTPError, ValueError, TypeError, UnicodeError, RecursionError):
        # Exceptions may contain Authorization headers. Never log or forward them.
        return _UNAVAILABLE


registry.register(
    name="atlas_query", toolset="atlas_query",
    schema={"name": "atlas_query",
            "description": ("Build one read-only FileMaker query with Atlas. operation=catalog: arguments {} returns "
                            "the entity index and the exact reply_schema; {\"entities\":[names]} returns those "
                            "entities and links. operation=validate: arguments {\"reply\":<reply_schema object>}; "
                            "returns ready, needs_clarification, invalid (repair once) or unavailable. "
                            "operation=execute: arguments {} runs only the plan Atlas last validated as ready. "
                            "Never takes SQL, URLs or identifiers."),
            "parameters": _SCHEMA},
    handler=_handle_atlas_query, max_result_size_chars=_MAX_RESPONSE_BYTES,
)
