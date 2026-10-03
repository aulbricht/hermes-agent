from decimal import Decimal
from types import SimpleNamespace

from agent import atlas_sol_budget as budget


def test_sol_call_reserves_before_dispatch_and_settles_estimated_usage(monkeypatch):
    events = []

    def post(_socket, _token, path, body):
        events.append((path, body))
        return {"status": "reserved"} if path.endswith("reservations") else {"status": "settled_estimated"}

    monkeypatch.setenv("ATLAS_SOL_ACCOUNTING_SOCKET", "/tmp/test-accounting.sock")
    monkeypatch.setenv("ATLAS_SOL_ACCOUNTING_TOKEN_FILE", "/tmp/test-accounting-token")
    monkeypatch.setattr(budget, "_read_token", lambda _path: "fixture-token")
    monkeypatch.setattr(budget, "_post", post)
    agent = SimpleNamespace(model=budget.SOL, reasoning_effort="low")
    response = SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=25, input_tokens_details=None))

    def perform(payload):
        assert events[0][0].endswith("reservations")
        assert payload["model"] == budget.SOL
        return response

    assert budget.admitted_call(agent, {"model": budget.SOL, "max_output_tokens": 500, "input": "hello"}, perform) is response
    assert events[-1][0].endswith("settlements")
    assert events[-1][1]["estimated"] is True
    assert Decimal(events[-1][1]["settled_usd"]) <= Decimal(events[0][1]["max_usd"])


def test_openrouter_sol_admission_pins_openai_and_accepts_observed_zero_fee(monkeypatch):
    events = []

    def post(_socket, _token, path, body):
        events.append((path, body))
        return {"status": "reserved"} if path.endswith("reservations") else {"status": "settled"}

    monkeypatch.setenv("ATLAS_MODEL_ROUTING_ENABLED", "true")
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_TRANSPORT", "openrouter")
    monkeypatch.setenv("ATLAS_SOL_ACCOUNTING_SOCKET", "/tmp/test-accounting.sock")
    monkeypatch.setenv("ATLAS_SOL_ACCOUNTING_TOKEN_FILE", "/tmp/test-accounting-token")
    monkeypatch.setattr(budget, "_read_token", lambda _path: "fixture-token")
    monkeypatch.setattr(budget, "_post", post)
    response = SimpleNamespace(
        id="generation-sol", model="openai/gpt-6.1-sol", service_tier="default",
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=25, input_tokens_details=None,
            cost=0, cost_details=SimpleNamespace(upstream_inference_cost="0.00045"), is_byok=True,
        ),
    )
    agent = SimpleNamespace(model=budget.SOL)

    def perform(payload):
        assert events[0][0].endswith("reservations")
        assert payload["model"] == "openai/gpt-6.1-sol"
        assert payload["service_tier"] == "auto"
        assert payload["extra_body"]["provider"] == {
            "order": ["openai"], "only": ["openai"],
            "allow_fallbacks": False, "require_parameters": True,
        }
        return response

    budget.admitted_call(agent, {"model": budget.SOL, "max_output_tokens": 500, "input": "hello"}, perform)
    reserve = events[0][1]
    settle = events[1][1]
    assert Decimal(reserve["max_usd"]) > 0
    assert settle["settled_usd"] == "0.00045"
    assert settle["estimated"] is False
    assert agent._atlas_sol_last_call["actual_cost_usd"] == "0"


