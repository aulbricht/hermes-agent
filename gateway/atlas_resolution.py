"""Fixed Atlas API construction generations, with explicit proof boundaries.

Only projections of actual captured construction/initialization inputs are
published. Native/auxiliary resolution, source-byte and external BYOK proofs are
outside this component. Probe reads never invoke a loader or provider resolver.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import datetime as dt
import hmac
import inspect
import json
import math
import threading
from types import MappingProxyType

from gateway.atlas_runtime_evidence import digest, process_identity

PROTOCOL = "atlas.hermes-resolution-generation.v2"
MODELS = {"openai/gpt-6-luna", "openai/gpt-6.1-sol"}
TOOLS = {"web", "acre-filemaker", "atlas_vault"}
ALIASES = {"default", "atlas-luna", "atlas-sol"}
PHASE2_WORKER_SESSION_KEY = "atlas-phase2-ownership-v1"
_DEFAULT_FIELDS = ("tool_delay", "disabled_toolsets", "providers_allowed", "providers_ignored",
                   "providers_order", "provider_sort", "provider_require_parameters",
                   "provider_data_collection", "service_tier", "request_overrides")
_RESOLUTION_KWARGS = frozenset({
    "model", "provider", "base_url", "api_key", "credential_pool", "api_mode",
    "enabled_toolsets", "disabled_toolsets", "max_tokens", "max_iterations",
    "reasoning_config", "fallback_model", "providers_allowed", "providers_ignored",
    "providers_order", "provider_sort", "provider_require_parameters",
    "provider_data_collection", "service_tier", "request_overrides", "tool_delay",
    "command", "args", "acp_command", "acp_args",
})
_AGENT_DEFAULT_ATTRS = {
    "tool_delay": "tool_delay", "disabled_toolsets": "disabled_toolsets",
    "providers_allowed": "providers_allowed", "providers_ignored": "providers_ignored",
    "providers_order": "providers_order", "provider_sort": "provider_sort",
    "provider_require_parameters": "provider_require_parameters",
    "provider_data_collection": "provider_data_collection", "service_tier": "service_tier",
    "request_overrides": "request_overrides",
}


class ResolutionDrift(RuntimeError):
    def __init__(self):
        super().__init__("atlas_resolution_generation_drift")


class ResolutionUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("atlas_resolution_generation_unavailable")


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def phase2_tool_free_binding(role, binding_hash):
    """Owner-defined worker policy, never selected by request tool controls."""
    return role == "main" and binding_hash == digest(["main", PHASE2_WORKER_SESSION_KEY])


def _responses_normalized_tools(tools, model):
    """Use Hermes's real Responses adapter to derive the allowed tool subset."""
    from agent.codex_responses_adapter import _preflight_codex_api_kwargs, _responses_tools

    converted = _responses_tools(tools)
    if converted is None:
        return []
    normalized = _preflight_codex_api_kwargs({
        "model": model, "instructions": "", "input": [],
        "tools": converted, "store": False,
    })
    return normalized.get("tools") or []


def constructor_defaults(factory):
    """Read selected real defaults, not a copied model/initializer algorithm."""
    parameters = inspect.signature(factory).parameters
    values = {}
    for name in _DEFAULT_FIELDS:
        parameter = parameters.get(name)
        if parameter is None or parameter.default is inspect.Parameter.empty:
            raise ResolutionUnavailable()
        value = parameter.default
        if name == "tool_delay":
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 60:
                raise ResolutionUnavailable()
        elif name == "provider_require_parameters":
            if type(value) is not bool:
                raise ResolutionUnavailable()
        elif value is not None:
            # Expanded defaults need owner review; never echo arbitrary objects
            # or credential-bearing default dictionaries.
            raise ResolutionUnavailable()
        values[name] = value
    return values


