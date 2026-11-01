"""One-turn ownership previews through Atlas's fixed, capability-bound callback.

Schema source: acre-atlas 72e6b0f6, ownership_work._proposal and ownership_execution.actions.
Business references are untrusted; only Atlas can resolve them against bound work.
"""

import json
import re
import unicodedata
from contextvars import ContextVar

import httpx

from tools.registry import registry


_capability: ContextVar[str] = ContextVar("atlas_ownership_capability", default="")
_CALLBACK = "http://127.0.0.1:8243/api/v1/internal/ownership-tool"
_PROTOCOL = "acre.ownership-tool.v1"
_MAX_REQUEST_BYTES = 131072
_MAX_RESPONSE_BYTES = 32768
# Atlas's real catalog envelope is ~48 KB. Match its existing 64 KB ceiling
# with headroom; 32 KB was a generic transport bound, not a catalog constraint.
_MAX_CATALOG_RESPONSE_BYTES = 65536
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
_LEGACY_CHANGE_SCHEMA = _object({
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
# Generic values remain selectors into Atlas-owned immutable observations.
# The separate namespace cannot reclassify typed name/email/address evidence.
_GENERIC_CHANGE_SCHEMA = _object({
    **_LEGACY_CHANGE_SCHEMA["properties"],
    "kind": {"type": "string", "enum": ["generic"]},
    "source_path": {**_text(240), "pattern": r"/generic/(?:[^~]|~[01])+"},
    "proposed_value": {"anyOf": [_text(2000), {"type": "null"}]},
    "possible_cleaned_value": {"type": "null"},
    "depends_on": {**_array(_text(160), 100), "uniqueItems": True},
})
_CHANGE_SCHEMA = {"anyOf": [_LEGACY_CHANGE_SCHEMA, _GENERIC_CHANGE_SCHEMA]}
# Only these two slots accept model-proposed field names. They are business
# data, never trusted scope; Atlas checks source values, target scope and Approval.
_AUTHORITY_KEYS = frozenset({
    "id", "work_id", "session_id", "turn_id", "principal", "principal_id",
    "user_id", "owner_id", "token", "capability", "atlas_ownership_capability",
    "atlas_import_capability", "callback_url", "url", "database",
})
_FIELD_MAP_SCHEMA = {
    "type": "object", "properties": {}, "required": [],
    "additionalProperties": {"type": "string", "maxLength": 2000},
    "maxProperties": 100,
    "propertyNames": {"type": "string", "minLength": 1, "maxLength": 160,
                      "not": {"enum": sorted(_AUTHORITY_KEYS)}},
}
_ACTION_SCHEMA = _object({
    "id": _text(160),
    "table": {"type": "string", "enum": ["API_Contact", "API_ContactDetail", "API_JOIN_propertyContact"]},
    "action": {"type": "string", "enum": ["create", "update"]},
    "fields": {**_FIELD_MAP_SCHEMA, "minProperties": 1},
    "target_id": {"anyOf": [_text(160), {"type": "null"}]},
    "duplicate_probe": _FIELD_MAP_SCHEMA,
    "dependencies": _array(_object({"field": _FIELD_MAP_SCHEMA["propertyNames"],
                                    "action_id": _text(160)}), 100),
    "holds": _array(_text(), 0),
})
# Reconciled with Atlas semantic-transformations / SNO-424, 2026-10-28.
# These selectors and business fields never confer authority. Atlas resolves
# immutable evidence, metadata, target scope, holds and overwrite decisions.
_TRANSFORMATION_SCHEMA = _object({
    "id": _text(160),
    "source_ref": _text(240),
    "association_index": {"type": "integer", "minimum": 0},
    "source_inputs": {**_array(_object({
        "source_ref": _text(240),
        "path": {**_text(240), "pattern": r"^(?:/(?:[^~]|~[01])*)+$"},
    }), 10), "minItems": 1},
    "action_id": _text(160),
    "table": _ACTION_SCHEMA["properties"]["table"],
    "field": {**_text(101), "pattern": r"^(?!id__)[A-Za-z][A-Za-z0-9_]{0,100}$",
              "not": {"enum": sorted(_AUTHORITY_KEYS | {"target_id"})}},
    "proposed_value": {**_text(2000), "description": "Model-proposed text, never observed evidence. Controls, format characters, surrogates, invisible fillers and variation selectors are refused unchanged."},
    "rationale": _text(2000),
    "guidance_revision": _text(160),
    "catalog_revision": _text(160),
    "uncertainty": _array(_text(2000), 10),
    "depends_on": {**_array(_text(160), 100), "minItems": 1, "uniqueItems": True},
})
_PROPOSAL_SCHEMA = _object({
    "property_id": _text(160),
    "evidence_hash": _text(160),
    "associations": _array(_object({
        "source_ref": _text(160),
        "contact_id": {"anyOf": [_text(160), {"type": "null"}]},
        "display_values": _object({"display_name": _text(240)}),
        "rationale": _text(),
        "role": _text(240),
    }), 100),
    "unresolved_questions": _array(_text(), 10),
    "changes": _array(_CHANGE_SCHEMA, 100),
    "execution_plan": {**_array(_ACTION_SCHEMA, 100), "minItems": 1},
    "transformations": _array(_TRANSFORMATION_SCHEMA, 100),
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
    if "not" in schema and value in schema["not"].get("enum", []):
        return False
    if "anyOf" in schema:
        return any(_matches(value, branch) for branch in schema["anyOf"])
    kind = schema["type"]
    if type(value) is not {"object": dict, "array": list, "string": str,
                           "integer": int, "null": type(None)}[kind]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if kind == "object":
        properties = schema["properties"]
        extra = schema["additionalProperties"]
        return (schema.get("minProperties", 0) <= len(value) <= schema.get("maxProperties", len(value))
                and set(schema["required"]).issubset(value)
                and all((key in properties or isinstance(extra, dict))
                        and ("propertyNames" not in schema or _matches(key, schema["propertyNames"]))
                        and _matches(item, properties[key] if key in properties else extra)
                        for key, item in value.items()))
    if kind == "array":
        return (schema.get("minItems", 0) <= len(value) <= schema["maxItems"]
                and all(_matches(item, schema["items"]) for item in value)
                and (not schema.get("uniqueItems") or len(set(value)) == len(value)))
    if kind == "string":
        return (schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", _MAX_REQUEST_BYTES)
                and (not schema.get("minLength") or bool(value.strip()))
                and ("pattern" not in schema or re.fullmatch(schema["pattern"], value) is not None)
                and re.search(r"(?:atown_|atimp_)[A-Za-z0-9_-]{32,128}", value) is None)
    if kind == "integer":
        return schema.get("minimum", value) <= value <= schema.get("maximum", value)
    return True


def _failed_schema_field(value, schema, field="arguments"):
    """Locate a rejected schema field without echoing business keys or values.

    Called only after _matches fails. Dynamic field-map keys and union values
    are reported at their enclosing schema-defined field.
    """
    if schema.get("type") == "object" and type(value) is dict:
        properties = schema["properties"]
        for key in schema["required"]:
            if key not in value:
                return field + "." + key
        for key, child_schema in properties.items():
            if key in value and not _matches(value[key], child_schema):
                return _failed_schema_field(value[key], child_schema, field + "." + key)
    elif schema.get("type") == "array" and type(value) is list:
        for item in value:
            if not _matches(item, schema["items"]):
                return _failed_schema_field(item, schema["items"], field + "[]")
    return field


def _transformation_value_safe(value):
    # Refuse, never clean. The original source remains Atlas-owned evidence.
    return not any(unicodedata.category(char) in {"Cc", "Cf", "Cs"}
                   or ord(char) in {0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x3164, 0xFFA0}
                   or 0xFE00 <= ord(char) <= 0xFE0F
                   or 0xE0100 <= ord(char) <= 0xE01EF for char in value)


def _generic_changes_valid(proposal):
    """Validate local references; only Atlas can resolve original source text."""
    changes = proposal.get("changes", [])
    if not any(change["kind"] == "generic" for change in changes):
        return True
    seen, targets = {}, set()
    for change in changes:
        if change["id"] in seen:
            return False
        if change["kind"] == "generic":
            index = change["association_index"]
            target = (index, change["source_path"])
            if (index >= len(proposal["associations"])
                    or proposal["associations"][index]["source_ref"] != change["source_ref"]
                    or target in targets
                    or (change["proposed_value"] is not None
                        and not _transformation_value_safe(change["proposed_value"]))):
                return False
            for dependency in change["depends_on"]:
                prior = seen.get(dependency)
                if (prior is None or prior["source_ref"] != change["source_ref"]
                        or prior["association_index"] != index):
                    return False
            targets.add(target)
        seen[change["id"]] = change
    return True


def _transformations_valid(proposal):
    """Check local proposal relationships without interpreting source meaning.

    Atlas alone verifies source pointer resolution/provenance, guidance/catalog
    revisions, writable non-key metadata and held ancestors before Approval.
    """
    transformations = proposal.get("transformations", [])
    if not transformations:
        return True
    actions = proposal.get("execution_plan", [])
    changes = proposal.get("changes", [])
    action_ids = [a["id"] for a in actions]
    change_ids = [c["id"] for c in changes]
    if len(set(action_ids)) != len(action_ids) or len(set(change_ids)) != len(change_ids):
        return False
    actions = {a["id"]: a for a in actions}
    changes = {c["id"]: c for c in changes}
    seen_ids, seen_targets = set(), set()
    for item in transformations:
        field = item["field"]
        target = (item["action_id"], item["table"], field)
        action = actions.get(item["action_id"])
        index = item["association_index"]
        if (item["id"] in seen_ids or target in seen_targets
                or field.startswith("id__") or not _transformation_value_safe(item["proposed_value"])
                or index >= len(proposal["associations"])
                or proposal["associations"][index]["source_ref"] != item["source_ref"]
                or action is None or action["table"] != item["table"]
                or action["fields"].get(field) != item["proposed_value"]):
            return False
        for dependency in item["depends_on"]:
            change = changes.get(dependency)
            if (change is None or change["source_ref"] != item["source_ref"]
                    or change["association_index"] != index):
                return False
        for selector in item["source_inputs"]:
            if selector["path"].startswith("/generic/"):
                if (selector["source_ref"] != item["source_ref"]
                        or not any(changes[dep]["kind"] == "generic"
                                   and changes[dep]["source_path"] == selector["path"]
                                   for dep in item["depends_on"])):
                    return False
        seen_ids.add(item["id"])
        seen_targets.add(target)
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
                and (set(error) == {"code"} or (set(error) == {"code", "rule"}
                     and error.get("code") == "OWNERSHIP_EVIDENCE_MISMATCH"
                     and type(error["rule"]) is str
                     and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", error["rule"]) is not None))
                and type(error["code"]) is str
                and error["code"] in _ERROR_CODES)
    return False


def _forward_operation(operation, arguments):
    token = _capability.get()
    if not token:
        return _UNAVAILABLE
    response_limit = _MAX_CATALOG_RESPONSE_BYTES if operation == "catalog" else _MAX_RESPONSE_BYTES
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
                    if size > response_limit:
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
        field = _failed_schema_field(args, _PROPOSAL_TOOL_SCHEMA)
        return json.dumps({"error": field + ": not accepted"})
    if (not _generic_changes_valid(args["proposal"])
            or not _transformations_valid(args["proposal"])):
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
    handler=_handle_atlas_ownership, max_result_size_chars=_MAX_CATALOG_RESPONSE_BYTES,
)
registry.register(
    name="atlas_ownership_proposal", toolset="atlas_ownership",
    schema={"name": "atlas_ownership_proposal", "description": "Submit a held ownership proposal or immutable correction revision. Revise requires base_revision and correction_text; propose omits them. Optional execution_plan proposes exact actions, field values, duplicate probes and earlier-action dependencies for separate review. Generic changes select original text through source_path within immutable observation.values.generic; proposed_value must equal the original or be null (held), possible_cleaned_value is null. Atlas resolves and verifies evidence. Every writable generic use requires an exact field-bound transformation with a matching source_path dependency. Optional transformations retain source selectors, exact action/table/field/value, rationale and guidance/catalog revisions for review; they are model proposals, never observed evidence. Business references must match bound evidence; Atlas validates scope. Never approves or executes.",
            "parameters": _PROPOSAL_TOOL_SCHEMA},
    handler=_handle_atlas_ownership_proposal, max_result_size_chars=_MAX_RESPONSE_BYTES,
)