def test_openrouter_sol_missing_cost_component_settles_estimated_ceiling(monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_TRANSPORT", "openrouter")
    response = {
        "model": "openai/gpt-6.1-sol", "service_tier": "default",
        "usage": {"input_tokens": 100, "output_tokens": 25, "cost": 0, "is_byok": True},
    }
    parts = budget._cost_components(response, Decimal("1"))
    expected = budget._estimated_usd(response, Decimal("1"))
    assert parts["actual_cost_estimated"] is True
    assert Decimal(parts["byok_total_cost_usd"]) == expected
    assert expected > 0


def test_responses_input_cache_counts_are_excluded_from_canonical_input(monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_TRANSPORT", "openrouter")
    fields = budget.openai_usage_fields({
        "usage": {"input_tokens": 100, "output_tokens": 25,
                  "input_tokens_details": {"cached_tokens": 10, "cache_write_tokens": 20}},
        "service_tier": "default",
    })
    assert fields["input_tokens"] == 70
    assert fields["input_tokens_total"] == 100
    assert fields["cache_read_tokens"] == 10
    assert fields["cache_write_tokens"] == 20


def test_denied_sol_call_falls_back_to_luna(monkeypatch):
    monkeypatch.setenv("ATLAS_SOL_ACCOUNTING_SOCKET", "/tmp/test-accounting.sock")
    monkeypatch.setenv("ATLAS_SOL_ACCOUNTING_TOKEN_FILE", "/tmp/test-accounting-token")
    monkeypatch.setattr(budget, "_read_token", lambda _path: "fixture-token")
    monkeypatch.setattr(budget, "_post", lambda *_args: {"status": "denied"})
    agent = SimpleNamespace(model=budget.SOL, reasoning_effort="low")
    seen = {}

    def perform(payload):
        seen.update(payload)
        return "ok"

    assert budget.admitted_call(agent, {"model": budget.SOL, "max_output_tokens": 500}, perform) == "ok"
    assert seen["model"] == budget.LUNA
    assert seen["reasoning"]["effort"] == "xhigh"
    assert agent.model == budget.LUNA

    assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}


def test_unbounded_sol_inputs_are_rejected():
    import pytest
    for extra in (
        {"previous_response_id": "resp_remote"},
        {"tools": [{"type": "web_search"}]},
        {"input": [{"type": "input_image", "image_url": "https://example.com/a.png"}]},
        {"input": [{"type": "input_file", "file_id": "file_remote"}]},
    ):
        with pytest.raises(ValueError):
            budget._maximum_usd({"model": budget.SOL, "max_output_tokens": 500, **extra})


def test_transport_and_settlement_failure_preserves_original_error(monkeypatch):
    import pytest
    monkeypatch.setenv("ATLAS_SOL_ACCOUNTING_SOCKET", "/tmp/test-accounting.sock")
    monkeypatch.setenv("ATLAS_SOL_ACCOUNTING_TOKEN_FILE", "/tmp/test-accounting-token")
    monkeypatch.setattr(budget, "_read_token", lambda _path: "fixture-token")
    def post(_socket, _token, path, body):
        if path.endswith("reservations"):
            return {"status": "reserved"}
        raise RuntimeError("accounting unavailable")
    monkeypatch.setattr(budget, "_post", post)
    def perform(payload):
        raise ValueError("provider interrupted")
    with pytest.raises(ValueError, match="provider interrupted"):
        budget.admitted_call(SimpleNamespace(model=budget.SOL), {"model": budget.SOL, "max_output_tokens": 500}, perform)


def test_usage_dict_preserves_cache_write_and_priority_pricing():
    response = {"usage": {"input_tokens": 100, "output_tokens": 25, "input_tokens_details": {"cached_tokens": 10, "cache_write_tokens": 20}}, "service_tier": "priority"}
    assert budget._estimated_usd(response, Decimal(1)) == Decimal("0.000882")


def test_codex_iteration_summary_and_retry_each_use_admission(monkeypatch):
    from agent.chat_completion_helpers import handle_max_iterations

    admitted = []
    responses = [SimpleNamespace(content=""), SimpleNamespace(content="final")]

    def admitted_call(agent, payload, perform):
        admitted.append(payload)
        return perform(payload)

    class Transport:
        @staticmethod
        def normalize_response(response):
            return SimpleNamespace(content=response.content)

    agent = SimpleNamespace(
        max_iterations=1, api_mode="codex_responses", model=budget.SOL,
        provider="openai-api", base_url="https://api.openai.com/v1",
        _base_url_lower="https://api.openai.com/v1", reasoning_config=None,
        max_tokens=100, cached_system_prompt="", ephemeral_system_prompt="",
        _cached_system_prompt="",
        prefill_messages=[],
        _should_sanitize_tool_calls=lambda: False,
        _copy_reasoning_content_for_api=lambda _msg, _api_msg: None,
        _sanitize_api_messages=lambda msgs: msgs,
        _drop_thinking_only_and_merge_users=lambda msgs: msgs,
        _supports_reasoning_extra_body=lambda: False,
        _is_openrouter_url=lambda: False,
        _build_api_kwargs=lambda msgs: {
            "model": budget.SOL, "input": msgs, "max_output_tokens": 100,
        },
        _get_transport=lambda: Transport(),
    )

    def run_stream(payload):
        assert payload["model"] == budget.SOL
        return responses.pop(0)

    agent._run_codex_stream = run_stream
    monkeypatch.setattr("agent.atlas_sol_budget.admitted_call", admitted_call)
    messages = [{"role": "user", "content": "summarize"}]

    assert handle_max_iterations(agent, messages, 1) == "final"
    assert len(admitted) == 2
    assert all(call["model"] == budget.SOL for call in admitted)


def test_sol_stream_transport_failure_does_not_hide_billable_retry(monkeypatch):
    import httpx
    import pytest
    from agent.codex_runtime import run_codex_stream

    monkeypatch.setenv("ATLAS_SOL_ACCOUNTING_SOCKET", "/tmp/accounting.sock")
    calls = []

    class Responses:
        def create(self, **_kwargs):
            calls.append(1)
            raise httpx.ConnectError("connection lost")

    client = SimpleNamespace(responses=Responses())
    agent = SimpleNamespace(
        _interrupt_requested=False,
        _ensure_primary_openai_client=lambda **_kwargs: client,
        _fire_stream_delta=lambda _text: None,
        _fire_reasoning_delta=lambda _text: None,
        _touch_activity=lambda _text: None,
        _client_log_context=lambda: "test",
    )
    with pytest.raises(httpx.ConnectError):
        run_codex_stream(agent, {"model": budget.SOL}, client=client)
    assert len(calls) == 1


def test_accounting_token_rejects_group_writable_file(tmp_path):
    import pytest

    token_file = tmp_path / "accounting-token"
    token_file.write_text("fixture-only\n", encoding="utf-8")
    token_file.chmod(0o660)
    with pytest.raises(ValueError):
        budget._read_token(token_file)


def test_luna_summary_usage_is_included_in_turn_receipts(monkeypatch):
    from agent.chat_completion_helpers import _record_atlas_summary_usage
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_ENABLED", "true")
    agent = SimpleNamespace(model=budget.LUNA, session_usage_calls=[], _atlas_route_request_id="turn-fixture", _atlas_sol_last_call={})
    response = SimpleNamespace(id="resp-summary", model=budget.LUNA, service_tier="default", usage={"input_tokens": 100, "output_tokens": 50, "input_tokens_details": {"cached_tokens": 80}, "output_tokens_details": {"reasoning_tokens": 20}})
    _record_atlas_summary_usage(agent, response)
    call = agent.session_usage_calls[0]
    assert call["generation_id"] == "resp-summary"
    assert call["provider"] == "openai" and call["route_request_id"] == "turn-fixture"
    assert call["input_tokens"] == 20 and call["input_tokens_total"] == 100 and call["output_tokens"] == 50
    assert call["cache_read_tokens"] == 80 and call["reasoning_tokens"] == 20


def test_admission_policy_snapshot_is_frozen_and_projection_excludes_paths(monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_ENABLED", "true")
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_TRANSPORT", "openrouter")
    monkeypatch.setenv("ATLAS_SOL_ACCOUNTING_SOCKET", "/private/run/accounting.sock")
    monkeypatch.setenv("ATLAS_SOL_ACCOUNTING_TOKEN_FILE", "/private/run/token")
    policy = budget.capture_admission_policy()
    projection = budget.canonical_admission_policy_projection(policy)
    assert policy.enabled is True
    assert policy.transport == "openrouter"
    assert policy.sol_model == "gpt-6.1-sol"
    assert policy.luna_model == "gpt-6-luna"
    assert projection["provider_order"] == ["openai"]
    assert projection["provider_only"] == ["openai"]
    assert projection["allow_fallbacks"] is False
    assert projection["require_parameters"] is True
    assert projection["request_service_tier"] == "auto"
    assert "/private/run" not in repr(projection)
    assert policy.accounting_socket_ref_sha256
    assert policy.accounting_token_file_ref_sha256
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_TRANSPORT", "openai")
    monkeypatch.setattr(budget, "SOL", "changed-sol")
    assert policy.transport == "openrouter"
    assert policy.sol_model == "gpt-6.1-sol"
    assert budget.canonical_admission_policy_projection() != projection


def test_guarded_admission_uses_one_policy_snapshot_through_luna_fallback(monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_ENABLED", "true")
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_TRANSPORT", "openrouter")
    monkeypatch.setenv("ATLAS_SOL_ACCOUNTING_SOCKET", "/tmp/test-accounting.sock")
    monkeypatch.setenv("ATLAS_SOL_ACCOUNTING_TOKEN_FILE", "/tmp/test-accounting-token")
    monkeypatch.setattr(budget, "_read_token", lambda _path: "fixture-token")
    events = []

    def post(_socket, _token, path, body):
        events.append((path, body))
        return {"status": "denied"}

    monkeypatch.setattr(budget, "_post", post)
    checked = []
    prepared_policy = budget.capture_admission_policy()

    class Guard:
        prepared = SimpleNamespace(policy=prepared_policy)

        def check_policy(self, policy):
            checked.append(("policy", policy))
            # Prove routing/limit/pinning after this point use the frozen input.
            monkeypatch.setenv("ATLAS_MODEL_ROUTING_TRANSPORT", "openai")
            monkeypatch.setattr(budget, "SOL", "changed-sol")
            monkeypatch.setattr(budget, "LUNA", "changed-luna")

        def check_dispatch(self, agent, outbound, policy):
            checked.append(("dispatch", policy, dict(outbound)))

        def authorize_budget_fallback(self, policy, source_model, target_model, effort):
            checked.append(("fallback-authorized", policy, source_model, target_model, effort))
            return "authorized-marker"

    agent = SimpleNamespace(model=budget.SOL, _atlas_resolution_guard=Guard())
    seen = {}

    def perform(payload):
        seen.update(payload)
        return "ok"

    assert budget.admitted_call(
        agent, {"model": "gpt-6.1-sol", "max_output_tokens": 500, "input": "hello"}, perform,
    ) == "ok"
    reserved_payload = budget._pin_openrouter_provider(
        {"model": "openai/gpt-6.1-sol", "max_output_tokens": 500, "input": "hello"}, checked[0][1],
    )
    assert Decimal(events[0][1]["max_usd"]) == budget._maximum_usd(reserved_payload, checked[0][1])
    assert seen["model"] == "openai/gpt-6-luna"
    assert seen["extra_body"]["provider"] == {
        "order": ["openai"], "only": ["openai"],
        "allow_fallbacks": False, "require_parameters": True,
    }
    assert seen["service_tier"] == "auto"
    assert seen["reasoning"]["effort"] == "xhigh"
    assert [item[0] for item in checked] == ["policy", "fallback-authorized", "dispatch"]
    assert checked[2][1] is prepared_policy
    assert checked[2][2]["model"] == "openai/gpt-6-luna"
    assert agent._atlas_sol_budget_fallback == "authorized-marker"


def test_guard_rejections_propagate_before_reservation_or_provider_dispatch(monkeypatch):
    import pytest

    monkeypatch.setenv("ATLAS_MODEL_ROUTING_ENABLED", "true")
    monkeypatch.setenv("ATLAS_SOL_ACCOUNTING_SOCKET", "/tmp/test-accounting.sock")
    monkeypatch.setenv("ATLAS_SOL_ACCOUNTING_TOKEN_FILE", "/tmp/test-accounting-token")
    monkeypatch.setattr(budget, "_read_token", lambda _path: "fixture-token")
    events = []
    monkeypatch.setattr(budget, "_post", lambda *_args: events.append("reserve") or {"status": "reserved"})

    class PolicyGuard:
        prepared = SimpleNamespace(policy=budget.capture_admission_policy())

        def check_policy(self, _policy):
            raise RuntimeError("policy drift")

        def check_dispatch(self, _agent, _payload, _policy):
            raise RuntimeError("dispatch drift")

    with pytest.raises(RuntimeError, match="policy drift"):
        budget.admitted_call(
            SimpleNamespace(model=budget.SOL, _atlas_resolution_guard=PolicyGuard()),
            {"model": budget.SOL, "max_output_tokens": 500}, lambda _payload: pytest.fail("dispatched"),
        )
    assert events == []

    # When dispatch validation rejects after reservation, it still propagates
    # and the provider callable remains untouched.
    class DispatchGuard:
        prepared = SimpleNamespace(policy=budget.capture_admission_policy())

        def check_policy(self, _policy):
            return None

        def check_dispatch(self, _agent, _payload, _policy):
            raise RuntimeError("dispatch drift")

    with pytest.raises(RuntimeError, match="dispatch drift"):
        budget.admitted_call(
            SimpleNamespace(model=budget.SOL, _atlas_resolution_guard=DispatchGuard()),
            {"model": budget.SOL, "max_output_tokens": 500}, lambda _payload: pytest.fail("dispatched"),
        )
    assert events == ["reserve", "reserve"]
