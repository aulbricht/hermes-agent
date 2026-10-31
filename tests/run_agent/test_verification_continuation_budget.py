"""End-to-end regression coverage for verification budget exhaustion (#61631)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _response(content="composed report"):
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
    )


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        instance = AIAgent(
            session_id="verify-budget-test",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            provider="openai-compat",
            model="test/model",
            max_iterations=1,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    instance._cached_system_prompt = "stable test prompt"
    instance._session_db = None
    instance._session_json_enabled = False
    instance.save_trajectories = False
    instance.compression_enabled = False
    instance._cleanup_task_resources = lambda *_a, **_kw: None
    instance._save_trajectory = lambda *_a, **_kw: None
    return instance


def _assert_pending_response_survives(agent, result):
    assert result["final_response"] == "composed report"
    assert result["turn_exit_reason"] == "max_iterations_reached(1/1)"
    assert result["completed"] is False
    assert agent._handle_max_iterations.call_count == 0
    assert [message["role"] for message in result["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_verify_on_stop_preserves_composed_report_at_budget_limit(agent, monkeypatch):
    def model_call(_api_kwargs):
        agent._turn_file_mutation_paths = {"changed.py"}
        return _response()

    agent._interruptible_api_call = model_call
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")

    with (
        patch("agent.verification_stop.build_verify_on_stop_nudge", return_value="verify it"),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit changed.py")

    _assert_pending_response_survives(agent, result)
    assert result["messages"][1]["_verification_stop_synthetic"] is True
    assert result["messages"][2]["_verification_stop_synthetic"] is True


def test_pre_verify_preserves_composed_report_at_budget_limit(agent, monkeypatch):
    def model_call(_api_kwargs):
        agent._turn_file_mutation_paths = {"changed.py"}
        return _response()

    agent._interruptible_api_call = model_call
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    with (
        patch("hermes_cli.plugins.has_hook", side_effect=lambda name: name == "pre_verify"),
        patch(
            "hermes_cli.plugins.get_pre_verify_continue_message",
            return_value="run project tests",
        ),
        patch("agent.verify_hooks.max_verify_nudges", return_value=2),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit changed.py")

    _assert_pending_response_survives(agent, result)
    assert result["messages"][1]["_pre_verify_synthetic"] is True
    assert result["messages"][2]["_pre_verify_synthetic"] is True


def test_intermediate_ack_uses_summary_instead_of_premature_text(agent, monkeypatch):
    agent.valid_tool_names = ["web_search"]
    agent._intent_ack_continuation = True
    agent._looks_like_codex_intermediate_ack = MagicMock(return_value=True)
    agent._interruptible_api_call = lambda _kwargs: _response("I'll inspect the files now")
    agent._handle_max_iterations = MagicMock(return_value="verified summary.")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("inspect /tmp/project")

    assert result["final_response"] == "verified summary."
    assert result["turn_exit_reason"] == "max_iterations_reached(1/1)"
    agent._handle_max_iterations.assert_called_once()


def test_later_verified_response_supersedes_pending_report(agent, monkeypatch):
    agent.max_iterations = 2
    agent.iteration_budget.max_total = 2
    answers = iter([_response("premature report"), _response("verified final report")])
    agent._interruptible_api_call = lambda _kwargs: next(answers)
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")

    with (
        patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            side_effect=["verify it", None],
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit changed.py")

    assert result["final_response"] == "verified final report"
    assert result["turn_exit_reason"] == "text_response(finish_reason=stop)"
    assert result["completed"] is True
    agent._handle_max_iterations.assert_not_called()


def _tool_call_response(n):
    call = SimpleNamespace(
        id=f"call_{n}",
        type="function",
        function=SimpleNamespace(name="web_search", arguments=f'{{"q": "lease {n}"}}'),
    )
    message = SimpleNamespace(content="", tool_calls=[call])
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
        model="test/model",
        usage=None,
    )


def test_atlas_query_turn_limit_is_four_calls_and_no_summary_call(agent, monkeypatch):
    agent.max_iterations = 4
    agent.iteration_budget.max_total = 4
    agent.max_tokens = 4096
    agent.no_iteration_summary = True
    agent.valid_tool_names = {"web_search"}
    sent = []

    def model_call(api_kwargs):
        sent.append(api_kwargs)
        return _tool_call_response(len(sent))

    agent._interruptible_api_call = model_call
    agent.client.chat.completions.create = MagicMock()
    agent._handle_max_iterations = MagicMock(wraps=agent._handle_max_iterations)
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    with (
        patch("run_agent.handle_function_call", return_value="rows"),
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("which leases end next month?")

    assert len(sent) == 4  # exactly four provider calls, no fifth
    agent.client.chat.completions.create.assert_not_called()  # the summary call is never made
    agent._handle_max_iterations.assert_not_called()
    # The reply is the fixed local notice, not a model-written summary.
    assert "maximum tool-iteration limit" in result["final_response"]
    assert result["turn_exit_reason"] == "max_iterations_reached(4/4)"
    # The per-turn output cap reaches every request.
    assert all(4096 in [kw.get(k) for k in ("max_tokens", "max_completion_tokens", "max_output_tokens")] for kw in sent)
