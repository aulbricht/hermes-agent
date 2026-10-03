"""Atlas-only receipts for completed auxiliary OpenAI calls.

The helper intentionally receives only normalized token usage. It never sends
prompts or generated content to the accounting service, and receipt failures do
not trigger another model request after a response has completed.
"""
from __future__ import annotations

import os
import stat
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


LUNA = "gpt-6-luna"
ACCOUNTING_SOCKET = "/run/acre-atlas-accounting/accounting.sock"
ACCOUNTING_TOKEN_FILE = "/etc/acre-atlas/accounting-ingest.token"


def atlas_auxiliary_enabled() -> bool:
    return os.environ.get("ATLAS_MODEL_ROUTING_ENABLED", "").lower() == "true"


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def receipt_payload(response: Any, *, requested_model: str = LUNA) -> dict[str, Any]:
    """Build a content-free provider receipt accepted by Atlas accounting."""
    from agent.atlas_sol_budget import OPENROUTER, _transport

    transport = _transport()
    usage = _get(response, "usage")
    input_tokens = _get(usage, "input_tokens")
    if input_tokens is None:
        input_tokens = _get(usage, "prompt_tokens")
    output_tokens = _get(usage, "output_tokens")
    if output_tokens is None:
        output_tokens = _get(usage, "completion_tokens")
    details = _get(usage, "input_tokens_details") or _get(usage, "prompt_tokens_details") or {}
    cache_read = _get(details, "cached_tokens", 0) or 0
    cache_write = _get(details, "cache_write_tokens", 0) or 0
    model = str(_get(response, "model", "") or requested_model)
    has_usage = input_tokens is not None and output_tokens is not None
    normalized_input = max(0, int(input_tokens or 0))
    normalized_output = max(0, int(output_tokens or 0))
    normalized_cache_read = min(normalized_input, max(0, int(cache_read)))
    normalized_cache_write = min(normalized_input - normalized_cache_read, max(0, int(cache_write)))
    estimated_cost = Decimal(0)
    if has_usage and model.removeprefix("openai/") == LUNA:
        inputs, outputs = normalized_input, normalized_output
        cached, writes = normalized_cache_read, normalized_cache_write
        long_factor = Decimal(2) if inputs > 272_000 else Decimal(1)
        output_factor = Decimal("1.5") if inputs > 272_000 else Decimal(1)
        tier = str(_get(response, "service_tier", "standard") or "standard").lower()
        service_factor = Decimal("0.5") if tier in {"flex", "batch"} else Decimal(2) if tier in {"fast", "priority"} else Decimal(1)
        estimated_cost = (
            (Decimal(inputs - cached - writes) * Decimal("0.10")
             + Decimal(cached) * Decimal("0.01")
             + Decimal(writes) * Decimal("0.125")) * long_factor
            + Decimal(outputs) * Decimal("0.50") * output_factor
        ) * service_factor / Decimal(1_000_000)

    is_byok_value = _get(usage, "is_byok")
    is_byok = is_byok_value if isinstance(is_byok_value, bool) else None
    cost_details = _get(usage, "cost_details") or {}
    raw_cost = _get(usage, "cost")
    raw_upstream = _get(cost_details, "upstream_inference_cost")
    if raw_upstream is None:
        raw_upstream = _get(usage, "upstream_cost_usd")
    try:
        charged = max(Decimal(0), Decimal(str(raw_cost))) if raw_cost is not None else None
    except Exception:
        charged = None
    try:
        upstream = max(Decimal(0), Decimal(str(raw_upstream))) if raw_upstream is not None else None
    except Exception:
        upstream = None
    estimated_total = Decimal(0)
    if transport == OPENROUTER:
        # Only claim an observed BYOK total when OpenRouter explicitly returns
        # both components. Token-derived combined estimates live in the
        # separate estimated_total_usd field, never in either component.
        if is_byok is True and (charged is None or upstream is None) and has_usage and model.removeprefix("openai/") == LUNA:
            estimated_total = estimated_cost * Decimal("1.05")
        cost_basis = (
            "byok_split" if is_byok is True and charged is not None and upstream is not None
            else "byok_estimated" if is_byok is True and estimated_total
            else "byok_upstream_missing" if is_byok is True
            else "openrouter_charge" if charged is not None
            else "unknown"
        )
        verification_state = "provider_response" if charged is not None or upstream is not None else "pending"
    else:
        charged = estimated_cost if has_usage and model == LUNA else None
        upstream = Decimal(0)
        cost_basis = "estimated" if charged is not None else "unknown"
        verification_state = "estimated" if charged is not None else "pending"

    return {
        "idempotency_key": "hermes-aux:" + uuid.uuid4().hex,
        "occurred_at": datetime.now(UTC).isoformat(),
        "user_id": None,
        "attribution": "system",
        "purpose": "system_other",
        "resource_type": "",
        "resource_id": "",
        "route_request_id": None,
        "reservation_id": None,
        "model": model,
        "provider": "openrouter" if transport == OPENROUTER else "openai",
        "provider_generation_id": str(_get(response, "id", "") or ""),
        "provider_user_hash": "atlas-system",
        "input_tokens": normalized_input,
        "output_tokens": normalized_output,
        "cache_read_tokens": normalized_cache_read,
        "cache_write_tokens": normalized_cache_write,
        "reasoning_tokens": max(0, int(_get(_get(usage, "output_tokens_details") or _get(usage, "completion_tokens_details") or {}, "reasoning_tokens", 0) or 0)),
        "request_count": 1,
        "charged_usd": str(charged or Decimal(0)),
        "upstream_usd": str(upstream or Decimal(0)),
        "is_byok": is_byok if transport == OPENROUTER else None,
        "cost_basis": cost_basis,
        "estimated_total_usd": str(estimated_total),
        "verification_state": verification_state,
        "source": "hermes_auxiliary",
    }


