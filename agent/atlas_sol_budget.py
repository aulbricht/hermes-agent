"""Optional Atlas-only, fail-closed admission for each Sol Responses request.

The gateway enables this with environment variables; other Hermes profiles do
not import Atlas credentials or contact Atlas accounting.
"""
from __future__ import annotations

import json
import os
import stat
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable


SOL = "gpt-6.1-sol"
LUNA = "gpt-6-luna"
OPENROUTER = "openrouter"
OPENROUTER_RESPONSES_BASE_URL = "https://openrouter.ai/api/v1"


def _transport() -> str:
    value = os.environ.get("ATLAS_MODEL_ROUTING_TRANSPORT", "openai").strip().lower()
    if value not in {"openai", OPENROUTER}:
        raise ValueError("invalid_atlas_model_routing_transport")
    return value


def _canonical_model(model: Any) -> str:
    value = str(model or "").strip()
    if value.startswith("openai/"):
        value = value[len("openai/"):]
    return value


def _routed_model(model: str, transport: str) -> str:
    return f"openai/{model}" if transport == OPENROUTER else model


def _pin_openrouter_provider(payload: dict[str, Any]) -> dict[str, Any]:
    """Pin OpenRouter Responses requests to OpenAI, with no provider fallback."""
    routed = dict(payload)
    extra = routed.get("extra_body")
    extra = dict(extra) if isinstance(extra, dict) else {}
    extra["provider"] = {
        "order": ["openai"], "only": ["openai"],
        "allow_fallbacks": False, "require_parameters": True,
    }
    routed["extra_body"] = extra
    # OpenRouter's default Responses tier is the standard tier. Strip any
    # mutable caller override so neither flex nor priority can be requested.
    # OpenRouter's Responses request enum uses `auto` for the default
    # provider tier; `default` is its normalized response label.
    routed["service_tier"] = "auto"
    return routed


def _validate_bounded_text_request(payload: dict[str, Any]) -> None:
    """Admit self-contained text and local functions with bounded token costs."""
    if any(payload.get(key) for key in ("previous_response_id", "conversation", "prompt")):
        raise ValueError("unbounded_sol_remote_context")
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ValueError("unbounded_sol_provider_tool")
    def visit(value):
        if isinstance(value, dict):
            if str(value.get("type") or "") in {"input_image", "image_url", "input_file", "file", "input_audio", "audio", "video"}:
                raise ValueError("unbounded_sol_media_input")
            if any(key in value for key in ("file_id", "file_url", "image_url", "image_data", "audio_data")):
                raise ValueError("unbounded_sol_media_reference")
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(payload.get("input"))
    visit(payload.get("messages"))


