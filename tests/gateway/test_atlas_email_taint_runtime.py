"""Executor propagation and api_server background-review controls (no provider calls)."""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import atlas_email_tool as email_tool


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_taint", [False, True])
async def test_run_agent_binds_shared_taint_holder_inside_executor(tmp_path, initial_taint):
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from hermes_state import SessionDB
    from tools.thread_context import propagate_context_to_thread

    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    db = SessionDB(tmp_path / "sessions.db")
    adapter._session_db = db
    caller_thread = threading.get_ident()
    seen = []

    class Agent:
        session_id = "executor-session"

        def run_conversation(self, **kwargs):
            assert threading.get_ident() != caller_thread
            assert email_tool.mailbox_read_this_run() is initial_taint
            holder = email_tool._tainted.get()
            assert holder is not None

            def tool_worker():
                assert email_tool._tainted.get() is holder
                assert email_tool.mailbox_read_this_run() is initial_taint
                email_tool._mark_tainted()

            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(propagate_context_to_thread(tool_worker)).result(timeout=5)
            assert email_tool.mailbox_read_this_run() is True
            seen.append(holder)
            return {"final_response": "done", "messages": []}

    outer = email_tool.bind_email_capability("", tainted=False)
    try:
        with patch.object(adapter, "_create_agent", return_value=Agent()) as create:
            result, _ = await adapter._run_agent(
                "hello", [], session_id="executor-session",
                atlas_email_mailbox_tainted=initial_taint,
            )
        assert create.call_args.kwargs["atlas_email_mailbox_tainted"] is initial_taint
        assert result["atlas_email_mailbox_tainted"] is True
        assert seen and adapter._session_is_mailbox_tainted("executor-session")
        assert email_tool.mailbox_read_this_run() is False
    finally:
        email_tool.clear_email_capability(outer)
        db.close()


def _finalizer_agent(spawned_reviews, synced_turns):
    return SimpleNamespace(
        max_iterations=10,
        iteration_budget=SimpleNamespace(remaining=5, used=1, max_total=10),
        no_iteration_summary=False,
        quiet_mode=True,
        model="test/model",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        session_id="sess-bg-review",
        platform="api_server",
        session_input_tokens=10,
        session_output_tokens=5,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_reasoning_tokens=0,
        session_prompt_tokens=10,
        session_completion_tokens=5,
        session_total_tokens=15,
        session_estimated_cost_usd=0.0,
        session_cost_status="ok",
        session_cost_source="estimated",
        context_compressor=SimpleNamespace(last_prompt_tokens=10),
        request_overrides={},
        _tool_guardrail_halt_decision=None,
        _response_was_previewed=False,
        _stream_callback=None,
        _interrupt_message=None,
        _skill_nudge_interval=10,
        _iters_since_skill=10,
        valid_tool_names={"skill_manage", "memory"},
        _save_trajectory=lambda *a, **k: None,
        _cleanup_task_resources=lambda *a, **k: None,
        _drop_trailing_empty_response_scaffolding=lambda msgs: None,
        _persist_session=lambda msgs, hist: None,
        _file_mutation_verifier_enabled=lambda: False,
        _turn_completion_explainer_enabled=lambda: False,
        _drain_pending_steer=lambda: None,
        clear_interrupt=lambda: None,
        _sync_external_memory_for_turn=lambda **kw: synced_turns.append(kw),
        _spawn_background_review=lambda **kw: spawned_reviews.append(kw),
    )


@pytest.mark.parametrize("skill_available,tainted,expected_reviews", [
    (False, False, 0), (False, True, 0), (True, False, 1), (True, True, 0),
])
def test_api_server_skill_review_with_memory_disabled(skill_available, tainted, expected_reviews):
    from agent.turn_finalizer import finalize_turn

    reviews, syncs = [], []
    agent = _finalizer_agent(reviews, syncs)
    # Reported live config has no memory section and no skills toolset. A clean
    # skill-enabled control proves memory=False alone does not suppress review.
    agent._memory_enabled = False
    agent._user_profile_enabled = False
    agent.valid_tool_names = {"skill_manage"} if skill_available else set()
    bound = email_tool.bind_email_capability("", tainted=tainted)
    try:
        result = finalize_turn(
            agent, final_response="done", api_call_count=1,
            interrupted=False, failed=False,
            messages=[{"role": "user", "content": "hello"},
                      {"role": "assistant", "content": "done"}],
            conversation_history=[], effective_task_id="config-control",
            turn_id="turn-config", user_message="hello", original_user_message="hello",
            _should_review_memory=False, _turn_exit_reason="text_response(finish_reason=stop)",
        )
        assert len(reviews) == expected_reviews
        assert len(syncs) == (0 if tainted else 1)
        assert result["atlas_email_mailbox_tainted"] is tainted
        if reviews:
            assert reviews[0]["review_memory"] is False
            assert reviews[0]["review_skills"] is True
    finally:
        email_tool.clear_email_capability(bound)


def test_background_entrypoints_refuse_tainted_transcripts_before_spawning():
    from run_agent import AIAgent

    bound = email_tool.bind_email_capability("", tainted=True)
    try:
        # No initialized agent or transport is needed: taint must stop these
        # entrypoints before they access providers or construct a review thread.
        agent = SimpleNamespace()
        with patch("run_agent.threading.Thread") as thread:
            AIAgent._spawn_background_review(agent, [], review_memory=True, review_skills=True)
            AIAgent._sync_external_memory_for_turn(
                agent, original_user_message="hello", final_response="done",
                interrupted=False, messages=[],
            )
        thread.assert_not_called()
    finally:
        email_tool.clear_email_capability(bound)


@pytest.mark.parametrize("tainted", [False, True])
def test_reported_api_server_config_flows_through_toolset_resolution(tmp_path, monkeypatch, tainted):
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from hermes_state import SessionDB

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = {
        "platform_toolsets": {"api_server": ["web", "acre-filemaker"]},
        "agent": {"disabled_toolsets": [
            "terminal", "file", "browser", "computer_use", "code_execution",
            "delegation", "cronjob", "tts", "memory", "session_search", "image_gen",
            "video_gen", "todo", "clarify",
        ]},
        "mcp_servers": {"acre-filemaker": {"enabled": True, "url": "http://127.0.0.1:1/mcp"}},
    }
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    db = SessionDB(tmp_path / "sessions.db")
    adapter._session_db = db
    try:
        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={
            "api_key": "synthetic", "base_url": None, "provider": None,
            "api_mode": None, "command": None, "args": [],
        }), patch("gateway.run._resolve_gateway_model", return_value="test/model"), patch(
            "gateway.run._load_gateway_config", return_value=config
        ), patch("gateway.run._current_max_iterations", return_value=90), patch(
            "run_agent.AIAgent"
        ) as agent:
            adapter._create_agent(atlas_email_authorized=True, atlas_email_mailbox_tainted=tainted)
        enabled = set(agent.call_args.kwargs["enabled_toolsets"])
        assert "atlas_email" in enabled
        assert not enabled & {"skills", "memory", "file", "session_search", "kanban"}
        if tainted:
            assert enabled == {"atlas_email"}
        else:
            assert {"web", "acre-filemaker"} <= enabled
    finally:
        db.close()
