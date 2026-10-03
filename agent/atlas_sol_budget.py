"""Optional Atlas-only, fail-closed admission for each Sol Responses request.

The gateway enables this with environment variables; other Hermes profiles do
not import Atlas credentials or contact Atlas accounting.
"""
from __future__ import annotations

import json
import hashlib
import os
import stat
import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable


SOL = "gpt-6.1-sol"
LUNA = "gpt-6-luna"
OPENROUTER = "openrouter"
OPENROUTER_RESPONSES_BASE_URL = "https://openrouter.ai/api/v1"


@dataclass(frozen=True)
class AdmissionPolicy:
    """One immutable snapshot of Atlas's nonsecret per-call admission rules."""

    enabled: bool
    accounting_socket_configured: bool
    accounting_token_file_configured: bool
    accounting_socket_path: str = field(repr=False)
    accounting_token_file_path: str = field(repr=False)
    accounting_socket_ref_sha256: str
    accounting_token_file_ref_sha256: str
    transport: str
    sol_model: str
    luna_model: str
    openrouter_provider: str
    openrouter_base_url: str
    openrouter_model_prefix: str
    provider_order: tuple[str, ...]
    provider_only: tuple[str, ...]
    allow_fallbacks: bool
    require_parameters: bool
    budget_enforcement: str
    request_service_tier: str
    pricing_tier: str
    default_output_tokens: int
    max_output_tokens: int
    long_context_threshold_bytes: int
    standard_sol_input_usd_per_million: Decimal
    standard_sol_output_usd_per_million: Decimal
    long_context_sol_input_usd_per_million: Decimal
    long_context_sol_output_usd_per_million: Decimal
    estimated_sol_input_usd_per_million: Decimal
    estimated_sol_cached_input_usd_per_million: Decimal
    estimated_sol_cache_write_usd_per_million: Decimal
    estimated_sol_output_usd_per_million: Decimal
    luna_input_usd_per_million: Decimal
    luna_cached_input_usd_per_million: Decimal
    luna_cache_write_usd_per_million: Decimal
    luna_output_usd_per_million: Decimal
    long_context_input_multiplier: Decimal
    long_context_output_multiplier: Decimal
    high_price_tiers: tuple[str, ...]
    discount_price_tiers: tuple[str, ...]
    known_price_tiers: tuple[str, ...]
    openrouter_margin: Decimal
    fallback_effort: str

    def public_projection(self) -> dict[str, Any]:
        """Return only canonical, nonsecret policy values for evidence hashing."""
        result = asdict(self)
        result.pop("accounting_socket_path")
        result.pop("accounting_token_file_path")
        for key, value in tuple(result.items()):
            if isinstance(value, Decimal):
                result[key] = str(value)
            elif isinstance(value, tuple):
                result[key] = list(value)
        return result


def capture_admission_policy() -> AdmissionPolicy:
    """Capture current nonsecret inputs once; performs no I/O or provider calls."""
    socket_path = os.environ.get("ATLAS_SOL_ACCOUNTING_SOCKET", "")
    token_file_path = os.environ.get("ATLAS_SOL_ACCOUNTING_TOKEN_FILE", "")
    socket_configured = bool(socket_path)
    token_file_configured = bool(token_file_path)
    enabled = (os.environ.get("ATLAS_MODEL_ROUTING_ENABLED") == "true"
               or socket_configured or token_file_configured)
    return AdmissionPolicy(
        enabled=enabled,
        accounting_socket_configured=socket_configured,
        accounting_token_file_configured=token_file_configured,
        accounting_socket_path=socket_path, accounting_token_file_path=token_file_path,
        accounting_socket_ref_sha256=hashlib.sha256(socket_path.encode()).hexdigest() if socket_path else "",
        accounting_token_file_ref_sha256=hashlib.sha256(token_file_path.encode()).hexdigest() if token_file_path else "",
        transport=_transport(), sol_model=SOL, luna_model=LUNA,
        openrouter_provider=OPENROUTER,
        openrouter_base_url=OPENROUTER_RESPONSES_BASE_URL,
        openrouter_model_prefix="openai/",
        provider_order=("openai",), provider_only=("openai",),
        allow_fallbacks=False, require_parameters=True,
        budget_enforcement="external_accounting_reservation_service",
        request_service_tier="auto", pricing_tier="standard",
        default_output_tokens=128_000, max_output_tokens=128_000,
        long_context_threshold_bytes=272_000,
        standard_sol_input_usd_per_million=Decimal("2.50"),
        standard_sol_output_usd_per_million=Decimal("10"),
        long_context_sol_input_usd_per_million=Decimal("5"),
        long_context_sol_output_usd_per_million=Decimal("15"),
        estimated_sol_input_usd_per_million=Decimal("2"),
        estimated_sol_cached_input_usd_per_million=Decimal("0.10"),
        estimated_sol_cache_write_usd_per_million=Decimal("2.50"),
        estimated_sol_output_usd_per_million=Decimal("10"),
        luna_input_usd_per_million=Decimal("0.10"),
        luna_cached_input_usd_per_million=Decimal("0.01"),
        luna_cache_write_usd_per_million=Decimal("0.125"),
        luna_output_usd_per_million=Decimal("0.50"),
        long_context_input_multiplier=Decimal("2"),
        long_context_output_multiplier=Decimal("1.5"),
        high_price_tiers=("fast", "priority"),
        discount_price_tiers=("flex", "batch"),
        known_price_tiers=("standard", "default", "auto", "flex", "batch", "fast", "priority"),
        openrouter_margin=Decimal("1.05"), fallback_effort="xhigh",
    )


