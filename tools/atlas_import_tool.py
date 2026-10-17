"""One-turn, Atlas-owned import operations through a fixed local callback."""

import json
from contextvars import ContextVar

import httpx

from tools.registry import registry


_capability: ContextVar[str] = ContextVar("atlas_import_capability", default="")
_CALLBACK = "http://127.0.0.1:8243/api/v1/internal/import-tool"
_OPERATIONS = {"evidence", "catalog", "lookup", "propose", "revise"}
_MAX_REQUEST_BYTES = 8_192
_MAX_RESPONSE_BYTES = 32_768


def bind_import_capability(value: str):
    return _capability.set(value)


def clear_import_capability(token) -> None:
    _capability.reset(token)


def import_capability_available() -> bool:
    return bool(_capability.get())


def _handle_atlas_import(args: dict, **_kwargs) -> str:
    token = _capability.get()
    if not token:
        return json.dumps({"error": "Import capability unavailable"})
    if not isinstance(args, dict) or args.get("operation") not in _OPERATIONS or not isinstance(args.get("arguments"), dict):
        return json.dumps({"error": "Invalid import operation"})
    body = {"operation": args["operation"], "arguments": args["arguments"]}
    try:
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return json.dumps({"error": "Invalid import operation"})
    if len(encoded) > _MAX_REQUEST_BYTES:
        return json.dumps({"error": "Import request exceeds limit"})
    try:
        with httpx.Client(timeout=httpx.Timeout(8, connect=2), follow_redirects=False,
                          trust_env=False) as client:
            with client.stream("POST", _CALLBACK, headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            }, content=encoded) as response:
                if response.status_code != 200:
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


registry.register(
    name="atlas_import",
    toolset="atlas_import",
    schema={
        "name": "atlas_import",
        "description": "Read evidence and verified Acre context, or prepare an import proposal for the current authorized import job. This tool cannot approve or execute writes.",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": sorted(_OPERATIONS)},
                "arguments": {"type": "object"},
            },
            "required": ["operation", "arguments"],
            "additionalProperties": False,
        },
    },
    handler=_handle_atlas_import,
    max_result_size_chars=_MAX_RESPONSE_BYTES,
)
