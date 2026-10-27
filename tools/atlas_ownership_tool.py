"""One-turn ownership previews through Atlas's fixed, capability-bound callback.

Schema source: acre-atlas 0427d925, ownership_work._proposal/_review_changes.
Business references are untrusted; only Atlas can resolve them against bound work.
"""

import json
import re
from contextvars import ContextVar

import httpx

from tools.registry import registry


_capability: ContextVar[str] = ContextVar("atlas_ownership_capability", default="")
_CALLBACK = "http://127.0.0.1:8243/api/v1/internal/ownership-tool"
_PROTOCOL = "acre.ownership-tool.v1"
_MAX_REQUEST_BYTES = 131072
_MAX_RESPONSE_BYTES = 32768
_UNAVAILABLE = '{"error":"Ownership operation unavailable"}'
_ERROR_CODES = frozenset({
    "OWNERSHIP_OPERATION_INVALID", "OWNERSHIP_TURN_REQUIRED", "OWNERSHIP_UNAVAILABLE",
    "OWNERSHIP_STATE_CHANGED", "OWNERSHIP_SHAPE_INVALID", "OWNERSHIP_EVIDENCE_MISMATCH",
    "OWNERSHIP_DUPLICATE_ASSOCIATION", "OWNERSHIP_RANGE_INVALID", "OWNERSHIP_TOO_LARGE",
    "OWNERSHIP_CORRECTION_REQUIRED", "OWNERSHIP_REVISION_REQUIRED", "OWNERSHIP_INTERNAL_ERROR",
})