def canonical_admission_policy_projection(policy: AdmissionPolicy | None = None) -> dict[str, Any]:
    """Canonical nonsecret projection for the owner generation publisher."""
    return (policy or capture_admission_policy()).public_projection()


def _transport() -> str:
    value = os.environ.get("ATLAS_MODEL_ROUTING_TRANSPORT", "openai").strip().lower()
    if value not in {"openai", OPENROUTER}:
        raise ValueError("invalid_atlas_model_routing_transport")
    return value


def _canonical_model(model: Any, policy: AdmissionPolicy | None = None) -> str:
    value = str(model or "").strip()
    prefix = policy.openrouter_model_prefix if policy else "openai/"
    if value.startswith(prefix):
        value = value[len(prefix):]
    return value


def _routed_model(model: str, transport: str, policy: AdmissionPolicy | None = None) -> str:
    prefix = policy.openrouter_model_prefix if policy else "openai/"
    is_openrouter = transport == (policy.openrouter_provider if policy else OPENROUTER)
    return f"{prefix}{model}" if is_openrouter else model


def _pin_openrouter_provider(payload: dict[str, Any], policy: AdmissionPolicy | None = None) -> dict[str, Any]:
    """Pin OpenRouter Responses requests to OpenAI, with no provider fallback."""
    routed = dict(payload)
    policy = policy or capture_admission_policy()
    extra = routed.get("extra_body")
    extra = dict(extra) if isinstance(extra, dict) else {}
    extra["provider"] = {
        "order": list(policy.provider_order), "only": list(policy.provider_only),
        "allow_fallbacks": policy.allow_fallbacks, "require_parameters": policy.require_parameters,
    }
    routed["extra_body"] = extra
    # OpenRouter's default Responses tier is the standard tier. Strip any
    # mutable caller override so neither flex nor priority can be requested.
    # OpenRouter's Responses request enum uses `auto` for the default
    # provider tier; `default` is its normalized response label.
    routed["service_tier"] = policy.request_service_tier
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


def _maximum_usd(payload: dict[str, Any], policy: AdmissionPolicy | None = None) -> Decimal:
    policy = policy or capture_admission_policy()
    _validate_bounded_text_request(payload)
    # Full serialized text bytes bound token counts, including function schemas. Above 272K
    # input tokens OpenAI applies its long-context price to the whole request.
    input_bound = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    output_bound = int(payload.get("max_output_tokens") or payload.get("max_tokens") or policy.default_output_tokens)
    if output_bound < 1 or output_bound > policy.max_output_tokens:
        raise ValueError("invalid_atlas_sol_output_limit")
    long_context = input_bound > policy.long_context_threshold_bytes
    input_rate = policy.long_context_sol_input_usd_per_million if long_context else policy.standard_sol_input_usd_per_million
    output_rate = policy.long_context_sol_output_usd_per_million if long_context else policy.standard_sol_output_usd_per_million
    tier = str(payload.get("service_tier") or "standard").lower()
    if tier not in policy.known_price_tiers:
        raise ValueError("unknown_atlas_sol_price_tier")
    # Reserve at least Standard even for Flex/Batch because the provider may
    # process a retried request at a different tier.
    tier_factor = Decimal(2) if tier in policy.high_price_tiers else Decimal(1)
    reserve = tier_factor * (Decimal(input_bound) * input_rate + Decimal(output_bound) * output_rate) / Decimal(1_000_000)
    return reserve * (policy.openrouter_margin if policy.transport == policy.openrouter_provider else Decimal(1))


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