def _read_token(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o037:
        raise ValueError("unsafe_atlas_accounting_token_file")
    if info.st_uid not in {0, os.geteuid()}:
        raise ValueError("untrusted_atlas_accounting_token_owner")
    if info.st_mode & 0o040 and info.st_gid not in {os.getegid(), *os.getgroups()}:
        raise ValueError("unavailable_atlas_accounting_token_group")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("atlas_accounting_token_empty")
    return token


def _headers() -> dict[str, str]:
    token_file = Path(
        os.environ.get("ATLAS_ACCOUNTING_TOKEN_FILE")
        or os.environ.get("ATLAS_SOL_ACCOUNTING_TOKEN_FILE")
        or ACCOUNTING_TOKEN_FILE
    )
    return {"X-Atlas-Accounting-Token": _read_token(token_file)}


def record_completed_response(response: Any, *, requested_model: str = LUNA) -> None:
    """Best-effort receipt submission; never repeats the completed LLM call."""
    import logging
    import httpx

    try:
        socket_path = os.environ.get("ATLAS_ACCOUNTING_SOCKET") or os.environ.get("ATLAS_SOL_ACCOUNTING_SOCKET") or ACCOUNTING_SOCKET
        transport = httpx.HTTPTransport(uds=socket_path)
        with httpx.Client(transport=transport, base_url="http://atlas-accounting", timeout=5) as client:
            result = client.post("/v1/receipts", headers=_headers(), json=receipt_payload(response, requested_model=requested_model))
        result.raise_for_status()
    except Exception as exc:
        logging.getLogger(__name__).error(
            "Atlas auxiliary receipt ingestion failed after completed response (error_type=%s)",
            type(exc).__name__,
        )


async def record_completed_response_async(response: Any, *, requested_model: str = LUNA) -> None:
    """Async counterpart; ingestion failure never retries the model request."""
    import logging
    import httpx

    try:
        socket_path = os.environ.get("ATLAS_ACCOUNTING_SOCKET") or os.environ.get("ATLAS_SOL_ACCOUNTING_SOCKET") or ACCOUNTING_SOCKET
        transport = httpx.AsyncHTTPTransport(uds=socket_path)
        async with httpx.AsyncClient(transport=transport, base_url="http://atlas-accounting", timeout=5) as client:
            result = await client.post("/v1/receipts", headers=_headers(), json=receipt_payload(response, requested_model=requested_model))
        result.raise_for_status()
    except Exception as exc:
        logging.getLogger(__name__).error(
            "Atlas auxiliary receipt ingestion failed after completed response (error_type=%s)",
            type(exc).__name__,
        )


def collect_delegated_response(agent, response, *, requested_model=LUNA):
    """Keep completed usage for durable broker settlement, even after revocation."""
    import hashlib
    payload = receipt_payload(response, requested_model=requested_model)
    context = agent._atlas_delegation_identities
    actor = context["actor_user_id"]
    usage = _get(response, "usage") or {}
    details = _get(usage, "cost_details") or {}
    def observed_cost(value):
        try:
            amount = Decimal(str(value))
            return float(amount) if not isinstance(value, bool) and amount.is_finite() and amount >= 0 else None
        except Exception:
            return None
    actual = observed_cost(_get(usage, "cost"))
    upstream = observed_cost(_get(details, "upstream_inference_cost", _get(usage, "upstream_cost_usd")))
    call = {
        "auxiliary": True,
        "user_id": actor, "actor_user_id": actor,
        "subject_user_id": context["subject_user_id"],
        "view_as_session_id": context["view_as_session_id"],
        "resource_type": context["resource_type"], "resource_id": context["resource_id"],
        "provider_user_hash": "atlas-user-" + hashlib.sha256(actor.encode()).hexdigest(),
        "generation_id": payload["provider_generation_id"],
        "model": payload["model"], "provider": payload["provider"],
        "route_request_id": getattr(agent, "_atlas_route_request_id", None),
        "reservation_id": None, "usage_available": _get(response, "usage") is not None,
        "input_tokens": max(0, payload["input_tokens"] - payload["cache_read_tokens"] - payload["cache_write_tokens"]),
        "input_tokens_total": payload["input_tokens"], "output_tokens": payload["output_tokens"],
        "cache_read_tokens": payload["cache_read_tokens"], "cache_write_tokens": payload["cache_write_tokens"],
        "reasoning_tokens": payload["reasoning_tokens"],
        "estimated_cost_usd": float(payload["estimated_total_usd"]),
        "actual_cost_usd": actual if payload["provider"] == "openrouter" else None,
        "upstream_cost_usd": upstream if payload["provider"] == "openrouter" else 0.0,
        "is_byok": payload["is_byok"], "cost_basis": payload["cost_basis"],
        "actual_cost_estimated": payload["cost_basis"] == "byok_estimated",
        "byok_total_cost_usd": float(payload["estimated_total_usd"]) if payload["cost_basis"] == "byok_estimated" else None,
        "service_tier": _get(response, "service_tier"),
    }
    with agent._atlas_paid_dispatch_lock:
        agent._atlas_auxiliary_usage_calls.append(call)
