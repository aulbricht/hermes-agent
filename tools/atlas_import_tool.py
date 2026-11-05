"""One-turn, Atlas-owned import operations through a fixed local callback."""

import json
from contextvars import ContextVar

import httpx

from tools.registry import registry


_capability: ContextVar[str] = ContextVar("atlas_import_capability", default="")
_CALLBACK = "http://127.0.0.1:8243/api/v1/internal/import-tool"
_READ_OPERATIONS = {"evidence", "catalog", "lookup"}
_READ_ARGUMENT_KEYS = {
    "evidence": {"offset", "limit"},
    "catalog": {"logical_ids"},
    "lookup": {"property_query", "property_id", "tenant_names"},
}
_PROPOSAL_OPERATIONS = {"change_set"}
_MAX_REQUEST_BYTES = 128 * 1024
_MAX_RESPONSE_BYTES = 32_768

_READ_ARGUMENTS_SCHEMA = {
    "type": "object",
    "description": "Choose only the argument shape matching operation; no fields from other read operations.",
    "anyOf": [
        {"type": "object", "description": "evidence: cited lines, at most 50 per call",
         "properties": {"offset": {"type": "integer", "minimum": 0},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
         "additionalProperties": False},
        {"type": "object", "description": "catalog: shared Acre meanings",
         "properties": {"logical_ids": {"type": "array", "items": {"type": "string"}}},
         "additionalProperties": False},
        {"type": "object", "description": "lookup: a Property query or verified Property ID",
         "properties": {"property_query": {"type": "string"},
                        "property_id": {"type": "string", "description": "Exact PRO-... ID returned by a verified Property lookup."},
                        "tenant_names": {"type": "array", "items": {"type": "string"}}},
         "additionalProperties": False},
    ],
}
_READ_ARGUMENT_FIELDS = set().union(*_READ_ARGUMENT_KEYS.values())


def bind_import_capability(value: str):
    return _capability.set(value)


def clear_import_capability(token) -> None:
    _capability.reset(token)


def import_capability_available() -> bool:
    return bool(_capability.get())


def _read_arguments(operation: str, arguments: dict) -> tuple[dict | None, str | None]:
    allowed = _READ_ARGUMENT_KEYS[operation]
    for key, value in arguments.items():
        if key not in _READ_ARGUMENT_FIELDS:
            return None, f"arguments.{key} is not a supported read field"
        if key not in allowed and value not in ("", [], None) and not (key == "offset" and value == 0):
            return None, f"arguments.{key} is not valid for {operation}"
    selected = {key: value for key, value in arguments.items()
                if key in allowed and value not in ("", [], None)}
    if operation == "evidence":
        if "limit" in selected and (type(selected["limit"]) is not int or not 1 <= selected["limit"] <= 50):
            return None, "arguments.limit must be an integer from 1 to 50"
        if "offset" in selected and (type(selected["offset"]) is not int or selected["offset"] < 0):
            return None, "arguments.offset must be a nonnegative integer"
    return selected, None


def _forward_operation(operation: str, arguments: dict) -> str:
    token = _capability.get()
    if not token:
        return json.dumps({"error": "Import capability unavailable"})
    body = {"operation": operation, "arguments": arguments}
    try:
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return json.dumps({"error": "Invalid import operation"})
    if len(encoded) > _MAX_REQUEST_BYTES:
        return json.dumps({"error": "Import request exceeds limit"})
    try:
        # Atlas can perform two sequential 35-second read-only FileMaker lookups
        # while validating a proposal; keep this bounded above that budget.
        with httpx.Client(timeout=httpx.Timeout(90, connect=2), follow_redirects=False,
                          trust_env=False) as client:
            with client.stream("POST", _CALLBACK, headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            }, content=encoded) as response:
                if response.status_code != 200:
                    if response.status_code == 422:
                        # The callback is fixed loopback; expose only short printable
                        # validation detail, never auth or transport errors.
                        chunks = []
                        size = 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > 1024:
                                break
                            chunks.append(chunk)
                        if size <= 1024:
                            try:
                                detail = json.loads(b"".join(chunks)).get("detail")
                                if (isinstance(detail, str) and 0 < len(detail) <= 160
                                        and detail.isprintable()):
                                    return json.dumps({"error": detail})
                            except (ValueError, AttributeError):
                                pass
                    return json.dumps({"error": "Import operation unavailable"})
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > _MAX_RESPONSE_BYTES:
                        return json.dumps({"error": "Import response exceeds limit"})
                    chunks.append(chunk)
        value = json.loads(b"".join(chunks))
        if not isinstance(value, dict):
            raise ValueError("response object required")
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (httpx.HTTPError, ValueError, UnicodeError):
        return json.dumps({"error": "Import operation unavailable"})


def _handle_atlas_import(args: dict, **_kwargs) -> str:
    if (not isinstance(args, dict) or args.get("operation") not in _READ_OPERATIONS
            or not isinstance(args.get("arguments"), dict)):
        return json.dumps({"error": "Invalid import operation"})
    arguments, error = _read_arguments(args["operation"], args["arguments"])
    if error:
        return json.dumps({"error": error})
    return _forward_operation(args["operation"], arguments)


def _handle_atlas_import_proposal(args: dict, **_kwargs) -> str:
    # Atlas accepts exactly {operation, arguments}; arguments are validated there.
    if (not isinstance(args, dict) or args.get("operation") != "change_set"
            or not isinstance(args.get("property_id"), str) or not isinstance(args.get("change_set"), list)
            or set(args) - {"operation", "property_id", "change_set"}):
        return json.dumps({"error": "Invalid import proposal"})
    return _forward_operation("change_set", {"property_id": args["property_id"], "change_set": args["change_set"]})


registry.register(
    name="atlas_import",
    toolset="atlas_import",
    schema={
        "name": "atlas_import",
        "description": "Read cited document evidence, shared Acre catalog meanings, and verified CRM record matches for the current authorized import job. This tool cannot propose, approve, or execute writes.",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": sorted(_READ_OPERATIONS)},
                "arguments": _READ_ARGUMENTS_SCHEMA,
            },
            "required": ["operation", "arguments"],
            "additionalProperties": False,
        },
    },
    handler=_handle_atlas_import,
    max_result_size_chars=_MAX_RESPONSE_BYTES,
)

registry.register(
    name="atlas_import_proposal",
    toolset="atlas_import",
    schema={
        "name": "atlas_import_proposal",
        "description": "Submit a cited, review-only change set for the current authorized import job. Use verified Property and catalog IDs. Validation creates a user Approval preview; this tool cannot approve or execute writes.",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": sorted(_PROPOSAL_OPERATIONS)},
                "property_id": {"type": "string", "description": "Exact verified PRO-... Property ID returned by atlas_import lookup."},
                "change_set": {"type": "array", "description": "Ordered actions, each with id, table, action (create|update), target_id (null for create), citation, fields ({name: {value}}) and dependencies ([{field, action_id}]). property_id is empty for a new Property.",
                               "items": {"type": "object"}},
            },
            "required": ["operation", "property_id", "change_set"],
            "additionalProperties": False,
        },
    },
    handler=_handle_atlas_import_proposal,
    max_result_size_chars=_MAX_RESPONSE_BYTES,
)
