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
_PROPOSAL_OPERATIONS = {"propose", "revise"}
_MAX_REQUEST_BYTES = 128 * 1024
_MAX_RESPONSE_BYTES = 32_768

# The provider builds function arguments from this schema. An unstructured
# `arguments: {type: object}` caused proposal calls to arrive as `{}` in a
# live import turn; the proposal now has its own required top-level fields.
_PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "row_id": {"type": "string", "description": "Unique ID for this proposed source row."},
                "source_label": {"type": "string", "description": "Exact named occupant label found in cited source evidence."},
                "source_evidence": {"type": "array", "items": {"type": "string"},
                                    "description": "Citation IDs returned by the evidence operation."},
                "record_type": {"type": "string", "description": "Verified destination entity from the catalog."},
                "action": {"type": "string", "enum": ["create", "update"]},
                "record_id": {"type": "string", "description": "Verified existing record ID for an update."},
                "match_resolution": {"type": "string", "description": "Verified match choice when needed."},
                "fields": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string", "description": "Verified physical field name from the catalog."},
                        "value": {"type": "string", "description": "Proposed Acre value as text."},
                        "source_value": {"type": "string", "description": "Exact value present in each cited source line."},
                        "source_evidence": {"type": "array", "items": {"type": "string"}},
                        "transformation": {"type": "string", "description": "Catalog-permitted conversion, such as copy or monthly_x_12."},
                    },
                    "required": ["field", "value", "source_value", "source_evidence", "transformation"],
                    "additionalProperties": False,
                }},
            },
            "required": ["row_id", "source_label", "source_evidence", "record_type", "action", "fields"],
            "additionalProperties": False,
        }},
        "unmapped": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "value": {"type": "string"},
                "evidence": {"type": "string", "description": "Citation ID for an excluded source fact."},
                "reason": {"type": "string"},
            },
            "required": ["label", "value", "evidence"],
            "additionalProperties": False,
        }},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["rows", "unmapped", "warnings"],
    "additionalProperties": False,
}

_READ_ARGUMENTS_SCHEMA = {
    "type": "object",
    "description": "Use only the selected operation's fields: evidence offset and limit (1–50); catalog logical_ids; lookup property_query or verified property_id with optional tenant_names. Leave unrelated fields empty.",
    "properties": {
        "offset": {"type": "integer"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "logical_ids": {"type": "array", "items": {"type": "string"}},
        "property_query": {"type": "string"},
        "property_id": {"type": "string", "description": "Exact PRO-... ID returned by a verified Property lookup."},
        "tenant_names": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}


def bind_import_capability(value: str):
    return _capability.set(value)


def clear_import_capability(token) -> None:
    _capability.reset(token)


def import_capability_available() -> bool:
    return bool(_capability.get())


def _read_arguments(operation: str, arguments: dict) -> tuple[dict | None, str | None]:
    allowed = _READ_ARGUMENT_KEYS[operation]
    for key in arguments:
        if key not in _READ_ARGUMENTS_SCHEMA["properties"]:
            return None, f"arguments.{key} is not a supported read field"
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
    if (not isinstance(args, dict) or args.get("operation") not in _PROPOSAL_OPERATIONS
            or not isinstance(args.get("property_id"), str)
            or not isinstance(args.get("proposal"), dict)
            or (args["operation"] == "revise" and not isinstance(args.get("expected_plan_hash"), str))
            or set(args) - {"operation", "property_id", "proposal", "expected_plan_hash"}):
        return json.dumps({"error": "Invalid import proposal"})
    arguments = {key: args[key] for key in ("property_id", "proposal", "expected_plan_hash") if key in args}
    return _forward_operation(args["operation"], arguments)


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
        "description": "Submit a cited, review-only mapping proposal or revision for the current authorized import job. Use verified Property and catalog IDs. Validation creates a user Approval preview; this tool cannot approve or execute writes.",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": sorted(_PROPOSAL_OPERATIONS)},
                "property_id": {"type": "string", "description": "Exact verified PRO-... Property ID returned by atlas_import lookup."},
                "proposal": _PROPOSAL_SCHEMA,
                "expected_plan_hash": {"type": "string", "description": "Exact current plan hash; required for revise."},
            },
            "required": ["operation", "property_id", "proposal"],
            "additionalProperties": False,
        },
    },
    handler=_handle_atlas_import_proposal,
    max_result_size_chars=_MAX_RESPONSE_BYTES,
)