def _validate_policy(policy):
    value = policy.public_projection()
    if (policy.sol_model != "gpt-6.1-sol" or policy.luna_model != "gpt-6-luna"
            or policy.openrouter_provider != "openrouter"
            or policy.openrouter_base_url != "https://openrouter.ai/api/v1"
            or policy.openrouter_model_prefix != "openai/"
            or policy.transport != "openrouter" or not policy.enabled
            or not policy.accounting_socket_configured or not policy.accounting_token_file_configured
            or policy.provider_order != ("openai",) or policy.provider_only != ("openai",)
            or policy.allow_fallbacks is not False or policy.require_parameters is not True
            or policy.request_service_tier != "auto" or policy.pricing_tier != "standard"
            or policy.default_output_tokens != 128_000 or policy.max_output_tokens != 128_000
            or policy.long_context_threshold_bytes != 272_000
            or str(policy.standard_sol_input_usd_per_million) != "2.50"
            or str(policy.standard_sol_output_usd_per_million) != "10"
            or str(policy.long_context_sol_input_usd_per_million) != "5"
            or str(policy.long_context_sol_output_usd_per_million) != "15"
            or str(policy.estimated_sol_input_usd_per_million) != "2"
            or str(policy.estimated_sol_cached_input_usd_per_million) != "0.10"
            or str(policy.estimated_sol_cache_write_usd_per_million) != "2.50"
            or str(policy.estimated_sol_output_usd_per_million) != "10"
            or str(policy.luna_input_usd_per_million) != "0.10"
            or str(policy.luna_cached_input_usd_per_million) != "0.01"
            or str(policy.luna_cache_write_usd_per_million) != "0.125"
            or str(policy.luna_output_usd_per_million) != "0.50"
            or str(policy.long_context_input_multiplier) != "2"
            or str(policy.long_context_output_multiplier) != "1.5"
            or policy.high_price_tiers != ("fast", "priority")
            or policy.discount_price_tiers != ("flex", "batch")
            or policy.known_price_tiers != ("standard", "default", "auto", "flex", "batch", "fast", "priority")
            or str(policy.openrouter_margin) != "1.05"
            or policy.fallback_effort != "xhigh"
            or policy.budget_enforcement != "external_accounting_reservation_service"):
        raise ResolutionUnavailable()
    return value