def _estimated_usd(response: Any, reserved: Decimal, policy: AdmissionPolicy | None = None) -> Decimal:
    policy = policy or capture_admission_policy()
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
    long_context = input_count > policy.long_context_threshold_bytes
    multiplier = policy.long_context_input_multiplier if long_context else Decimal(1)
    output_multiplier = policy.long_context_output_multiplier if long_context else Decimal(1)
    model = _canonical_model(_field(response, "model", policy.sol_model), policy)
    if model == policy.luna_model:
        input_rate, cached_rate, write_rate, output_rate = (
            policy.luna_input_usd_per_million, policy.luna_cached_input_usd_per_million,
            policy.luna_cache_write_usd_per_million, policy.luna_output_usd_per_million,
        )
    else:
        input_rate, cached_rate, write_rate, output_rate = (
            policy.estimated_sol_input_usd_per_million,
            policy.estimated_sol_cached_input_usd_per_million,
            policy.estimated_sol_cache_write_usd_per_million,
            policy.estimated_sol_output_usd_per_million,
        )
    result = ((Decimal(input_count - cached - writes) * input_rate + Decimal(cached) * cached_rate + Decimal(writes) * write_rate) * multiplier
              + Decimal(output_count) * output_rate * output_multiplier) / Decimal(1_000_000)
    tier = str(_field(response, "service_tier", "standard") or "standard").lower()
    result *= (Decimal(2) if tier in policy.high_price_tiers
               else Decimal("0.5") if tier in policy.discount_price_tiers else Decimal(1))
    return result * (policy.openrouter_margin if policy.transport == policy.openrouter_provider else Decimal(1))


def _cost_components(response: Any, reserved: Decimal, policy: AdmissionPolicy | None = None) -> dict[str, Any]:
    """Return content-free OpenRouter cost components and a safe Sol estimate."""
    policy = policy or capture_admission_policy()
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
        reported_total = _estimated_usd(response, reserved, policy)
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
    try:
        resolution_guard = vars(agent).get("_atlas_resolution_guard")
    except TypeError:
        resolution_guard = None
    # Preserve legacy pass-through exactly when Atlas routing is disabled and
    # this is not a generation-guarded turn.
    if resolution_guard is None:
        enabled = (os.environ.get("ATLAS_MODEL_ROUTING_ENABLED") == "true"
                   or bool(os.environ.get("ATLAS_SOL_ACCOUNTING_SOCKET", ""))
                   or bool(os.environ.get("ATLAS_SOL_ACCOUNTING_TOKEN_FILE", "")))
        if not enabled:
            return perform(payload)
    if resolution_guard is not None:
        # The accepted generation owns the frozen policy used for the entire
        # request. Capture live globals once, compare them to that policy, then
        # route/reserve/fallback only from the prepared snapshot.
        from gateway.atlas_resolution import ResolutionDrift
        policy = resolution_guard.prepared.policy
        live_policy = capture_admission_policy()
        resolution_guard.check_policy(live_policy)
        if live_policy.public_projection() != policy.public_projection():
            raise ResolutionDrift()
    else:
        policy = capture_admission_policy()
    socket_path = policy.accounting_socket_path
    enabled = policy.enabled
    if enabled:
        agent._atlas_sol_last_call = {}

    def dispatch(outbound):
        # Optional v1 observation is best effort and precedes the generation
        # gate so that the gate check is the last operation before provider I/O.
        try:
            if enabled:
                from gateway.atlas_runtime_evidence import record_dispatch
                record_dispatch(agent, outbound)
        except Exception:
            pass  # Optional metadata must never interfere with admission.
        # This observes the final payload after any Sol->Luna rewrite. A gate
        # failure propagates and prevents the provider callable from running.
        if resolution_guard is not None:
            resolution_guard.check_dispatch(agent, outbound, policy)
        return perform(outbound)

    if not enabled:
        return dispatch(payload)

    transport = policy.transport
    requested_model = _canonical_model(payload.get("model"), policy)
    routed_payload = dict(payload)
    if transport == OPENROUTER:
        routed_payload["model"] = _routed_model(requested_model, policy.transport, policy)
        routed_payload = _pin_openrouter_provider(routed_payload, policy)
    if requested_model != policy.sol_model:
        return dispatch(routed_payload)
    reservation_id = "sol-" + uuid.uuid4().hex
    try:
        if not socket_path:
            raise ValueError("atlas_accounting_socket_missing")
        maximum = _maximum_usd(routed_payload, policy)
        token_path = Path(policy.accounting_token_file_path)
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
        routed_payload["model"] = _routed_model(policy.luna_model, transport, policy)
        routed_payload["reasoning"] = {"effort": policy.fallback_effort}
        routed_payload.pop("reasoning_effort", None)
        if transport == OPENROUTER:
            routed_payload = _pin_openrouter_provider(routed_payload, policy)
        if resolution_guard is not None:
            agent._atlas_sol_budget_fallback = resolution_guard.authorize_budget_fallback(
                policy, source_model=_routed_model(policy.sol_model, transport, policy),
                target_model=routed_payload["model"], effort=policy.fallback_effort,
            )
        agent.model = _routed_model(policy.luna_model, transport, policy)
        agent.reasoning_config = {"enabled": True, "effort": policy.fallback_effort}
        agent._atlas_sol_last_call = {"route_request_id": getattr(agent, "_atlas_route_request_id", None), "reservation_id": None, "actual_model": agent.model}
        return dispatch(routed_payload)
    try:
        response = dispatch(routed_payload)
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
        components = _cost_components(response, maximum, policy) if transport == OPENROUTER else {}
        estimated = Decimal(components["byok_total_cost_usd"]) if components else _estimated_usd(response, maximum, policy)
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
