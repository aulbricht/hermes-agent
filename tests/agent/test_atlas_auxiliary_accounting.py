import asyncio
from types import SimpleNamespace

import pytest

from agent import atlas_auxiliary_accounting as accounting
from agent import auxiliary_client


def _response():
    return SimpleNamespace(
        id="resp-test", model=accounting.LUNA, service_tier="standard",
        choices=[SimpleNamespace(message=SimpleNamespace(content="summary"))],
        usage=SimpleNamespace(
            prompt_tokens=100, completion_tokens=20,
            prompt_tokens_details=SimpleNamespace(cached_tokens=10, cache_write_tokens=5),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=3),
        ),
    )


def test_atlas_compression_uses_only_direct_openai_and_meters_once(monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_ENABLED", "true")
    calls = []
    receipts = []

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return _response()

    client = SimpleNamespace(base_url="https://api.openai.com/v1", chat=SimpleNamespace(completions=Completions()))

    def get_client(provider, model, **kwargs):
        assert provider == "openai-api"
        assert model == accounting.LUNA
        assert kwargs["base_url"] == "https://api.openai.com/v1"
        assert kwargs["api_mode"] == "codex_responses"
        assert kwargs["main_runtime"] is None
        return client, model

    monkeypatch.setattr(auxiliary_client, "_get_cached_client", get_client)
    monkeypatch.setattr(accounting, "record_completed_response", lambda response, **kw: receipts.append((response, kw)))

    response = auxiliary_client.call_llm(task="compression", provider="openrouter", model="gpt-6.1-sol", messages=[{"role": "user", "content": "history"}], max_tokens=200)

    assert response.choices[0].message.content == "summary"
    assert calls[0]["model"] == accounting.LUNA
    assert calls[0]["extra_body"]["reasoning"]["effort"] == "medium"
    assert "temperature" not in calls[0]
    assert len(receipts) == 1
    assert receipts[0][0] is response


def test_atlas_compression_does_not_fallback_when_direct_credentials_missing(monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_ENABLED", "true")
    monkeypatch.setattr(auxiliary_client, "_get_cached_client", lambda *_args, **_kwargs: (None, None))
    monkeypatch.setattr(auxiliary_client, "_try_configured_fallback_for_unavailable_client", lambda *_args: pytest.fail("provider fallback attempted"))
    with pytest.raises(RuntimeError, match="direct OpenAI API credentials"):
        auxiliary_client.call_llm(task="compression", messages=[{"role": "user", "content": "history"}])


def test_atlas_compression_provider_error_does_not_try_another_provider(monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_ENABLED", "true")
    attempts = []

    class Completions:
        def create(self, **_kwargs):
            attempts.append("openai")
            raise RuntimeError("OpenAI rejected request")

    client = SimpleNamespace(base_url="https://api.openai.com/v1", chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr(auxiliary_client, "_get_cached_client", lambda *_args, **_kwargs: (client, accounting.LUNA))
    monkeypatch.setattr(auxiliary_client, "_try_configured_fallback_for_unavailable_client", lambda *_args: pytest.fail("provider fallback attempted"))
    with pytest.raises(RuntimeError, match="OpenAI rejected request"):
        auxiliary_client.call_llm(task="compression", messages=[{"role": "user", "content": "history"}])
    assert attempts == ["openai"]


def test_atlas_title_generation_overrides_openrouter_and_sol_with_direct_luna(monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_ENABLED", "true")
    calls = []

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return _response()

    client = SimpleNamespace(base_url="https://api.openai.com/v1", chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr(auxiliary_client, "_get_cached_client", lambda provider, model, **kwargs: (client, model))
    monkeypatch.setattr(accounting, "record_completed_response", lambda *_args, **_kwargs: None)

    auxiliary_client.call_llm(
        task="title_generation", provider="openrouter", model="gpt-6.1-sol",
        messages=[{"role": "user", "content": "make title"}], temperature=0.4,
    )

    assert calls[0]["model"] == accounting.LUNA
    assert calls[0]["extra_body"]["reasoning"]["effort"] == "low"
    assert "temperature" not in calls[0]


def test_atlas_openrouter_auxiliary_pins_openai_responses_and_meters_provider_cost(monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_ENABLED", "true")
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_TRANSPORT", "openrouter")
    calls = []
    receipts = []

    class Responses:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(close=lambda: None)

    final = SimpleNamespace(
        id="resp-or", model="openai/gpt-6-luna", service_tier="default",
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=20,
            input_tokens_details=SimpleNamespace(cached_tokens=10, cache_write_tokens=5),
            output_tokens_details=SimpleNamespace(reasoning_tokens=3),
            cost=0, cost_details=SimpleNamespace(upstream_inference_cost="0.000010"), is_byok=True,
        ),
        output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text="summary")])],
    )
    monkeypatch.setattr("agent.codex_runtime._consume_codex_event_stream", lambda *_args, **_kwargs: final)
    client = SimpleNamespace(base_url="https://openrouter.ai/api/v1", api_key="fixture", responses=Responses())

    def get_client(provider, model, **kwargs):
        assert provider == "openrouter"
        assert model == "openai/gpt-6-luna"
        assert kwargs["base_url"] == "https://openrouter.ai/api/v1"
        assert kwargs["api_mode"] == "codex_responses"
        return auxiliary_client.CodexAuxiliaryClient(client, model), model

    monkeypatch.setattr(auxiliary_client, "_get_cached_client", get_client)
    monkeypatch.setattr(accounting, "record_completed_response", lambda response, **kw: receipts.append(accounting.receipt_payload(response, **kw)))

    response = auxiliary_client.call_llm(
        task="title_generation", provider="openai-api", model="gpt-6.1-sol",
        messages=[{"role": "user", "content": "make title"}], temperature=0.4,
    )

    assert response.id == "resp-or"
    assert calls[0]["model"] == "openai/gpt-6-luna"
    assert calls[0]["provider"] == {
        "order": ["openai"], "only": ["openai"],
        "allow_fallbacks": False, "require_parameters": True,
    }
    assert calls[0]["service_tier"] == "auto"
    assert "temperature" not in calls[0]
    assert len(receipts) == 1
    assert receipts[0]["provider"] == "openrouter"
    assert receipts[0]["is_byok"] is True
    assert receipts[0]["charged_usd"] == "0"
    assert receipts[0]["upstream_usd"] == "0.000010"
    assert receipts[0]["cost_basis"] == "byok_split"
    assert receipts[0]["estimated_total_usd"] == "0"


def test_atlas_openrouter_byok_missing_component_keeps_observed_cost_separate(monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_TRANSPORT", "openrouter")
    response = SimpleNamespace(
        model="openai/gpt-6-luna", service_tier="default",
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=20,
            input_tokens_details=SimpleNamespace(cached_tokens=0),
            cost=0, cost_details=SimpleNamespace(), is_byok=True,
        ),
    )
    payload = accounting.receipt_payload(response)
    assert payload["charged_usd"] == "0"
    assert payload["upstream_usd"] == "0"
    assert payload["cost_basis"] == "byok_estimated"
    assert payload["estimated_total_usd"] == "0.0000210"


def test_atlas_aux_receipt_cost_is_estimated_and_content_free():
    response = _response()
    payload = accounting.receipt_payload(response)
    assert payload["provider"] == "openai"
    assert payload["purpose"] == "system_other"
    assert payload["verification_state"] == "estimated"
    assert payload["charged_usd"] == "0.000019225"
    assert payload["input_tokens"] == 100
    assert payload["output_tokens"] == 20
    assert "summary" not in repr(payload)


def test_responses_adapter_preserves_usage_and_generation_metadata(monkeypatch):
    usage = SimpleNamespace(
        input_tokens=100, output_tokens=20, total_tokens=120,
        input_tokens_details=SimpleNamespace(cached_tokens=10, cache_write_tokens=5),
        output_tokens_details=SimpleNamespace(reasoning_tokens=3),
    )
    final = SimpleNamespace(
        id="resp-codex", model=accounting.LUNA, service_tier="standard", usage=usage,
        output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text="summary")])],
    )
    monkeypatch.setattr("agent.codex_runtime._consume_codex_event_stream", lambda *_args, **_kwargs: final)

    class Responses:
        def create(self, **_kwargs):
            return SimpleNamespace(close=lambda: None)

    client = SimpleNamespace(base_url="https://api.openai.com/v1", responses=Responses())
    response = auxiliary_client._CodexCompletionsAdapter(client, accounting.LUNA).create(
        model=accounting.LUNA, messages=[{"role": "user", "content": "history"}],
    )
    receipt = accounting.receipt_payload(response)
    assert response.id == "resp-codex"
    assert response.usage.input_tokens_details.cached_tokens == 10
    assert response.usage.output_tokens_details.reasoning_tokens == 3
    assert receipt["provider_generation_id"] == "resp-codex"
    assert receipt["cache_read_tokens"] == 10