def semantic_projection(kwargs, snapshot, policy, defaults, alias, binding_hash, role, override):
    """Project the very inputs used by construction; do not resolve settings."""
    if role not in {"main", "support"} or alias not in ALIASES:
        raise ResolutionUnavailable()
    if (type(binding_hash) is not str or len(binding_hash) != 64
            or any(ch not in "0123456789abcdef" for ch in binding_hash)):
        raise ResolutionUnavailable()
    if (kwargs.get("model") not in MODELS or kwargs.get("provider") != "openrouter"
            or kwargs.get("base_url") != "https://openrouter.ai/api/v1"
            or kwargs.get("api_mode") != "codex_responses"):
        raise ResolutionUnavailable()
    if ((alias == "atlas-sol" and kwargs.get("model") != "openai/gpt-6.1-sol")
            or (alias == "atlas-luna" and kwargs.get("model") != "openai/gpt-6-luna")):
        raise ResolutionUnavailable()
    if set(kwargs) - _RESOLUTION_KWARGS:
        raise ResolutionUnavailable()
    request_overrides = kwargs.get("request_overrides")
    if request_overrides is not None and type(request_overrides) is not dict:
        raise ResolutionUnavailable()
    if request_overrides:
        # This surface can inject arbitrary provider request fields. Until a
        # reviewed allowlist exists, guarded Atlas requests require none.
        raise ResolutionUnavailable()
    if kwargs.get("api_key") is not None and type(kwargs.get("api_key")) is not str:
        raise ResolutionUnavailable()
    if kwargs.get("credential_pool") is not None and isinstance(kwargs.get("credential_pool"), (dict, list, str, int, float, bool)):
        raise ResolutionUnavailable()
    if kwargs.get("command") not in (None, "") or kwargs.get("acp_command") not in (None, ""):
        raise ResolutionUnavailable()
    if kwargs.get("args") not in (None, [], ()) or kwargs.get("acp_args") not in (None, [], ()):
        raise ResolutionUnavailable()
    try:
        json.dumps({k: v for k, v in kwargs.items() if k not in {"api_key", "credential_pool"}},
                   ensure_ascii=False, allow_nan=False)
        json.dumps(defaults, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise ResolutionUnavailable() from None
    toolsets = kwargs.get("enabled_toolsets")
    if type(toolsets) is not list or any(x not in TOOLS for x in toolsets):
        raise ResolutionUnavailable()
    reasoning = kwargs.get("reasoning_config")
    if (type(reasoning) is not dict or set(reasoning) - {"enabled", "effort"}
            or reasoning.get("effort") not in {"none", "low", "medium", "high", "xhigh", "max"}):
        raise ResolutionUnavailable()
    iterations = kwargs.get("max_iterations")
    cap = kwargs.get("max_tokens")
    if type(iterations) is not int or not 1 <= iterations <= 90 or (cap is not None and (type(cap) is not int or not 1 <= cap <= 128000)):
        raise ResolutionUnavailable()
    config = snapshot.config_copy()
    model_config = config.get("model") or {}
    if type(model_config) is not dict or config.get("custom_providers"):
        raise ResolutionUnavailable()
    limits = {name: model_config.get(name) for name in ("context_length", "max_tokens")}
    if any(v is not None and (type(v) is not int or not 1 <= v <= 1000000) for v in limits.values()):
        raise ResolutionUnavailable()
    mcp = config.get("mcp_servers") or {}
    if type(mcp) is not dict or set(mcp) - {"acre-filemaker", "atlas_vault"}:
        raise ResolutionUnavailable()
    # Configured fallback is captured, never inferred from an empty recent
    # dispatch. Nonempty credential-bearing provider chains need a later slice.
    if kwargs.get("fallback_model"):
        raise ResolutionUnavailable()
    if role == "support" and (toolsets or mcp or snapshot.tools_copy()):
        raise ResolutionUnavailable()
    tools = snapshot.tools_copy()
    tool_free = phase2_tool_free_binding(role, binding_hash)
    if tool_free and (toolsets or tools):
        raise ResolutionUnavailable()
    names = []
    for tool in tools:
        try:
            name = tool["function"]["name"]
        except (KeyError, TypeError):
            raise ResolutionUnavailable() from None
        if type(name) is not str or len(name) > 128 or not name.replace("_", "").isalnum():
            raise ResolutionUnavailable()
        names.append(name)
    if override:
        # No raw session/model override values are exported. A guard cannot
        # bless the current API path's route-skipping behavior as an applied
        # session override. Preserve normal behavior and block guarded use.
        raise ResolutionUnavailable()
    responses_tools = _responses_normalized_tools(tools, kwargs["model"])
    if any(not isinstance(tool, dict) or tool.get("type") != "function" for tool in responses_tools):
        raise ResolutionUnavailable()
    return {"role": role, "alias": alias, "binding_sha256": binding_hash,
            "tool_access": "none" if tool_free else "captured_platform_tools",
            # The factory receives the resolved effective value. Preserve an
            # explicit None when supplied, and fill only omitted arguments
            # from the inspected constructor defaults.
            "constructor": {name: copy.deepcopy(kwargs.get(name, defaults.get(name))) for name in
                            ("model", "provider", "base_url", "api_mode", "max_tokens",
                             "reasoning_config", "enabled_toolsets", "disabled_toolsets",
                             "max_iterations", "tool_delay", "providers_allowed", "providers_ignored",
                             "providers_order", "provider_sort", "provider_require_parameters",
                             "provider_data_collection", "service_tier", "request_overrides")},
            "constructor_defaults": copy.deepcopy(defaults),
            "configured_limits": limits, "omitted_output_limit": cap is None and limits["max_tokens"] is None,
            "configured_mcp_names": sorted(mcp), "loaded_tool_names": sorted(names),
            "tool_definitions_sha256": digest(tools), "tool_registry_generation": snapshot.tool_generation,
            "responses_tool_definitions_sha256": digest(responses_tools),
            "session_override": "absent", "configured_fallback": "empty",
            "admission_policy": _validate_policy(policy)}


@dataclass(frozen=True, repr=False)
class PreparedTurn:
    _kwargs: object = field(repr=False)
    initialization: object = field(repr=False)
    policy: object = field(repr=False)
    _responses_tools_json: str = field(repr=False)
    semantic_json: str
    dependency_json: str
    scope: tuple

    @classmethod
    def capture(cls, kwargs, initialization, policy, defaults, alias, binding_hash, role, override, dependencies):
        spec = semantic_projection(kwargs, initialization, policy, defaults, alias, binding_hash, role, override)
        # Keep only the reviewed resolution inputs. Opaque runtime objects are
        # preserved by identity; request callbacks/history/DB objects are never
        # copied, stringified, represented or stored here.
        private = {k: copy.deepcopy(v) for k, v in defaults.items()}
        for key, value in kwargs.items():
            private[key] = value if key in {"credential_pool", "api_key"} else copy.deepcopy(value)
        responses_tools = _responses_normalized_tools(initialization.tools_copy(), spec["constructor"]["model"])
        return cls(MappingProxyType(private), initialization, policy, json.dumps(responses_tools, sort_keys=True, separators=(",", ":")),
                   json.dumps(spec, sort_keys=True, separators=(",", ":")),
                   json.dumps(dependencies, sort_keys=True, separators=(",", ":")),
                   (role, alias, binding_hash))

    def factory_kwargs(self):
        return {k: (v if k == "credential_pool" or k == "api_key" else copy.deepcopy(v)) for k, v in self._kwargs.items()}

    def credentials_equal(self, other):
        a, b = self._kwargs.get("api_key"), other._kwargs.get("api_key")
        if type(a) is str and type(b) is str:
            return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8")) and self._kwargs.get("credential_pool") is other._kwargs.get("credential_pool")
        return a is b and self._kwargs.get("credential_pool") is other._kwargs.get("credential_pool")

    def private_config_equal(self, other):
        return hmac.compare_digest(self.initialization._config_json.encode("utf-8"), other.initialization._config_json.encode("utf-8"))


class ResolutionStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._records = {}
        self._counter = 0
        self.identity = process_identity()

    def publish(self, prepared):
        if self.identity["state"] != "linux_proc_identity":
            raise ResolutionUnavailable()
        with self._lock:
            old = self._records.get(prepared.scope)
            if (old and old["prepared"].semantic_json == prepared.semantic_json
                    and old["prepared"].dependency_json == prepared.dependency_json
                    and old["prepared"].credentials_equal(prepared)
                    and old["prepared"].private_config_equal(prepared)):
                return old["generation_id"]
            self._counter += 1
            generation_id = digest({"process": self.identity, "generation": self._counter,
                                    "semantic": json.loads(prepared.semantic_json), "dependencies": json.loads(prepared.dependency_json)})
            self._records[prepared.scope] = {"generation_id": generation_id, "generation": self._counter,
                                           "resolved_at_utc": _now(), "prepared": prepared,
                                           "construction": None}
            if len(self._records) > 32:
                del self._records[next(iter(self._records))]
            return generation_id

    def discard(self, scope):
        """Owner startup invalidation; retain the monotonic generation counter."""
        with self._lock:
            self._records.pop(scope, None)

    def accept(self, expected, prepared, live_dependencies):
        with self._lock:
            old = self._records.get(prepared.scope)
            if (old is None or old["generation_id"] != expected
                    or old["prepared"].semantic_json != prepared.semantic_json
                    or old["prepared"].dependency_json != prepared.dependency_json
                    or not old["prepared"].credentials_equal(prepared)
                    or not old["prepared"].private_config_equal(prepared)
                    or json.loads(prepared.dependency_json) != live_dependencies()):
                raise ResolutionDrift()
            # Construction must consume the exact immutable private snapshot
            # originally published, not a freshly recaptured equivalent copy.
            accepted_prepared = old["prepared"]
        return DispatchGuard(self, expected, accepted_prepared, live_dependencies)

    def _record_construction(self, expected, prepared, state):
        with self._lock:
            record = self._records.get(prepared.scope)
            if record is None or record["generation_id"] != expected:
                raise ResolutionDrift()
            if record["prepared"] is not prepared:
                raise ResolutionDrift()
            previous = record.get("construction")
            if previous is not None and previous != state:
                raise ResolutionDrift()
            record["construction"] = copy.deepcopy(state)

    def proof(self, nonce, live_dependencies):
        with self._lock:
            records = [dict(record) for record in self._records.values()]
        entries = []
        for record in records:
            prepared = record["prepared"]
            try:
                current = live_dependencies(prepared.scope)
                state = "ready_conditioned_on_admission" if current == json.loads(prepared.dependency_json) else "invalidated"
            except Exception:
                state = "invalidated"
            construction = record.get("construction")
            entries.append({k: record[k] for k in ("generation_id", "generation", "resolved_at_utc")} |
                           {"state": state, "construction_state": "observed" if construction is not None else "not_observed",
                            "construction": construction,
                            "semantic": json.loads(prepared.semantic_json),
                            "semantic_sha256": digest(json.loads(prepared.semantic_json))})
        return {"protocol": PROTOCOL, "nonce": nonce, "observed_at_utc": _now(), "process": self.identity,
                "resolutions": entries, "state": "available" if entries else "unavailable",
                "scope": "fixed_atlas_api_construction_and_admission",
                "loaded_source_sha256": None, "external_byok_state": "not_observed",
                "accounting_policy_generation": None, "native_auxiliary_coverage": False}


class DispatchGuard:
    def __init__(self, store, expected, prepared, live_dependencies):
        self.store, self.expected, self.prepared, self.live_dependencies = store, expected, prepared, live_dependencies
        self.spec = json.loads(prepared.semantic_json)
        self._tool_free = self.spec["tool_access"] == "none"
        self._constructed_state = None
        self._fallback_authorization = None
        self.dispatch_count = 0
        self._tool_execution_denied = False

    @staticmethod
    def _agent_fields(agent):
        fields = dict(vars(agent))
        # AIAgent stores this property's backing field; lightweight callers may
        # expose a plain base_url attribute. Read neither arbitrary properties
        # nor a duplicated endpoint resolver.
        if "base_url" not in fields:
            fields["base_url"] = fields.get("_base_url")
        return fields

    def _check_current(self):
        self.check_completion()
        self.store.accept(self.expected, self.prepared, self.live_dependencies)

    def check_tool_execution(self, agent):
        """Reject even fabricated provider tool calls for the fixed worker."""
        if self._tool_free:
            self._tool_execution_denied = True
            raise ResolutionDrift()

    def check_completion(self):
        # Conversation error handling cannot convert a denied tool attempt
        # into an accepted fixed-worker answer or another provider dispatch.
        if self._tool_execution_denied:
            raise ResolutionDrift()

    def check_policy(self, policy):
        self._check_current()
        if _validate_policy(policy) != self.spec["admission_policy"]:
            raise ResolutionDrift()

    def _check_tool_free_surface(self, fields):
        if self._tool_free:
            if (fields.get("tools") != [] or fields.get("enabled_toolsets") != []
                    or type(fields.get("valid_tool_names")) is not set
                    or fields["valid_tool_names"]):
                raise ResolutionDrift()

    def check_constructed(self, agent):
        self._check_current()
        fields = self._agent_fields(agent)
        self._check_tool_free_surface(fields)
        intended = self.spec["constructor"]
        for name in ("model", "provider", "base_url", "api_mode", "reasoning_config",
                     "enabled_toolsets", "disabled_toolsets", "max_iterations"):
            if fields.get(name) != intended[name]:
                raise ResolutionDrift()
        self._check_private_credentials(fields)
        if digest(fields.get("tools")) != self.spec["tool_definitions_sha256"] or fields.get("_fallback_activated", False):
            raise ResolutionDrift()
        if fields.get("_tool_snapshot_generation") != self.prepared.initialization.tool_generation:
            raise ResolutionDrift()
        expected_cap = intended["max_tokens"] or self.spec["configured_limits"]["max_tokens"]
        if fields.get("max_tokens") != expected_cap:
            raise ResolutionDrift()
        defaults_and_inputs = {**self.spec["constructor_defaults"], **intended}
        for input_name, attr_name in _AGENT_DEFAULT_ATTRS.items():
            expected_value = defaults_and_inputs.get(input_name)
            if input_name == "request_overrides" and expected_value is None:
                expected_value = {}
            if fields.get(attr_name) != expected_value:
                raise ResolutionDrift()
        configured_context = self.spec["configured_limits"]["context_length"]
        if fields.get("_config_context_length") != configured_context:
            raise ResolutionDrift()
        compressor = fields.get("context_compressor")
        context_length = getattr(compressor, "context_length", None)
        if type(context_length) is not int or not 1 <= context_length <= 1_000_000:
            raise ResolutionDrift()
        defaults_state = {attr: fields.get(attr) for attr in _AGENT_DEFAULT_ATTRS.values()}
        # The first construction check freezes effective defaults and runtime
        # limits. Subsequent checks and each dispatch must preserve them.
        state = {
            "api_mode": fields.get("api_mode"), "context_length": context_length,
            "configured_context_length": fields.get("_config_context_length"),
            "context_length_evidence": (
                "configured_limit_verified_at_construction" if configured_context is not None
                else "construction_only_context_compressor_observation"
            ),
            "max_tokens": fields.get("max_tokens"),
            "tool_generation": fields.get("_tool_snapshot_generation"),
            "tool_definitions_sha256": digest(fields.get("tools")),
            "model": fields.get("model"), "provider": fields.get("provider"),
            "base_url": fields.get("base_url"),
            "reasoning_config": copy.deepcopy(fields.get("reasoning_config")),
            "enabled_toolsets": copy.deepcopy(fields.get("enabled_toolsets")),
            "disabled_toolsets": copy.deepcopy(fields.get("disabled_toolsets")),
            "max_iterations": fields.get("max_iterations"),
            "defaults": defaults_state,
        }
        if self._constructed_state is None:
            self._constructed_state = state
        elif state != self._constructed_state:
            raise ResolutionDrift()
        self.store._record_construction(self.expected, self.prepared, state)

    def _check_private_credentials(self, fields):
        expected_key = self.prepared._kwargs.get("api_key")
        actual_key = fields.get("api_key")
        if type(expected_key) is not str or type(actual_key) is not str:
            raise ResolutionDrift()
        if not hmac.compare_digest(expected_key.encode("utf-8"), actual_key.encode("utf-8")):
            raise ResolutionDrift()
        expected_pool = self.prepared._kwargs.get("credential_pool")
        if fields.get("_credential_pool") is not expected_pool:
            raise ResolutionDrift()
        client_kwargs = fields.get("_client_kwargs")
        if client_kwargs is not None:
            if type(client_kwargs) is not dict:
                raise ResolutionDrift()
            client_key = client_kwargs.get("api_key")
            client_base = client_kwargs.get("base_url")
            if (type(client_key) is not str or type(client_base) is not str
                    or not hmac.compare_digest(expected_key.encode("utf-8"), client_key.encode("utf-8"))
                    or client_base.rstrip("/") != str(self.prepared._kwargs.get("base_url") or "").rstrip("/")):
                raise ResolutionDrift()

    def authorize_budget_fallback(self, policy, source_model, target_model, effort):
        """Authorize exactly the gate's Sol-to-Luna fallback for this turn."""
        self.check_policy(policy)
        intended = self.spec["constructor"]
        if (intended["model"] != "openai/" + policy.sol_model
                or source_model != intended["model"]
                or target_model != "openai/" + policy.luna_model
                or effort != policy.fallback_effort):
            raise ResolutionDrift()
        if self._fallback_authorization is None:
            self._fallback_authorization = {
                "token": object(), "source_model": source_model,
                "target_model": target_model, "effort": effort,
                "policy": policy.public_projection(),
            }
        marker = self._fallback_authorization
        if marker["policy"] != policy.public_projection():
            raise ResolutionDrift()
        return marker["token"]

    def check_dispatch(self, agent, outbound, policy):
        self.check_policy(policy)
        if self._constructed_state is None:
            raise ResolutionUnavailable()
        fields = self._agent_fields(agent)
        self._check_tool_free_surface(fields)
        intended = self.spec["constructor"]
        actual = outbound.get("model")
        reasoning = outbound.get("reasoning")
        if reasoning is not None and type(reasoning) is not dict:
            raise ResolutionDrift()
        effort = (reasoning or {}).get("effort") or outbound.get("reasoning_effort")
        normal_route = (actual == intended["model"]
                        and effort == intended["reasoning_config"]["effort"])
        fallback = (actual == "openai/" + policy.luna_model
                    and effort == policy.fallback_effort
                    and self._fallback_authorization is not None
                    and fields.get("_atlas_sol_budget_fallback") is self._fallback_authorization["token"]
                    and self._fallback_authorization["source_model"] == intended["model"]
                    and self._fallback_authorization["target_model"] == actual
                    and self._fallback_authorization["effort"] == effort
                    and self._fallback_authorization["policy"] == policy.public_projection())
        if (not (normal_route or fallback) or fields.get("model") != actual
                or fields.get("provider") != intended["provider"] or fields.get("base_url") != intended["base_url"]):
            raise ResolutionDrift()
        self._check_private_credentials(fields)
        if fields.get("_fallback_activated", False):
            raise ResolutionDrift()
        expected_reasoning_config = (self._constructed_state["reasoning_config"] if normal_route
                                     else {"enabled": True, "effort": policy.fallback_effort})
        if fields.get("reasoning_config") != expected_reasoning_config:
            raise ResolutionDrift()
        if (fields.get("api_mode") != self._constructed_state["api_mode"]
                or fields.get("_config_context_length") != self._constructed_state["configured_context_length"]
                or getattr(fields.get("context_compressor"), "context_length", None) != self._constructed_state["context_length"]
                or fields.get("max_tokens") != self._constructed_state["max_tokens"]
                or fields.get("_tool_snapshot_generation") != self._constructed_state["tool_generation"]
                or digest(fields.get("tools")) != self._constructed_state["tool_definitions_sha256"]
                or fields.get("max_iterations") != self._constructed_state["max_iterations"]
                or fields.get("enabled_toolsets") != self._constructed_state["enabled_toolsets"]
                or fields.get("disabled_toolsets") != self._constructed_state["disabled_toolsets"]
                or any(fields.get(name) != value for name, value in self._constructed_state["defaults"].items())):
            raise ResolutionDrift()
        expected_cap = intended["max_tokens"] or self.spec["configured_limits"]["max_tokens"]
        if outbound.get("max_output_tokens") != expected_cap:
            raise ResolutionDrift()
        extra_body = outbound.get("extra_body")
        if extra_body is not None and type(extra_body) is not dict:
            raise ResolutionDrift()
        if self._tool_free:
            for body in (outbound, extra_body or {}):
                if (body.get("tools") not in (None, [])
                        or body.get("functions") not in (None, [])
                        or body.get("tool_choice") not in (None, "none")
                        or body.get("function_call") not in (None, "none")):
                    raise ResolutionDrift()
        provider = (extra_body or {}).get("provider")
        if provider != {"order": list(policy.provider_order), "only": list(policy.provider_only),
                       "allow_fallbacks": policy.allow_fallbacks, "require_parameters": policy.require_parameters} or outbound.get("service_tier") != policy.request_service_tier:
            raise ResolutionDrift()
        actual_tools = outbound.get("tools")
        if actual_tools is None:
            actual_tools = []
        if type(actual_tools) is not list:
            raise ResolutionDrift()
        try:
            allowed_tools = json.loads(self.prepared._responses_tools_json)
            if digest(allowed_tools) != self.spec["responses_tool_definitions_sha256"]:
                raise ResolutionDrift()
            permitted = {json.dumps(tool, sort_keys=True, separators=(",", ":")) for tool in allowed_tools}
            supplied = [json.dumps(tool, sort_keys=True, separators=(",", ":")) for tool in actual_tools]
        except (TypeError, ValueError):
            raise ResolutionDrift() from None
        if len(set(supplied)) != len(supplied) or any(tool not in permitted for tool in supplied):
            raise ResolutionDrift()
        if outbound.get("temperature") is not None:
            raise ResolutionDrift()
        self.dispatch_count += 1