def _object(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


def _text(limit=2000):
    return {"type": "string", "minLength": 1, "maxLength": limit}


def _array(items, limit):
    return {"type": "array", "items": items, "maxItems": limit}


_ADDRESS_SCHEMA = _object({
    "entityType": {"type": "string", "enum": ["Address"]},
    **{key: _text(240) for key in (
        "addressStreet1", "addressStreet2", "addressCity", "addressState", "addressZip")},
}, ["entityType"])
_CHANGE_SCHEMA = _object({
    "id": _text(160),
    "source_ref": _text(160),
    "association_index": {"type": "integer", "minimum": 0},
    "kind": {"type": "string", "enum": ["name", "email", "address"]},
    "intent": {"type": "string", "enum": ["create", "reuse", "update"]},
    "proposed_value": {"anyOf": [_text(), {"type": "null"}, _ADDRESS_SCHEMA]},
    "possible_cleaned_value": {"anyOf": [_text(254), {"type": "null"}]},
    "depends_on": _array(_text(160), 100),
    "uncertainty": _array(_text(), 10),
    "rationale": _text(),
})
_PROPOSAL_SCHEMA = _object({
    "property_id": _text(160),
    "evidence_hash": _text(160),
    "associations": _array(_object({
        "source_ref": _text(160),
        "contact_id": {"anyOf": [_text(160), {"type": "null"}]},
        "display_values": _object({"display_name": _text(240)}),
        "rationale": _text(),
        "role": {"type": "string", "enum": ["Property Manager"]},
    }), 100),
    "unresolved_questions": _array(_text(), 10),
    "changes": _array(_CHANGE_SCHEMA, 100),
}, ["property_id", "evidence_hash", "associations", "unresolved_questions"])
_READ_SCHEMA = _object({
    "operation": {"type": "string", "enum": ["evidence", "catalog"]},
    "arguments": _object({
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
    }, []),
})
_PROPOSAL_TOOL_SCHEMA = _object({
    "operation": {"type": "string", "enum": ["propose", "revise"]},
    "expected_version": {"type": "integer"},
    "proposal": _PROPOSAL_SCHEMA,
    "base_revision": {"type": "integer", "minimum": 1},
    "correction_text": _text(),
}, ["operation", "expected_version", "proposal"])


def bind_ownership_capability(value: str):
    # Invalid callers cannot enable definitions or put arbitrary text in headers.
    value = value if isinstance(value, str) and re.fullmatch(r"atown_[A-Za-z0-9_-]{32,128}", value) else ""
    return _capability.set(value)


def clear_ownership_capability(token) -> None:
    _capability.reset(token)


def ownership_capability_available() -> bool:
    return bool(_capability.get())


def _matches(value, schema):
    """Validate the closed schema subset above, including every nested object.

    No coercion, unknown fields or model-supplied schema. Atlas additionally
    checks evidence, dependencies and state; this gate conveys no authority.
    """
    if "anyOf" in schema:
        return any(_matches(value, branch) for branch in schema["anyOf"])
    kind = schema["type"]
    if type(value) is not {"object": dict, "array": list, "string": str,
                           "integer": int, "null": type(None)}[kind]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if kind == "object":
        return (not set(value) - set(schema["properties"])
                and set(schema["required"]).issubset(value)
                and all(_matches(item, schema["properties"][key]) for key, item in value.items()))
    if kind == "array":
        return len(value) <= schema["maxItems"] and all(_matches(item, schema["items"]) for item in value)
    if kind == "string":
        return (schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", _MAX_REQUEST_BYTES)
                and (not schema.get("minLength") or bool(value.strip()))
                and re.search(r"(?:atown_|atimp_)[A-Za-z0-9_-]{32,128}", value) is None)
    if kind == "integer":
        return schema.get("minimum", value) <= value <= schema.get("maximum", value)
    return True


def _response_valid(value, operation):
    if type(value) is not dict or value.get("protocol") != _PROTOCOL:
        return False
    if value.get("ok") is True:
        return (set(value) == {"protocol", "ok", "operation", "result"}
                and value["operation"] == operation and type(value["result"]) is dict)
    if value.get("ok") is False:
        error = value.get("error")
        return (set(value) == {"protocol", "ok", "error"} and type(error) is dict
                and set(error) == {"code"} and type(error["code"]) is str
                and error["code"] in _ERROR_CODES)
    return False


def _forward_operation(operation, arguments):
    token = _capability.get()
    if not token:
        return _UNAVAILABLE
    try:
        encoded = json.dumps({"protocol": _PROTOCOL, "operation": operation, "arguments": arguments},
                             ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BYTES:
            return _UNAVAILABLE
        with httpx.Client(timeout=httpx.Timeout(30, connect=2), follow_redirects=False,
                          trust_env=False) as client:
            with client.stream("POST", _CALLBACK, headers={
                "Authorization": "Bearer " + token, "Content-Type": "application/json",
            }, content=encoded) as response:
                if (response.status_code != 200
                        or response.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json"):
                    return _UNAVAILABLE
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > _MAX_RESPONSE_BYTES:
                        return _UNAVAILABLE
                    chunks.append(chunk)
        raw = b"".join(chunks).decode("utf-8")
        value = json.loads(raw)
        # Check decoded strings too, so JSON unicode escapes cannot echo a token.
        result = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if not _response_valid(value, operation) or re.search(r"(?:atown_|atimp_)[A-Za-z0-9_-]{32,128}", result):
            return _UNAVAILABLE
        return result
    except (httpx.HTTPError, ValueError, TypeError, UnicodeError, RecursionError):
        # Exceptions may contain Authorization headers. Never log or forward them.
        return _UNAVAILABLE


def _handle_atlas_ownership(args: dict, **_kwargs) -> str:
    if not _matches(args, _READ_SCHEMA) or (args["operation"] == "catalog" and args["arguments"]):
        return _UNAVAILABLE
    return _forward_operation(args["operation"], args["arguments"])


def _handle_atlas_ownership_proposal(args: dict, **_kwargs) -> str:
    if not _matches(args, _PROPOSAL_TOOL_SCHEMA):
        return _UNAVAILABLE
    keys = {"operation", "expected_version", "proposal"}
    if args["operation"] == "revise":
        keys |= {"base_revision", "correction_text"}
    if set(args) != keys:
        return _UNAVAILABLE
    return _forward_operation(args["operation"], {key: value for key, value in args.items() if key != "operation"})


registry.register(
    name="atlas_ownership", toolset="atlas_ownership",
    schema={"name": "atlas_ownership", "description": "Read evidence or catalog guidance for the current authorized ownership work. Catalog takes empty arguments. No Approval or execution authority.",
            "parameters": _READ_SCHEMA},
    handler=_handle_atlas_ownership, max_result_size_chars=_MAX_RESPONSE_BYTES,
)
registry.register(
    name="atlas_ownership_proposal", toolset="atlas_ownership",
    schema={"name": "atlas_ownership_proposal", "description": "Submit a held ownership proposal or immutable correction revision. Revise requires base_revision and correction_text; propose omits them. Business references must match bound evidence. Never approves or executes.",
            "parameters": _PROPOSAL_TOOL_SCHEMA},
    handler=_handle_atlas_ownership_proposal, max_result_size_chars=_MAX_RESPONSE_BYTES,
)