def test_responses_adapter_forwards_openrouter_provider_pin(monkeypatch):
    captured = {}
    final = SimpleNamespace(
        id="resp-or-adapter", model="openai/gpt-6-luna", service_tier="default",
        usage=SimpleNamespace(input_tokens=10, output_tokens=2), output=[],
    )
    monkeypatch.setattr("agent.codex_runtime._consume_codex_event_stream", lambda *_args, **_kwargs: final)

    class Responses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(close=lambda: None)

    client = SimpleNamespace(base_url="https://openrouter.ai/api/v1", responses=Responses())
    adapter = auxiliary_client._CodexCompletionsAdapter(client, "openai/gpt-6-luna")
    adapter.create(
        model="openai/gpt-6-luna", messages=[{"role": "user", "content": "x"}],
        extra_body={"provider": {"only": ["openai"], "allow_fallbacks": False}, "service_tier": "auto"},
    )
    assert captured["provider"] == {"only": ["openai"], "allow_fallbacks": False}
    assert captured["service_tier"] == "auto"


def test_atlas_async_compression_meters_without_retrying_generation(monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_ENABLED", "true")
    creates = []
    receipts = []

    class Completions:
        async def create(self, **kwargs):
            creates.append(kwargs)
            return _response()

    client = SimpleNamespace(base_url="https://api.openai.com/v1", chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr(auxiliary_client, "_get_cached_client", lambda *_args, **_kwargs: (client, accounting.LUNA))

    async def record(response, **kwargs):
        receipts.append(response)
        raise OSError("receipt endpoint unavailable")

    monkeypatch.setattr(accounting, "record_completed_response_async", record)
    response = asyncio.run(auxiliary_client.async_call_llm(task="compression", messages=[{"role": "user", "content": "history"}]))
    assert response.choices[0].message.content == "summary"
    assert len(creates) == 1
    assert len(receipts) == 1


@pytest.mark.parametrize("async_mode", [False, True])
def test_delegated_aux_usage_actor_bound_and_retained_after_revocation(monkeypatch, async_mode):
    import hashlib
    import threading
    from agent import atlas_delegation as policy
    monkeypatch.setenv("ATLAS_MODEL_ROUTING_ENABLED", "true")
    agent = SimpleNamespace(_atlas_delegation_policy=policy.POLICY_VERSION,
        _atlas_delegation_identities={"actor_user_id": "actor", "subject_user_id": "subject", "view_as_session_id": "session", "resource_type": "chat", "resource_id": "turn_fixture", "receipt_limit": "256"},
        _atlas_route_request_id="route_fixture", _atlas_paid_dispatch_lock=threading.Lock(),
        _atlas_paid_dispatch_count=0, _atlas_auxiliary_usage_calls=[])
    authorized = [True]
    sent = []
    def check(agent):
        if not authorized[0]:
            raise policy.DelegationDenied("expired")
    monkeypatch.setattr(policy, "validate_dispatch", check)
    def create(**kwargs):
        sent.append(kwargs)
        authorized[0] = False
        return _response()
    async def async_create(**kwargs):
        return create(**kwargs)
    client = SimpleNamespace(base_url="https://api.openai.com/v1", chat=SimpleNamespace(completions=SimpleNamespace(create=async_create if async_mode else create)))
    monkeypatch.setattr(auxiliary_client, "_get_cached_client", lambda *args, **kwargs: (client, accounting.LUNA))
    monkeypatch.setattr(accounting, "record_completed_response", lambda *args, **kwargs: pytest.fail("delegated usage must use durable terminal settlement"))
    monkeypatch.setattr(accounting, "record_completed_response_async", lambda *args, **kwargs: pytest.fail("delegated usage must use durable terminal settlement"))
    with policy.dispatch_context(agent):
        result = auxiliary_client.async_call_llm(task="compression", messages=[{"role": "user", "content": "history"}]) if async_mode else auxiliary_client.call_llm(task="compression", messages=[{"role": "user", "content": "history"}])
        if async_mode:
            result = asyncio.run(result)
        with pytest.raises(policy.DelegationDenied):
            policy.admit_paid_dispatch(agent)
    assert result.id == "resp-test"
    assert agent._atlas_paid_dispatch_count == 1
    receipt, = policy.terminal_usage_calls(agent)
    assert receipt["user_id"] == receipt["actor_user_id"] == "actor"
    assert receipt["subject_user_id"] == "subject" and receipt["view_as_session_id"] == "session"
    assert receipt["resource_id"] == "turn_fixture" and receipt["route_request_id"] == "route_fixture"
    assert sent[0]["user"] == receipt["provider_user_hash"] == "atlas-user-" + hashlib.sha256(b"actor").hexdigest()
    assert receipt["input_tokens"] == 85 and receipt["input_tokens_total"] == 100 and receipt["generation_id"] == "resp-test"


def test_responses_adapter_keeps_actor_hash_on_actual_provider_request(monkeypatch):
    final = SimpleNamespace(id="resp", model=accounting.LUNA, usage=SimpleNamespace(input_tokens=1, output_tokens=1), output=[])
    monkeypatch.setattr("agent.codex_runtime._consume_codex_event_stream", lambda *args, **kwargs: final)
    requests = []
    client = SimpleNamespace(base_url="https://api.openai.com/v1", responses=SimpleNamespace(create=lambda **kwargs: requests.append(kwargs) or SimpleNamespace(close=lambda: None)))
    auxiliary_client._CodexCompletionsAdapter(client, accounting.LUNA).create(messages=[{"role": "user", "content": "read"}], user="atlas-user-fixture")
    assert requests[0]["user"] == "atlas-user-fixture"