def _read_token(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o037:
        raise ValueError("unsafe_atlas_accounting_token_file")
    if info.st_uid not in {0, os.geteuid()}:
        raise ValueError("untrusted_atlas_accounting_token_owner")
    if info.st_mode & 0o040 and info.st_gid not in {os.getegid(), *os.getgroups()}:
        raise ValueError("unavailable_atlas_accounting_token_group")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("atlas_accounting_token_empty")
    return value


def _maximum_usd(payload: dict[str, Any]) -> Decimal:
    _validate_bounded_text_request(payload)
    # Full serialized text bytes bound token counts, including function schemas. Above 272K
    # input tokens OpenAI applies its long-context price to the whole request.
    input_bound = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    output_bound = int(payload.get("max_output_tokens") or payload.get("max_tokens") or 128_000)
    if output_bound < 1 or output_bound > 128_000:
        raise ValueError("invalid_atlas_sol_output_limit")
    long_context = input_bound > 272_000
    input_rate = Decimal("5") if long_context else Decimal("2.50")
    output_rate = Decimal("15") if long_context else Decimal("10")
    tier = str(payload.get("service_tier") or "standard").lower()
    if tier not in {"standard", "default", "auto", "flex", "batch", "fast", "priority"}:
        raise ValueError("unknown_atlas_sol_price_tier")
    # Reserve at least Standard even for Flex/Batch because the provider may
    # process a retried request at a different tier.
    tier_factor = Decimal(2) if tier in {"fast", "priority"} else Decimal(1)
    reserve = tier_factor * (Decimal(input_bound) * input_rate + Decimal(output_bound) * output_rate) / Decimal(1_000_000)
    return reserve * (Decimal("1.05") if _transport() == OPENROUTER else Decimal(1))


def _field(value: Any, key: str, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def openai_usage_fields(response: Any) -> dict[str, Any]:
    """Preserve raw Responses counts; canonical Hermes input excludes cached tokens."""
    usage = _field(response, "usage")
    inputs = _field(usage, "input_tokens")
    outputs = _field(usage, "output_tokens")
    details = _field(usage, "input_tokens_details")
    output_details = _field(usage, "output_tokens_details")
    cache_read = max(0, int(_field(details, "cached_tokens", 0) or 0))
    cache_write = max(0, int(_field(details, "cache_write_tokens", 0) or _field(details, "cache_creation_tokens", 0) or 0))
    input_total = max(0, int(inputs or 0))
    cache_read = min(cache_read, input_total)
    cache_write = min(cache_write, input_total - cache_read)
    fields = {
        # Hermes canonical input excludes cached tokens; keep the raw Responses
        # total separately so provider receipts can reconstruct the API total.
        "input_tokens": max(0, input_total - cache_read - cache_write),
        "input_tokens_total": input_total,
        "output_tokens": max(0, int(outputs or 0)),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": max(0, int(_field(output_details, "reasoning_tokens", 0) or 0)),
        "usage_available": inputs is not None and outputs is not None,
        "service_tier": _field(response, "service_tier"),
    }
    if _transport() == OPENROUTER:
        usage = _field(response, "usage")
        components = _cost_components(response, Decimal(0))
        fields.update(components)
    return fields


def _estimated_usd(response: Any, reserved: Decimal) -> Decimal:
    usage = _field(response, "usage")
    if usage is None:
        return reserved
    input_tokens = _field(usage, "input_tokens")
    output_tokens = _field(usage, "output_tokens")
    if input_tokens is None or output_tokens is None:
        return reserved
    input_count = max(0, int(input_tokens))
    output_count = max(0, int(output_tokens))
    detail = _field(usage, "input_tokens_details")
    cached = max(0, int(_field(detail, "cached_tokens", 0) or 0)) if detail else 0
    cached = min(cached, input_count)
    writes = min(input_count - cached, max(0, int(_field(detail, "cache_write_tokens", 0) or 0))) if detail else 0
    long_context = input_count > 272_000
    multiplier = Decimal(2) if long_context else Decimal(1)
    output_multiplier = Decimal("1.5") if long_context else Decimal(1)
    model = _canonical_model(_field(response, "model", SOL))
    if model == LUNA:
        input_rate, cached_rate, write_rate, output_rate = (
            Decimal("0.10"), Decimal("0.01"), Decimal("0.125"), Decimal("0.50"),
        )
    else:
        input_rate, cached_rate, write_rate, output_rate = (
            Decimal("2"), Decimal("0.10"), Decimal("2.50"), Decimal("10"),
        )
    result = ((Decimal(input_count - cached - writes) * input_rate + Decimal(cached) * cached_rate + Decimal(writes) * write_rate) * multiplier
              + Decimal(output_count) * output_rate * output_multiplier) / Decimal(1_000_000)
    tier = str(_field(response, "service_tier", "standard") or "standard").lower()
    result *= Decimal(2) if tier in {"fast", "priority"} else Decimal("0.5") if tier in {"flex", "batch"} else Decimal(1)
    return result * (Decimal("1.05") if _transport() == OPENROUTER else Decimal(1))


def _cost_components(response: Any, reserved: Decimal) -> dict[str, Any]:
    """Return content-free OpenRouter cost components and a safe Sol estimate."""
    usage = _field(response, "usage")
    details = _field(usage, "cost_details")
    raw_cost = _field(usage, "cost")
    upstream = _field(details, "upstream_inference_cost")
    try:
        charged = max(Decimal(0), Decimal(str(raw_cost))) if raw_cost is not None else None
    except Exception:
        charged = None
    try:
        upstream_cost = max(Decimal(0), Decimal(str(upstream))) if upstream is not None else None
    except Exception:
        upstream_cost = None
    is_byok = _field(usage, "is_byok")
    if is_byok is True and charged is not None and upstream_cost is not None:
        # OpenRouter explicitly reports both components, including a zero fee
        # while the BYOK allowance covers it. Do not invent a fee in that case.
        reported_total = charged + upstream_cost
        estimated = False
    elif is_byok is False and charged is not None:
        reported_total = charged
        estimated = False
    elif is_byok is None and upstream_cost is None and charged is not None and charged > 0:
        # A positive OpenRouter charge is an observed total when the response
        # does not declare BYOK. An explicit zero without BYOK metadata is
        # ambiguous, so estimate rather than claiming a free request.
        reported_total = charged
        estimated = False
    else:
        reported_total = _estimated_usd(response, reserved)
        estimated = True
    return {
        "actual_cost_usd": None if charged is None else str(charged),
        "upstream_cost_usd": None if upstream_cost is None else str(upstream_cost),
        "byok_total_cost_usd": str(reported_total),
        "actual_cost_estimated": estimated,
        "is_byok": is_byok if isinstance(is_byok, bool) else None,
        "settlement_estimated": estimated,
    }


def _post(socket_path: str, token: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    import httpx

    transport = httpx.HTTPTransport(uds=socket_path)
    with httpx.Client(transport=transport, base_url="http://atlas-accounting", timeout=5) as client:
        response = client.post(path, headers={"X-Atlas-Accounting-Token": token}, json=body)
    response.raise_for_status()
    return response.json()


def admitted_call(agent: Any, payload: dict[str, Any], perform: Callable[[dict[str, Any]], Any]) -> Any:
    socket_path = os.environ.get("ATLAS_SOL_ACCOUNTING_SOCKET", "")
    enabled = os.environ.get("ATLAS_MODEL_ROUTING_ENABLED") == "true" or bool(socket_path) or bool(os.environ.get("ATLAS_SOL_ACCOUNTING_TOKEN_FILE"))
    if enabled:
        agent._atlas_sol_last_call = {}
    if not enabled:
        return perform(payload)
    transport = _transport()
    requested_model = _canonical_model(payload.get("model"))
    routed_payload = dict(payload)
    if transport == OPENROUTER:
        routed_payload["model"] = _routed_model(requested_model, transport)
        routed_payload = _pin_openrouter_provider(routed_payload)
    if requested_model != SOL:
        return perform(routed_payload)
    reservation_id = "sol-" + uuid.uuid4().hex
    try:
        if not socket_path:
            raise ValueError("atlas_accounting_socket_missing")
        maximum = _maximum_usd(routed_payload)
        token_path = Path(os.environ["ATLAS_SOL_ACCOUNTING_TOKEN_FILE"])
        token = _read_token(token_path)
        reservation = _post(socket_path, token, "/v1/sol/reservations", {
            "reservation_id": reservation_id, "user_id": "atlas-hermes", "max_usd": str(maximum),
            "route_request_id": getattr(agent, "_atlas_route_request_id", None),
        })
    except Exception:
        reservation = {"status": "unavailable"}
    if reservation.get("status") != "reserved":
        # A failed gate cannot authorize Sol. Switch the current agent too so
        # subsequent tool iterations stay on Luna for this turn.
        routed_payload = dict(routed_payload)
        routed_payload["model"] = _routed_model(LUNA, transport)
        routed_payload["reasoning"] = {"effort": "xhigh"}
        routed_payload.pop("reasoning_effort", None)
        if transport == OPENROUTER:
            routed_payload = _pin_openrouter_provider(routed_payload)
        agent.model = _routed_model(LUNA, transport)
        agent.reasoning_config = {"enabled": True, "effort": "xhigh"}
        agent._atlas_sol_last_call = {"route_request_id": getattr(agent, "_atlas_route_request_id", None), "reservation_id": None, "actual_model": agent.model}
        return perform(routed_payload)
    try:
        response = perform(routed_payload)
    except BaseException:
        # A streamed or interrupted call can still have incurred provider cost.
        # Hold the full estimate when usage is unavailable.
        try:
            _post(socket_path, token, "/v1/sol/settlements", {
                "reservation_id": reservation_id, "settled_usd": str(maximum), "estimated": True,
            })
        except Exception:
            pass  # Keep the reservation and preserve the transport exception.
        raise
    try:
        components = _cost_components(response, maximum) if transport == OPENROUTER else {}
        estimated = Decimal(components["byok_total_cost_usd"]) if components else _estimated_usd(response, maximum)
        generation_id = str(_field(response, "id", "") or "")
        actual_model = str(_field(response, "model", routed_payload["model"]) or routed_payload["model"])
        agent._atlas_sol_last_call = {
            "route_request_id": getattr(agent, "_atlas_route_request_id", None),
            "reservation_id": reservation_id, "max_usd": str(maximum),
            "settled_usd": str(estimated), "provider_generation_id": generation_id,
            "actual_model": actual_model, "settlement_estimated": bool(components.get("actual_cost_estimated", True)),
            **components,
        }
        _post(socket_path, token, "/v1/sol/settlements", {
            "reservation_id": reservation_id,
            "settled_usd": str(estimated), "estimated": bool(components.get("settlement_estimated", True)),
            "provider_generation_id": generation_id, "actual_model": actual_model,
        })
    except Exception:
        # The reservation stays held if settlement is unavailable; the model
        # response is already complete and must not be retried for accounting.
        pass
    return response
