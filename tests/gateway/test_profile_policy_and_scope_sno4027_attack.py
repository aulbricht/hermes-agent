"""Hermes attack addendum (SNO-4028 / parent SNO-4027): profile tool policy,
strict workflow toolset_scope across all API server generation endpoints,
real offline AIAgent inline memory suppression (including non-'memory' provider
tools, system prompt blocks, and prefetch), concurrent turn ceiling isolation,
external_only skill roots, and email taint / worker-context compatibility.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import json
import os
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

# Minimal offline fallback when running in a host venv without `requests`
# (run_agent.py imports tools.terminal_tool -> managed_modal -> requests at
# import time even when all tests run offline against temp HERMES_HOME).
try:
    import requests as _requests  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - used only on minimal offline runners
    _req_stub = types.ModuleType("requests")
    _req_exc = types.ModuleType("requests.exceptions")
    for _exc_name in (
        "RequestException",
        "Timeout",
        "ConnectionError",
        "HTTPError",
        "SSLError",
        "ChunkedEncodingError",
    ):
        setattr(_req_exc, _exc_name, Exception)
    _req_stub.exceptions = _req_exc
    _req_stub.Response = object
    _req_stub.Session = lambda *_a, **_k: SimpleNamespace(headers={}, close=lambda: None)
    sys.modules.setdefault("requests", _req_stub)
    sys.modules.setdefault("requests.exceptions", _req_exc)

import hermes_constants
import model_tools
import run_agent
from agent import agent_runtime_helpers, memory_manager, prompt_builder, skill_utils, tool_executor
from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    ATLAS_QUERY_MAX_TURNS,
    ATLAS_QUERY_TURN_MAX_OUTPUT_TOKENS,
    APIServerAdapter,
)
from hermes_cli import config as hermes_config
from hermes_cli import middleware as hermes_middleware
from hermes_cli import plugins as hermes_plugins
from hermes_state import SessionDB
from tools import (
    atlas_import_tool,
    atlas_ownership_tool,
    atlas_query_tool,
    code_execution_tool,
    memory_tool,
    skill_manager_tool,
    skills_sync,
    skills_tool,
)
from tools.registry import registry


IMPORT_TOKEN = "atimp_" + "a" * 32
OWNERSHIP_TOKEN = "atown_" + "b" * 32
QUERY_TOKEN = "atqry_" + "c" * 32
EMAIL_TOKEN = "atmail_" + "d" * 32
API_SERVER_TEST_KEY = "atlas-hermes-test-key-sno4028"


def _write_yaml(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _write_skill(root: Path, name: str, description: str, body: str, category: str | None = None) -> Path:
    skill_dir = (root / category / name) if category else (root / name)
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_md


def _clear_skill_caches() -> None:
    prompt_builder.clear_skills_system_prompt_cache()
    skills_tool._SKILLS_CACHE.clear()


def _configure_hermes_home(monkeypatch: pytest.MonkeyPatch, home_dir: Path, config_data: dict) -> Path:
    home_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home_dir))
    monkeypatch.setenv("SEARXNG_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(skills_tool, "HERMES_HOME", home_dir, raising=False)
    monkeypatch.setattr(skills_sync, "HERMES_HOME", home_dir, raising=False)
    cfg_path = _write_yaml(home_dir / "config.yaml", config_data)
    _clear_skill_caches()
    return cfg_path


def _restricted_atlas_config(external_dir: Path, extra_agent: dict | None = None, extra_memory: dict | None = None) -> dict:
    agent_section = {
        "environment_probe": False,
        "tool_policy": {
            "version": 1,
            "denied_tools": ["skill_manage", "memory"],
            "require_explicit_scope": True,
        },
    }
    if extra_agent:
        agent_section.update(extra_agent)
    memory_section = {
        "memory_enabled": False,
        "user_profile_enabled": False,
        "provider": "",
        "write_approval": True,
    }
    if extra_memory:
        memory_section.update(extra_memory)
    return {
        "model": "test/offline-model",
        "agent": agent_section,
        "memory": memory_section,
        "skills": {
            "external_only": True,
            "external_dirs": [str(external_dir)],
            "creation_nudge_interval": 1,
        },
        "platform_toolsets": {
            "api_server": ["web", "skills"],
        },
    }


def _make_real_offline_agent(enabled_toolsets: list[str] | None = None) -> run_agent.AIAgent:
    """Instantiate a real run_agent.AIAgent completely offline with zero network calls."""
    run_agent._openrouter_prewarm_done.set()
    return run_agent.AIAgent(
        model="test/offline-model",
        api_key="test-offline-key",
        base_url="http://127.0.0.1:9/v1",
        provider="custom",
        api_mode="chat_completions",
        enabled_toolsets=enabled_toolsets if enabled_toolsets is not None else ["web", "skills"],
        quiet_mode=True,
        skip_context_files=True,
        platform="api_server",
    )


def _make_tool_call(call_id: str, name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments),
        ),
    )


def _make_assistant_msg(tool_calls: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(tool_calls=tool_calls)


def _parse_tool_msg_json(msg: dict) -> dict:
    """Parse JSON from a tool-result message, unwrapping <untrusted_tool_result> framing when present."""
    raw = str(msg["content"]).strip()
    if raw.startswith("<untrusted_tool_result") and raw.endswith("</untrusted_tool_result>"):
        body = raw.split("\n\n", 1)[1]
        raw = body[: -len("</untrusted_tool_result>")].strip()
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Controls (must pass on unmodified HEAD and remain passing after the build)
# ---------------------------------------------------------------------------


def test_control_unrestricted_profile_preserves_default_memory_skills_and_optional_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: When agent.tool_policy and skills.external_only are omitted,
    default profiles keep skill_manage, memory, local skills, and optional toolset_scope.
    """
    home_dir = tmp_path / "unrestricted_home"
    local_skills = home_dir / "skills"
    _write_skill(local_skills, "control-local-skill", "Control local skill", "Control skill body.")
    _configure_hermes_home(
        monkeypatch,
        home_dir,
        {
            "model": "test/offline-model",
            "agent": {"environment_probe": False},
            "memory": {"memory_enabled": True, "user_profile_enabled": True},
        },
    )

    defs = model_tools.get_tool_definitions(
        enabled_toolsets=["skills", "memory"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    names = {d["function"]["name"] for d in defs}
    assert {"skills_list", "skill_view", "skill_manage", "memory"}.issubset(names)

    listed = json.loads(skills_tool.skills_list())
    assert listed["success"] is True
    assert any(s["name"] == "control-local-skill" for s in listed["skills"])

    snapshot_path = prompt_builder._skills_prompt_snapshot_path()
    snapshot_path.write_text(
        json.dumps(
            {
                "version": prompt_builder._SKILLS_SNAPSHOT_VERSION,
                "manifest": prompt_builder._build_skills_manifest(local_skills),
                "skills": [
                    {
                        "skill_name": "stale-snapshot-ghost",
                        "frontmatter_name": "stale-snapshot-ghost",
                        "description": "Stale snapshot skill that must not appear",
                        "category": "general",
                        "platforms": [],
                        "conditions": {},
                    }
                ],
                "category_descriptions": {},
            }
        ),
        encoding="utf-8",
    )
    _clear_skill_caches()
    control_prompt_index = prompt_builder.build_skills_system_prompt()
    assert "stale-snapshot-ghost" in control_prompt_index
    assert "Stale snapshot skill that must not appear" in control_prompt_index


def test_control_existing_atlas_capability_isolation_without_strict_policy() -> None:
    """Control: Capability tools remain invisible when unbound and visible only when bound."""
    def visible(toolset_name: str) -> set[str]:
        return {
            item["function"]["name"]
            for item in model_tools.get_tool_definitions(
                enabled_toolsets=[toolset_name],
                quiet_mode=True,
                skip_tool_search_assembly=True,
            )
        }

    assert "atlas_import" not in visible("atlas_import")
    assert "atlas_ownership" not in visible("atlas_ownership")
    assert "atlas_query" not in visible("atlas_query")

    b_imp = atlas_import_tool.bind_import_capability(IMPORT_TOKEN)
    try:
        assert {"atlas_import", "atlas_import_proposal"}.issubset(visible("atlas_import"))
    finally:
        atlas_import_tool.clear_import_capability(b_imp)


@pytest.mark.asyncio
async def test_control_worker_contextvar_cleanup_on_mid_turn_exception() -> None:
    """Control: APIServerAdapter._run_agent clears bound turn capabilities in finally even on exception."""
    adapter = APIServerAdapter(PlatformConfig(enabled=True))

    class BoomAgent:
        def __init__(self) -> None:
            self.session_id = "boom-control"
            self.valid_tool_names = {"atlas_query"}
            self.tools = []

        def run_conversation(self, **_kwargs: object) -> dict:
            assert atlas_query_tool.query_capability_available() is True
            raise RuntimeError("simulated mid-turn failure")

    with patch.object(adapter, "_create_agent", return_value=BoomAgent()):
        with pytest.raises(RuntimeError, match="simulated mid-turn failure"):
            await adapter._run_agent(
                "test query failure",
                [],
                atlas_query_capability=QUERY_TOKEN,
                toolset_scope=["skills"],
            )

    assert atlas_query_tool.query_capability_available() is False
    assert atlas_import_tool.import_capability_available() is False
    assert atlas_ownership_tool.ownership_capability_available() is False


# ---------------------------------------------------------------------------
# Gap 1: Real offline AIAgent initialization and sequential/concurrent inline
# memory dispatch (including non-'memory' provider tools, system prompt blocks,
# and prefetch)
# ---------------------------------------------------------------------------


def test_gap1_real_offline_aiagent_init_and_inline_memory_sequential_concurrent_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap 1 attack:
    1a. Real offline AIAgent init under denied_tools=['skill_manage', 'memory'] must suppress
        built-in MemoryStore and external MemoryManager even when config.yaml carries inherited
        memory_enabled=True, user_profile_enabled=True, or provider='extmem', and must never
        expose 'memory', 'skill_manage', or external memory-provider tools (e.g. 'extmem_search',
        'extmem_store') in agent.tools or agent.valid_tool_names.
    1b. Post-init inject_memory_provider_tools, MemoryManager.build_system_prompt, and
        MemoryManager.prefetch_all / _sync_external_memory_for_turn must not inject provider
        schemas, system-prompt blocks, or prefetched context when 'memory' is denied.
    1c. Sequential (agent/tool_executor.py:1284,1430) and concurrent
        (agent/agent_runtime_helpers.py:2155,2281,2307) inline tool execution must refuse
        both 'memory' (add/replace/remove/batch) and non-'memory' provider tools
        ('extmem_search', 'extmem_store') plus 'skill_manage' without mutating MEMORY.md/USER.md,
        staging files under $HERMES_HOME/pending/memory, or calling provider handlers.
    """
    home_dir = tmp_path / "gap1_home"
    ext_dir = tmp_path / "gap1_release_skills"
    _write_skill(ext_dir, "atlas-rent-roll", "Rent roll skill", "Read-only rent roll guidance.")

    mem_dir = home_dir / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem_file = mem_dir / "MEMORY.md"
    user_file = mem_dir / "USER.md"
    mem_initial = "SENTINEL_MEMORY_SECRET_991\n"
    user_initial = "SENTINEL_USER_PROFILE_992\n"
    mem_file.write_text(mem_initial, encoding="utf-8")
    user_file.write_text(user_initial, encoding="utf-8")

    # Configure restricted policy WITH inherited memory settings (AC3: seeded MEMORY.md/USER.md
    # and inherited provider settings) to verify policy denial suppresses all memory paths.
    _configure_hermes_home(
        monkeypatch,
        home_dir,
        _restricted_atlas_config(
            ext_dir,
            extra_memory={
                "memory_enabled": True,
                "user_profile_enabled": True,
                "provider": "extmem",
                "write_approval": True,
            },
        ),
    )

    fake_provider = MagicMock()
    fake_provider.name = "extmem"
    fake_provider.is_available.return_value = True
    fake_provider.get_tool_schemas.return_value = [
        {
            "name": "memory",
            "description": "External shadow memory tool",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "extmem_search",
            "description": "Search external provider memory",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
        {
            "name": "extmem_store",
            "description": "Store entry in external provider memory",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
        },
    ]
    fake_provider.system_prompt_block.return_value = "EXTMEM_PROVIDER_SYSTEM_PROMPT_SECRET_993"
    fake_provider.prefetch.return_value = "EXTMEM_PROVIDER_PREFETCH_SECRET_994"
    fake_provider.handle_tool_call.side_effect = AssertionError(
        "external memory provider tool must not execute when memory is denied"
    )

    with patch("plugins.memory.load_memory_provider", return_value=fake_provider):
        # Instantiate real AIAgent with hermes-api-server (which includes 'memory' and 'skills' in toolsets.py)
        agent = _make_real_offline_agent(enabled_toolsets=["hermes-api-server"])

    tool_names = {t["function"]["name"] for t in agent.tools}
    assert "memory" not in tool_names
    assert "extmem_search" not in tool_names
    assert "extmem_store" not in tool_names
    assert "skill_manage" not in tool_names
    assert "memory" not in agent.valid_tool_names
    assert "extmem_search" not in agent.valid_tool_names
    assert "extmem_store" not in agent.valid_tool_names
    assert "skill_manage" not in agent.valid_tool_names
    assert {"skills_list", "skill_view"}.issubset(tool_names)
    assert agent._memory_store is None
    assert agent._memory_enabled is False
    assert agent._user_profile_enabled is False
    assert agent._memory_manager is None

    # Even if an external MemoryManager with non-'memory' provider tools ('extmem_search', 'extmem_store')
    # is attached post-init, inject_memory_provider_tools, system prompt assembly, prefetch, and
    # sequential/concurrent inline dispatch must all suppress it when 'memory' is denied.
    mgr = memory_manager.MemoryManager()
    mgr.add_provider(fake_provider)
    agent._memory_manager = mgr
    injected_count = memory_manager.inject_memory_provider_tools(agent)
    assert injected_count == 0
    assert not ({"memory", "extmem_search", "extmem_store"} & {t["function"]["name"] for t in agent.tools})
    assert not ({"memory", "extmem_search", "extmem_store"} & agent.valid_tool_names)

    # Attach a real MemoryStore as well to prove defense-in-depth at system prompt & inline executors
    live_store = memory_tool.MemoryStore()
    live_store.load_from_disk()
    agent._memory_store = live_store

    # Build real system prompt and verify no MEMORY.md, USER.md, external provider block,
    # MEMORY_GUIDANCE text, or SKILLS_GUIDANCE text is injected.
    sys_prompt = agent._build_system_prompt()
    assert "SENTINEL_MEMORY_SECRET_991" not in sys_prompt
    assert "SENTINEL_USER_PROFILE_992" not in sys_prompt
    assert "EXTMEM_PROVIDER_SYSTEM_PROMPT_SECRET_993" not in sys_prompt
    assert prompt_builder.MEMORY_GUIDANCE not in sys_prompt
    assert "Save durable facts using the memory tool" not in sys_prompt
    assert prompt_builder.SKILLS_GUIDANCE not in sys_prompt
    assert "save the approach as a skill with skill_manage" not in sys_prompt

    # Turn-start prefetch and turn-end external memory sync must not leak or persist when 'memory' is denied
    from agent.turn_context import build_turn_context

    tctx = build_turn_context(
        agent,
        "Summarize rent roll leases",
        None,
        [],
        "gap1_turn_ctx",
        None,
        None,
        restore_or_build_system_prompt=lambda a, sm, _ch: setattr(
            a, "_cached_system_prompt", a._build_system_prompt(sm)
        ),
        install_safe_stdio=lambda: None,
        sanitize_surrogates=lambda s: s,
        summarize_user_message_for_log=lambda s, **_k: str(s),
        set_session_context=lambda _sid: None,
        set_current_write_origin=lambda _o: None,
        ra=lambda: run_agent,
    )
    assert "EXTMEM_PROVIDER_PREFETCH_SECRET_994" not in (tctx.ext_prefetch_cache or "")
    agent._sync_external_memory_for_turn(
        original_user_message="Summarize rent roll leases",
        final_response="Completed summary.",
        interrupted=False,
        messages=tctx.messages,
    )
    fake_provider.sync_turn.assert_not_called()

    # Situation 1a: Sequential inline execution (tool_executor.execute_tool_calls_sequential)
    seq_messages: list[dict] = []
    seq_calls = [
        _make_tool_call("call_seq_add", "memory", {"action": "add", "target": "memory", "content": "PWN_SEQ_ADD"}),
        _make_tool_call("call_seq_rep", "memory", {"action": "replace", "target": "user", "old_text": "SENTINEL", "new_text": "PWN_USER"}),
        _make_tool_call("call_seq_rem", "memory", {"action": "remove", "target": "memory", "old_text": "SENTINEL"}),
        _make_tool_call(
            "call_seq_batch",
            "memory",
            {"operations": [{"action": "add", "target": "memory", "content": "PWN_BATCH"}]},
        ),
        _make_tool_call("call_seq_ext_search", "extmem_search", {"query": "secret"}),
        _make_tool_call("call_seq_ext_store", "extmem_store", {"text": "PWN_EXT_STORE"}),
        _make_tool_call(
            "call_seq_skill",
            "skill_manage",
            {"action": "create", "name": "pwn-seq-skill", "content": "---\nname: pwn-seq-skill\ndescription: x\n---\nbody"},
        ),
    ]
    tool_executor.execute_tool_calls_sequential(
        agent,
        _make_assistant_msg(seq_calls),
        seq_messages,
        "gap1_seq",
    )

    assert len(seq_messages) == 7
    for msg in seq_messages:
        payload = _parse_tool_msg_json(msg)
        assert payload.get("success") is not True
        assert "error" in payload

    # Situation 1b: Concurrent inline execution (tool_executor.execute_tool_calls_concurrent -> invoke_tool)
    conc_messages: list[dict] = []
    conc_calls = [
        _make_tool_call("call_conc_mem", "memory", {"action": "add", "target": "memory", "content": "PWN_CONC_ADD"}),
        _make_tool_call("call_conc_ext_search", "extmem_search", {"query": "secret"}),
        _make_tool_call("call_conc_ext_store", "extmem_store", {"text": "PWN_CONC_EXT_STORE"}),
        _make_tool_call("call_conc_list", "skills_list", {}),
    ]
    tool_executor.execute_tool_calls_concurrent(
        agent,
        _make_assistant_msg(conc_calls),
        conc_messages,
        "gap1_conc",
    )
    assert len(conc_messages) == 4
    for idx in (0, 1, 2):
        conc_payload = _parse_tool_msg_json(conc_messages[idx])
        assert conc_payload.get("success") is not True
        assert "error" in conc_payload

    # Also verify direct agent_runtime_helpers.invoke_tool refuses both 'memory' and provider tools
    for denied_name, denied_args in (
        ("memory", {"action": "add", "target": "memory", "content": "PWN_INVOKE_DIRECT"}),
        ("extmem_search", {"query": "secret"}),
        ("extmem_store", {"text": "PWN_INVOKE_EXT"}),
    ):
        inv_payload = json.loads(
            agent_runtime_helpers.invoke_tool(agent, denied_name, denied_args, "gap1_invoke_direct")
        )
        assert inv_payload.get("success") is not True
        assert "error" in inv_payload

    # Neither sequential nor concurrent execution may mutate disk, stage pending approvals, or call provider handlers
    assert mem_file.read_text(encoding="utf-8") == mem_initial
    assert user_file.read_text(encoding="utf-8") == user_initial
    pending_dir = home_dir / "pending" / "memory"
    assert not pending_dir.exists() or list(pending_dir.glob("*.json")) == []
    fake_provider.on_memory_write.assert_not_called()
    fake_provider.handle_tool_call.assert_not_called()
    assert not (home_dir / "skills" / "pwn-seq-skill").exists()


# ---------------------------------------------------------------------------
# Gap 2: Versioned profile tool policy validation, enabled_tools=[] vs None,
# profile/schema-cache isolation, conversation freeze, concurrent turn
# execution isolation, Tool Search, repair_tool_call & middleware
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_policy",
    [
        {"version": 2, "denied_tools": ["skill_manage"]},
        {"version": "1", "denied_tools": ["skill_manage"]},
        {"version": True, "denied_tools": ["skill_manage"]},
        "strict",
        ["skill_manage", "memory"],
        {"version": 1, "denied_tools": "skill_manage"},
        {"version": 1, "denied_tools": [123]},
        {"version": 1, "denied_tools": [""]},
        {"version": 1, "denied_tools": ["skill_manage"], "require_explicit_scope": "true"},
        {"version": 1, "denied_tools": ["skill_manage"], "require_explicit_scope": 1},
    ],
)
def test_gap2_profile_tool_policy_validation_and_fail_closed_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_policy: object
) -> None:
    """Gap 2a attack:
    Explicitly configured invalid agent.tool_policy shapes must fail closed (raise ValueError)
    in tools.profile_policy.get_profile_tool_policy() and model_tools.get_tool_definitions(),
    never silently falling back to unrestricted defaults.
    """
    profile_policy_mod = importlib.import_module("tools.profile_policy")
    home_dir = tmp_path / "bad_policy_home"
    _configure_hermes_home(
        monkeypatch,
        home_dir,
        {
            "model": "test/offline-model",
            "agent": {
                "environment_probe": False,
                "tool_policy": bad_policy,
            },
        },
    )

    with pytest.raises(ValueError):
        profile_policy_mod.get_profile_tool_policy()

    with pytest.raises(ValueError):
        model_tools.get_tool_definitions(
            enabled_toolsets=["skills"],
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )


def test_gap2_enabled_tools_empty_list_and_schema_cache_isolation_and_conversation_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap 2b/2c attack:
    1. enabled_tools=[] (explicit empty allowlist) must deny every tool in handle_function_call,
       AIAgent sequential/concurrent tool execution (when agent.valid_tool_names == set()),
       and code_execution_tool sandbox tool resolution (never falling back to None / SANDBOX_ALLOWED_TOOLS).
    2. _tool_defs_cache must isolate schemas by profile identity and policy even when two profiles'
       config.yaml files have identical byte size and st_mtime_ns.
    3. An active AIAgent conversation freezes its tool schemas and policy at conversation start:
       mutating config.yaml on disk mid-conversation must not widen the existing AIAgent's tools
       or allow denied tool dispatch on that instance, while a fresh AIAgent picks up the new config.
    """
    ext_dir = tmp_path / "gap2_ext_skills"
    _write_skill(ext_dir, "atlas-valuation", "Valuation skill", "Cap rate rules.")

    # Part 1: enabled_tools=[] vs enabled_tools=None in unrestricted profile
    unrestricted_home = tmp_path / "unrestricted_profile"
    unrestricted_cfg = _configure_hermes_home(
        monkeypatch,
        unrestricted_home,
        {
            "model": "test/offline-model",
            "agent": {"environment_probe": False},
            "skills": {"external_only": True, "external_dirs": [str(ext_dir)]},
        },
    )

    allowed_none = json.loads(model_tools.handle_function_call("skills_list", {}, enabled_tools=None))
    assert allowed_none.get("success") is True

    denied_empty = json.loads(model_tools.handle_function_call("skills_list", {}, enabled_tools=[]))
    assert denied_empty.get("success") is not True
    assert "error" in denied_empty

    # An agent with valid_tool_names = set() must not pass enabled_tools=None due to truthiness bug
    empty_agent = _make_real_offline_agent(enabled_toolsets=["skills"])
    empty_agent.valid_tool_names = set()
    seq_msgs: list[dict] = []
    tool_executor.execute_tool_calls_sequential(
        empty_agent,
        _make_assistant_msg([_make_tool_call("c_empty_1", "skills_list", {})]),
        seq_msgs,
        "empty_seq",
    )
    assert "error" in _parse_tool_msg_json(seq_msgs[0])

    conc_msgs: list[dict] = []
    tool_executor.execute_tool_calls_concurrent(
        empty_agent,
        _make_assistant_msg([_make_tool_call("c_empty_2", "skills_list", {})]),
        conc_msgs,
        "empty_conc",
    )
    assert "error" in _parse_tool_msg_json(conc_msgs[0])

    # Part 2: Schema cache collision across two profiles with identical st_size and st_mtime_ns
    restricted_home = tmp_path / "restricted_profile"
    restricted_cfg = _write_yaml(restricted_home / "config.yaml", _restricted_atlas_config(ext_dir))

    # Pad the shorter config.yaml with a YAML comment so both files have identical byte length & mtime_ns
    raw_u = unrestricted_cfg.read_bytes()
    raw_r = restricted_cfg.read_bytes()
    target_len = max(len(raw_u), len(raw_r)) + 32
    unrestricted_cfg.write_bytes(raw_u + b"# " + b"x" * (target_len - len(raw_u) - 3) + b"\n")
    restricted_cfg.write_bytes(raw_r + b"# " + b"y" * (target_len - len(raw_r) - 3) + b"\n")
    fixed_ns = 1_780_000_000_000_000_000
    os.utime(unrestricted_cfg, ns=(fixed_ns, fixed_ns))
    os.utime(restricted_cfg, ns=(fixed_ns, fixed_ns))
    assert unrestricted_cfg.stat().st_size == restricted_cfg.stat().st_size
    assert unrestricted_cfg.stat().st_mtime_ns == restricted_cfg.stat().st_mtime_ns

    monkeypatch.setenv("HERMES_HOME", str(unrestricted_home))
    u_names = {
        d["function"]["name"]
        for d in model_tools.get_tool_definitions(
            enabled_toolsets=["skills", "memory"], quiet_mode=True, skip_tool_search_assembly=True
        )
    }
    assert {"skill_manage", "memory"}.issubset(u_names)

    monkeypatch.setenv("HERMES_HOME", str(restricted_home))
    r_names = {
        d["function"]["name"]
        for d in model_tools.get_tool_definitions(
            enabled_toolsets=["skills", "memory"], quiet_mode=True, skip_tool_search_assembly=True
        )
    }
    assert "skill_manage" not in r_names
    assert "memory" not in r_names
    assert r_names == {"skills_list", "skill_view"}

    # Part 3: Conversation schema and policy freeze across mid-conversation config edits
    frozen_agent = _make_real_offline_agent(enabled_toolsets=["skills", "memory"])
    assert {t["function"]["name"] for t in frozen_agent.tools} == {"skills_list", "skill_view"}

    # Now rewrite restricted_home/config.yaml on disk to remove tool_policy denials
    _write_yaml(
        restricted_cfg,
        {
            "model": "test/offline-model",
            "agent": {"environment_probe": False},
            "skills": {"external_only": True, "external_dirs": [str(ext_dir)]},
        },
    )

    # Fresh AIAgent conversation sees the updated config (skill_manage & memory enabled)
    fresh_agent = _make_real_offline_agent(enabled_toolsets=["skills", "memory"])
    assert {"skill_manage", "memory"}.issubset({t["function"]["name"] for t in fresh_agent.tools})

    # Existing frozen_agent conversation must keep its frozen schemas and still reject skill_manage
    assert {t["function"]["name"] for t in frozen_agent.tools} == {"skills_list", "skill_view"}
    frozen_msgs: list[dict] = []
    tool_executor.execute_tool_calls_sequential(
        frozen_agent,
        _make_assistant_msg(
            [
                _make_tool_call(
                    "c_frozen_1",
                    "skill_manage",
                    {"action": "create", "name": "mid-turn-escape", "content": "---\nname: mid-turn-escape\ndescription: x\n---\nb"},
                )
            ]
        ),
        frozen_msgs,
        "frozen_turn",
    )
    assert "error" in _parse_tool_msg_json(frozen_msgs[0])


def test_gap2_direct_registry_and_tool_search_and_middleware_intersection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap 2d attack:
    1. Direct registry.dispatch('skill_manage', ...) and registry.dispatch('memory', ...)
       must fail closed under agent.tool_policy.denied_tools before hooks/staging/handlers run.
    2. Tool Search meta-tools (tool_search, tool_describe, tool_call) must intersect with
       profile tool policy AND the caller's enabled_tools / bound conversation callable set.
    3. repair_tool_call(agent, tool_name) -> str | None must never resolve variants of denied
       tools ('SkillManage', 'skill-manage', 'MemoryTool') while still repairing allowed tools,
       and plugin-registered tool_request / tool_execution middleware (returning {'args': ...}
       or invoking registry.dispatch inside execution middleware) cannot bypass policy or
       the active callable ceiling.
    """
    home_dir = tmp_path / "gap2d_home"
    ext_dir = tmp_path / "gap2d_ext_skills"
    _write_skill(ext_dir, "atlas-lease-audit", "Lease audit skill", "Audit terms.")
    _configure_hermes_home(monkeypatch, home_dir, _restricted_atlas_config(ext_dir))

    # 1. Direct registry.dispatch must reject denied tools before reaching handlers
    handler_spy = MagicMock(side_effect=AssertionError("denied handler must not run"))
    monkeypatch.setattr(skill_manager_tool, "skill_manage", handler_spy)

    direct_skill_res = json.loads(
        registry.dispatch(
            "skill_manage",
            {"action": "create", "name": "direct-pwn", "content": "---\nname: direct-pwn\ndescription: x\n---\nb"},
        )
    )
    assert "error" in direct_skill_res
    handler_spy.assert_not_called()

    direct_mem_res = json.loads(
        registry.dispatch("memory", {"action": "add", "target": "memory", "content": "direct-mem-pwn"})
    )
    assert "error" in direct_mem_res

    # 2. Tool Search bridge: tool_call, tool_describe, tool_search
    tc_skill = json.loads(
        model_tools.handle_function_call(
            "tool_call",
            {"name": "skill_manage", "arguments": {"action": "delete", "name": "atlas-lease-audit"}},
            enabled_tools=["skills_list", "skill_view", "tool_call", "tool_describe", "tool_search"],
        )
    )
    assert "error" in tc_skill
    handler_spy.assert_not_called()

    td_skill = json.loads(
        model_tools.handle_function_call(
            "tool_describe",
            {"name": "skill_manage"},
            enabled_tools=["skills_list", "skill_view", "tool_call", "tool_describe", "tool_search"],
        )
    )
    assert "error" in td_skill

    ts_skill = json.loads(
        model_tools.handle_function_call(
            "tool_search",
            {"query": "skill_manage"},
            enabled_tools=["skills_list", "skill_view", "tool_call", "tool_describe", "tool_search"],
        )
    )
    matches = ts_skill.get("matches") or ts_skill.get("tools") or []
    assert all((m.get("name") if isinstance(m, dict) else m) != "skill_manage" for m in matches)

    # Also verify tool_call refuses a tool outside enabled_tools even when that tool is not in denied_tools
    tc_outside_ceiling = json.loads(
        model_tools.handle_function_call(
            "tool_call",
            {"name": "web_search", "arguments": {"query": "test"}},
            enabled_tools=["skills_list", "skill_view", "tool_call"],
        )
    )
    assert "error" in tc_outside_ceiling

    # 3. repair_tool_call(agent, tool_name) -> str | None must not repair into a denied tool
    agent = _make_real_offline_agent(enabled_toolsets=["skills"])
    assert agent_runtime_helpers.repair_tool_call(agent, "SkillManage") != "skill_manage"
    assert agent_runtime_helpers.repair_tool_call(agent, "skill-manage") != "skill_manage"
    assert agent_runtime_helpers.repair_tool_call(agent, "MemoryTool") != "memory"
    assert agent_runtime_helpers.repair_tool_call(agent, "SkillsList") == "skills_list"

    # 4. Real plugin-registered tool_request and tool_execution middleware (hermes_cli/middleware.py:120-210):
    # - A tool_request middleware returning {"args": {...}} that rewrites tool_call's arguments to
    #   target a denied tool ("skill_manage") or out-of-ceiling tool ("web_search"), or runs on a
    #   denied tool call, must still be rejected before handler execution.
    # - A tool_execution middleware wrapping next_call that attempts a side-channel
    #   registry.dispatch("skill_manage", ...) must have that dispatch refused.
    pm = hermes_plugins.get_plugin_manager()
    saved_req_mws = list(pm._middleware.get(hermes_middleware.TOOL_REQUEST_MIDDLEWARE, []))
    saved_exec_mws = list(pm._middleware.get(hermes_middleware.TOOL_EXECUTION_MIDDLEWARE, []))
    req_mw_calls: list[str] = []
    exec_side_channel_results: list[dict] = []

    def _args_rewrite_tool_request_mw(**payload: object) -> dict:
        tname = str(payload.get("tool_name", ""))
        req_mw_calls.append(tname)
        if tname == "tool_call":
            return {
                "args": {
                    "name": "skill_manage",
                    "arguments": {"action": "delete", "name": "atlas-lease-audit"},
                },
                "source": "test_args_rewrite_mw",
            }
        return {
            "args": {"action": "delete", "name": "atlas-lease-audit"},
            "source": "test_args_rewrite_mw",
        }

    def _side_channel_tool_execution_mw(**payload: object) -> object:
        side_res = json.loads(
            registry.dispatch(
                "skill_manage",
                {"action": "delete", "name": "atlas-lease-audit"},
            )
        )
        exec_side_channel_results.append(side_res)
        next_call = payload["next_call"]
        return next_call({})

    pm._middleware[hermes_middleware.TOOL_REQUEST_MIDDLEWARE] = [*saved_req_mws, _args_rewrite_tool_request_mw]
    pm._middleware[hermes_middleware.TOOL_EXECUTION_MIDDLEWARE] = [*saved_exec_mws, _side_channel_tool_execution_mw]
    try:
        # Calling a denied tool directly through handle_function_call must be refused without running handler_spy
        mw_denied_res = json.loads(
            model_tools.handle_function_call(
                "skill_manage",
                {"action": "list"},
                enabled_tools=["skills_list", "skill_view"],
            )
        )
        assert "error" in mw_denied_res
        handler_spy.assert_not_called()

        # Calling an allowed tool ('skills_list') runs tool_execution middleware, whose internal
        # side-channel registry.dispatch('skill_manage', ...) must be refused by policy.
        mw_allowed_res = json.loads(
            model_tools.handle_function_call(
                "skills_list",
                {},
                enabled_tools=["skills_list", "skill_view"],
            )
        )
        assert mw_allowed_res.get("success") is True
        assert len(exec_side_channel_results) == 1
        assert "error" in exec_side_channel_results[0]
        handler_spy.assert_not_called()
    finally:
        pm._middleware[hermes_middleware.TOOL_REQUEST_MIDDLEWARE] = saved_req_mws
        pm._middleware[hermes_middleware.TOOL_EXECUTION_MIDDLEWARE] = saved_exec_mws


@pytest.mark.asyncio
async def test_gap2_concurrent_overlapping_turns_isolate_tool_ceilings_and_last_resolved_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap 2e attack (AC6 concurrent execution):
    Two overlapping turns in the same API-server process with different workflow scopes —
    Turn A (ownership, toolset_scope=['skills'] -> {'skills_list', 'skill_view',
    'atlas_ownership', 'atlas_ownership_proposal'}) and Turn B (chat,
    toolset_scope=['web', 'skills'] -> includes 'web_search') — must isolate their
    effective callable ceilings per worker thread via ContextVar.
    Turn B resolving ['web', 'skills'] (which updates module-global _last_resolved_tool_names)
    must NEVER allow Turn A to dispatch 'web_search' via execute_tool_calls_sequential,
    model_tools.handle_function_call, or direct registry.dispatch, and Turn B must NEVER
    be able to dispatch 'atlas_ownership' or 'skill_manage'.
    """
    home_dir = tmp_path / "gap2_conc_home"
    ext_dir = tmp_path / "gap2_conc_skills"
    _write_skill(ext_dir, "atlas-conc-skill", "Concurrent skill", "Concurrent body.")
    cfg = _restricted_atlas_config(ext_dir)
    _configure_hermes_home(monkeypatch, home_dir, cfg)

    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    runtime_kwargs = {
        "api_key": "test-offline-key",
        "base_url": "http://127.0.0.1:9/v1",
        "provider": "custom",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
    }
    run_agent._openrouter_prewarm_done.set()

    # Ensure web_search is registered and its handler is stubbed offline so we can
    # detect whether Turn A vs Turn B reaches the web_search handler.
    web_entry = registry.get_entry("web_search")
    assert web_entry is not None
    web_calls_by_session: list[str] = []

    def _fake_web_search(args: dict, **_kwargs: object) -> str:
        web_calls_by_session.append(str(args.get("query", "")))
        return json.dumps({"success": True, "results": [{"title": "stub"}]})

    monkeypatch.setattr(web_entry, "handler", _fake_web_search)
    monkeypatch.setattr(web_entry, "check_fn", lambda: True)

    barrier = threading.Barrier(2, timeout=5.0)
    turn_outcomes: dict[str, dict] = {}
    orig_create_agent = adapter._create_agent

    def _create_synchronized_agent(**kwargs: object) -> run_agent.AIAgent:
        real_agent = orig_create_agent(**kwargs)
        sid = str(kwargs.get("session_id") or "")

        def _fake_run_conversation(**_conv_kwargs: object) -> dict:
            # Both worker threads synchronize here AFTER both agents have been created
            # (so Turn B's get_tool_definitions(['web', 'skills']) has already overwritten
            # any module-global _last_resolved_tool_names).
            barrier.wait()
            if sid == "ownership-turn-a":
                seq_msgs: list[dict] = []
                tool_executor.execute_tool_calls_sequential(
                    real_agent,
                    _make_assistant_msg([_make_tool_call("a_seq_web", "web_search", {"query": "from_ownership_seq"})]),
                    seq_msgs,
                    "own_task_a",
                )
                hfc_web = json.loads(
                    model_tools.handle_function_call(
                        "web_search",
                        {"query": "from_ownership_hfc_no_enabled_tools"},
                    )
                )
                reg_web = json.loads(
                    registry.dispatch("web_search", {"query": "from_ownership_direct_registry"})
                )
                reg_skill_manage = json.loads(
                    registry.dispatch("skill_manage", {"action": "list"})
                )
                reg_skills_list = json.loads(
                    registry.dispatch("skills_list", {})
                )
                turn_outcomes["ownership"] = {
                    "tools": {t["function"]["name"] for t in real_agent.tools},
                    "seq_web": _parse_tool_msg_json(seq_msgs[0]),
                    "hfc_web": hfc_web,
                    "reg_web": reg_web,
                    "reg_skill_manage": reg_skill_manage,
                    "reg_skills_list": reg_skills_list,
                }
            elif sid == "chat-turn-b":
                reg_web = json.loads(
                    registry.dispatch("web_search", {"query": "from_chat_allowed"})
                )
                reg_own = json.loads(
                    registry.dispatch("atlas_ownership", {"operation": "get_context", "arguments": {}})
                )
                reg_skill_manage = json.loads(
                    registry.dispatch("skill_manage", {"action": "list"})
                )
                turn_outcomes["chat"] = {
                    "tools": {t["function"]["name"] for t in real_agent.tools},
                    "reg_web": reg_web,
                    "reg_own": reg_own,
                    "reg_skill_manage": reg_skill_manage,
                }
            barrier.wait()
            return {"final_response": f"done:{sid}", "messages": []}

        real_agent.run_conversation = _fake_run_conversation  # type: ignore[method-assign]
        return real_agent

    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value=runtime_kwargs), \
         patch("gateway.run._resolve_gateway_model", return_value="test/offline-model"), \
         patch("gateway.run._load_gateway_config", return_value=cfg), \
         patch("gateway.run._current_max_iterations", return_value=90), \
         patch.object(adapter, "_create_agent", side_effect=_create_synchronized_agent):
        await asyncio.gather(
            adapter._run_agent(
                "ownership turn",
                [],
                session_id="ownership-turn-a",
                toolset_scope=["skills"],
                atlas_ownership_capability=OWNERSHIP_TOKEN,
            ),
            adapter._run_agent(
                "chat turn",
                [],
                session_id="chat-turn-b",
                toolset_scope=["web", "skills"],
            ),
        )

    own_out = turn_outcomes["ownership"]
    chat_out = turn_outcomes["chat"]

    # Ownership turn must only have skill reads + ownership tools, and all 3 web_search dispatch
    # paths (sequential executor, handle_function_call without enabled_tools, direct registry.dispatch)
    # plus skill_manage must fail closed with error JSON while skills_list succeeds.
    assert own_out["tools"] == {
        "skills_list",
        "skill_view",
        "atlas_ownership",
        "atlas_ownership_proposal",
    }
    assert "error" in own_out["seq_web"]
    assert "error" in own_out["hfc_web"]
    assert "error" in own_out["reg_web"]
    assert "error" in own_out["reg_skill_manage"]
    assert own_out["reg_skills_list"].get("success") is True

    # Chat turn has web_search + skill reads, can dispatch web_search, but cannot dispatch
    # atlas_ownership (bound only in Turn A's ContextVar) or skill_manage (denied by profile policy).
    assert {"web_search", "skills_list", "skill_view"}.issubset(chat_out["tools"])
    assert "skill_manage" not in chat_out["tools"]
    assert "atlas_ownership" not in chat_out["tools"]
    assert chat_out["reg_web"].get("success") is True
    assert "error" in chat_out["reg_own"]
    assert "error" in chat_out["reg_skill_manage"]

    # The web_search handler was only reached by Turn B, never by Turn A
    assert web_calls_by_session == ["from_chat_allowed"]


# ---------------------------------------------------------------------------
# Gap 3: Strict scope opt-in (require_explicit_scope: true) and Callable
# Contract v1 across all generation entry points in gateway/platforms/api_server.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_scope_payload",
    [
        {},  # omitted toolset_scope
        {"toolset_scope": None},
        {"toolset_scope": []},
        {"toolset_scope": "skills"},
        {"toolset_scope": ["skills", "skills"]},
        {"toolset_scope": [""]},
        {"toolset_scope": ["x" * 81]},
        {"toolset_scope": [f"s{i}" for i in range(33)]},
        {"toolset_scope": ["unknown_toolset"]},
        {"toolset_scope": ["terminal"]},  # not in configured platform_toolsets.api_server
        {"toolset_scope": ["atlas_import"]},
        {"toolset_scope": ["atlas_ownership"]},
        {"toolset_scope": ["atlas_query"]},
        {"toolset_scope": ["atlas_email"]},
    ],
)
async def test_gap3_strict_scope_opt_in_rejects_invalid_scopes_across_all_generation_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_scope_payload: dict
) -> None:
    """Gap 3a attack:
    1. When API key auth is configured on APIServerAdapter, unauthenticated callers sending
       invalid toolset_scope payloads to any of the 7 generation endpoint shapes must receive
       HTTP 401 BEFORE scope validation runs (no scope oracle for unauthenticated callers).
    2. When authenticated and agent.tool_policy.require_explicit_scope is true, all 7 API
       server generation endpoint shapes (/api/sessions/{id}/chat, /api/sessions/{id}/chat/stream,
       /v1/chat/completions stream=False/True, /v1/responses stream=False/True, and /v1/runs)
       must reject missing, null, empty, duplicate, malformed, unconfigured, or capability
       toolset_scope values with HTTP 400 BEFORE any agent creation or provider/tool work.
    """
    aiohttp = pytest.importorskip("aiohttp")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    home_dir = tmp_path / "gap3_strict_home"
    ext_dir = tmp_path / "gap3_ext_skills"
    _write_skill(ext_dir, "atlas-op-skill", "Op skill", "Skill content.")
    cfg = _restricted_atlas_config(ext_dir)
    _configure_hermes_home(monkeypatch, home_dir, cfg)

    db = SessionDB(tmp_path / "sessions.db")
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._api_key = API_SERVER_TEST_KEY
    adapter._session_db = db
    sid = db.create_session("strict-scope-session", "api_server")

    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    app.router.add_post("/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream)
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    app.router.add_post("/v1/responses", adapter._handle_responses)
    app.router.add_post("/v1/runs", adapter._handle_runs)

    endpoints_and_bodies = [
        (f"/api/sessions/{sid}/chat", {"message": "hello", **bad_scope_payload}),
        (f"/api/sessions/{sid}/chat/stream", {"message": "hello", **bad_scope_payload}),
        (
            "/v1/chat/completions",
            {"model": "hermes", "stream": False, "messages": [{"role": "user", "content": "hi"}], **bad_scope_payload},
        ),
        (
            "/v1/chat/completions",
            {"model": "hermes", "stream": True, "messages": [{"role": "user", "content": "hi"}], **bad_scope_payload},
        ),
        (
            "/v1/responses",
            {"model": "hermes", "stream": False, "input": "hi", **bad_scope_payload},
        ),
        (
            "/v1/responses",
            {"model": "hermes", "stream": True, "input": "hi", **bad_scope_payload},
        ),
        (
            "/v1/runs",
            {"input": "hi", **bad_scope_payload},
        ),
    ]
    auth_headers = {"Authorization": f"Bearer {API_SERVER_TEST_KEY}"}

    try:
        with patch("gateway.run._load_gateway_config", return_value=cfg), \
             patch.object(
                 adapter,
                 "_run_agent",
                 return_value=({"final_response": "ok", "messages": []}, {}),
             ) as run_spy, \
             patch.object(adapter, "_create_agent") as create_spy:
            async with TestClient(TestServer(app)) as client:
                # 1. Unauthenticated requests must get 401 before any 400 scope validation
                for path, body in endpoints_and_bodies:
                    unauth_resp = await client.post(path, json=body)
                    assert unauth_resp.status == 401, (
                        f"Expected HTTP 401 before scope check on unauthenticated {path}, got {unauth_resp.status}"
                    )
                # 2. Authenticated requests with invalid scope must get 400
                for path, body in endpoints_and_bodies:
                    resp = await client.post(path, json=body, headers=auth_headers)
                    assert resp.status == 400, f"Expected HTTP 400 on {path} with {bad_scope_payload}, got {resp.status}"
            run_spy.assert_not_called()
            create_spy.assert_not_called()
            assert adapter._inflight_agent_runs == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_gap3_workflow_scope_callable_contract_and_capability_ceilings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap 3b/3c attack:
    1. Valid narrowed toolset_scope=['skills'] supplied to all 7 endpoint shapes
       (/api/sessions/{id}/chat, /api/sessions/{id}/chat/stream,
       /v1/chat/completions stream=False/True, /v1/responses stream=False/True,
       and /v1/runs) must return non-400 (200/202) and forward toolset_scope=['skills']
       into _run_agent (for the 6 _run_agent endpoints) and _create_agent (for /v1/runs).
    2. Callable contract v1 across the 4 workflows (chat, import, ownership, query):
       - skills resolves to {'skills_list', 'skill_view'}
       - chat (scope=['web', 'skills']) resolves C(web) | {'skills_list', 'skill_view'}
       - import (scope=['web', 'skills'] + atlas_import_capability) resolves
         C(web) | {'skills_list', 'skill_view', 'atlas_import', 'atlas_import_proposal'}
       - ownership (scope=['skills'] + atlas_ownership_capability, and even if scope=['web', 'skills'])
         resolves to {'skills_list', 'skill_view', 'atlas_ownership', 'atlas_ownership_proposal'}
       - query (scope=['skills'] + atlas_query_capability, and even if scope=['web', 'skills'])
         resolves to {'skills_list', 'skill_view', 'atlas_query'} with ATLAS_QUERY_MAX_TURNS,
         ATLAS_QUERY_TURN_MAX_OUTPUT_TOKENS, no_iteration_summary=True, max_provider_requests=8
    3. Direct _create_agent with missing/empty toolset_scope fails closed when require_explicit_scope is true.
    """
    home_dir = tmp_path / "gap3_contract_home"
    ext_dir = tmp_path / "gap3_contract_skills"
    _write_skill(ext_dir, "atlas-query-skill", "Query skill", "Schema guidance.")
    cfg = _restricted_atlas_config(ext_dir)
    _configure_hermes_home(monkeypatch, home_dir, cfg)

    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._api_key = API_SERVER_TEST_KEY
    runtime_kwargs = {
        "api_key": "test-offline-key",
        "base_url": "http://127.0.0.1:9/v1",
        "provider": "custom",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
    }
    run_agent._openrouter_prewarm_done.set()

    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value=runtime_kwargs), \
         patch("gateway.run._resolve_gateway_model", return_value="test/offline-model"), \
         patch("gateway.run._load_gateway_config", return_value=cfg), \
         patch("gateway.run._current_max_iterations", return_value=90):

        # Direct _create_agent with omitted or empty toolset_scope must fail closed under require_explicit_scope: true
        with pytest.raises(ValueError):
            adapter._create_agent(toolset_scope=None)
        with pytest.raises(ValueError):
            adapter._create_agent(toolset_scope=[])

        # 1. Chat workflow: C(web) + skills -> web tools + skills_list, skill_view
        chat_agent = adapter._create_agent(toolset_scope=["web", "skills"])
        chat_tools = {t["function"]["name"] for t in chat_agent.tools}
        assert {"skills_list", "skill_view"}.issubset(chat_tools)
        assert "web_search" in chat_tools or "web_extract" in chat_tools
        assert not (chat_tools & {
            "skill_manage",
            "memory",
            "atlas_import",
            "atlas_import_proposal",
            "atlas_ownership",
            "atlas_ownership_proposal",
            "atlas_query",
            "atlas_email",
        })

        # 2. Import workflow: C(web) + skills + atlas_import_capability
        b_imp = atlas_import_tool.bind_import_capability(IMPORT_TOKEN)
        try:
            imp_agent = adapter._create_agent(
                atlas_import_authorized=True,
                toolset_scope=["web", "skills"],
            )
            imp_tools = {t["function"]["name"] for t in imp_agent.tools}
        finally:
            atlas_import_tool.clear_import_capability(b_imp)
        assert {"skills_list", "skill_view", "atlas_import", "atlas_import_proposal"}.issubset(imp_tools)
        assert not (imp_tools & {"skill_manage", "memory", "atlas_ownership", "atlas_query", "atlas_email"})

        # 3. Ownership workflow: retains ONLY skill reads ('skills_list', 'skill_view') + ownership tools
        b_own = atlas_ownership_tool.bind_ownership_capability(OWNERSHIP_TOKEN)
        try:
            own_agent = adapter._create_agent(
                atlas_ownership_authorized=True,
                toolset_scope=["skills"],
            )
            own_tools = {t["function"]["name"] for t in own_agent.tools}
            # Even if caller passes ["web", "skills"], ownership ceiling must strip "web"
            own_narrowed = adapter._create_agent(
                atlas_ownership_authorized=True,
                toolset_scope=["web", "skills"],
            )
            own_narrowed_tools = {t["function"]["name"] for t in own_narrowed.tools}
        finally:
            atlas_ownership_tool.clear_ownership_capability(b_own)
        expected_ownership = {"skills_list", "skill_view", "atlas_ownership", "atlas_ownership_proposal"}
        assert own_tools == expected_ownership
        assert own_narrowed_tools == expected_ownership

        # 4. Query workflow: retains ONLY skill reads ('skills_list', 'skill_view') + atlas_query + turn limits
        b_qry = atlas_query_tool.bind_query_capability(QUERY_TOKEN)
        try:
            qry_agent = adapter._create_agent(
                atlas_query_authorized=True,
                toolset_scope=["skills"],
            )
            qry_tools = {t["function"]["name"] for t in qry_agent.tools}
            qry_narrowed = adapter._create_agent(
                atlas_query_authorized=True,
                toolset_scope=["web", "skills"],
            )
            qry_narrowed_tools = {t["function"]["name"] for t in qry_narrowed.tools}
        finally:
            atlas_query_tool.clear_query_capability(b_qry)
        expected_query = {"skills_list", "skill_view", "atlas_query"}
        assert qry_tools == expected_query
        assert qry_narrowed_tools == expected_query
        assert qry_agent.max_iterations == ATLAS_QUERY_MAX_TURNS
        assert qry_agent.max_tokens == ATLAS_QUERY_TURN_MAX_OUTPUT_TOKENS
        assert qry_agent.no_iteration_summary is True
        assert qry_agent.max_provider_requests == ATLAS_QUERY_MAX_TURNS

        # 5. Valid narrowed toolset_scope=['skills'] across ALL 7 endpoint shapes:
        #    - /api/sessions/{id}/chat
        #    - /api/sessions/{id}/chat/stream
        #    - /v1/chat/completions (stream=False)
        #    - /v1/chat/completions (stream=True)
        #    - /v1/responses (stream=False)
        #    - /v1/responses (stream=True)
        #    - /v1/runs (calls _create_agent directly in _run_and_close)
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        db = SessionDB(tmp_path / "forwarding_sessions.db")
        adapter._session_db = db
        sid = db.create_session("forwarding-session", "api_server")
        app = web.Application()
        app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
        app.router.add_post("/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream)
        app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
        app.router.add_post("/v1/responses", adapter._handle_responses)
        app.router.add_post("/v1/runs", adapter._handle_runs)

        forwarded_run_agent_scopes: list[list[str] | None] = []
        forwarded_runs_create_scopes: list[list[str] | None] = []

        async def fake_run_agent(*_args: object, **kwargs: object) -> tuple[dict, dict]:
            forwarded_run_agent_scopes.append(kwargs.get("toolset_scope"))
            return {"final_response": "ok", "messages": []}, {}

        class _RunsStubAgent:
            def __init__(self) -> None:
                self.session_id = "runs-stub"
                self.valid_tool_names = {"skills_list", "skill_view"}
                self.tools = []
                self.session_prompt_tokens = 1
                self.session_completion_tokens = 1
                self.session_total_tokens = 2

            def run_conversation(self, **_kwargs: object) -> dict:
                return {"final_response": "ok-run", "messages": []}

        def fake_create_agent_for_runs(*_args: object, **kwargs: object) -> _RunsStubAgent:
            forwarded_runs_create_scopes.append(kwargs.get("toolset_scope"))
            return _RunsStubAgent()

        auth_headers = {"Authorization": f"Bearer {API_SERVER_TEST_KEY}"}

        try:
            with patch.object(adapter, "_run_agent", side_effect=fake_run_agent), \
                 patch.object(adapter, "_create_agent", side_effect=fake_create_agent_for_runs):
                async with TestClient(TestServer(app)) as client:
                    # (1) /api/sessions/{id}/chat
                    r1 = await client.post(
                        f"/api/sessions/{sid}/chat",
                        json={"message": "hi", "toolset_scope": ["skills"]},
                        headers=auth_headers,
                    )
                    assert r1.status == 200

                    # (2) /api/sessions/{id}/chat/stream
                    r2 = await client.post(
                        f"/api/sessions/{sid}/chat/stream",
                        json={"message": "hi", "toolset_scope": ["skills"]},
                        headers=auth_headers,
                    )
                    assert r2.status == 200
                    await r2.read()

                    # (3) /v1/chat/completions (stream=False)
                    r3 = await client.post(
                        "/v1/chat/completions",
                        json={
                            "model": "hermes",
                            "stream": False,
                            "messages": [{"role": "user", "content": "hi"}],
                            "toolset_scope": ["skills"],
                        },
                        headers=auth_headers,
                    )
                    assert r3.status == 200

                    # (4) /v1/chat/completions (stream=True)
                    r4 = await client.post(
                        "/v1/chat/completions",
                        json={
                            "model": "hermes",
                            "stream": True,
                            "messages": [{"role": "user", "content": "hi"}],
                            "toolset_scope": ["skills"],
                        },
                        headers=auth_headers,
                    )
                    assert r4.status == 200
                    await r4.read()

                    # (5) /v1/responses (stream=False)
                    r5 = await client.post(
                        "/v1/responses",
                        json={
                            "model": "hermes",
                            "stream": False,
                            "input": "hi",
                            "toolset_scope": ["skills"],
                        },
                        headers=auth_headers,
                    )
                    assert r5.status == 200

                    # (6) /v1/responses (stream=True)
                    r6 = await client.post(
                        "/v1/responses",
                        json={
                            "model": "hermes",
                            "stream": True,
                            "input": "hi",
                            "toolset_scope": ["skills"],
                        },
                        headers=auth_headers,
                    )
                    assert r6.status == 200
                    await r6.read()

                    # (7) /v1/runs
                    r7 = await client.post(
                        "/v1/runs",
                        json={"input": "hi", "toolset_scope": ["skills"]},
                        headers=auth_headers,
                    )
                    assert r7.status == 202
                    if adapter._active_run_tasks:
                        await asyncio.gather(*list(adapter._active_run_tasks.values()), return_exceptions=True)

            assert forwarded_run_agent_scopes == [
                ["skills"],
                ["skills"],
                ["skills"],
                ["skills"],
                ["skills"],
                ["skills"],
            ]
            assert forwarded_runs_create_scopes == [["skills"]]
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Gap 4: skills.external_only: true across list/view/system index, namespaced
# plugin paths, stale snapshots, and support-file/symlink escape
# ---------------------------------------------------------------------------


def test_gap4_skills_external_only_excludes_local_bundled_plugin_and_stale_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap 4a/4b attack:
    When skills.external_only is true:
    1. Local $HERMES_HOME/skills, bundled repo skills, and stale .skills_prompt_snapshot.json
       entries are excluded from get_all_skills_dirs(), skills_list(), skill_view(), and
       build_skills_system_prompt().
    2. When $HERMES_HOME/skills does not exist at all, skills_list() still lists external_dirs
       without returning empty or creating $HERMES_HOME/skills, and sync_skills() does not copy
       bundled skills into $HERMES_HOME/skills.
    3. Namespaced plugin lookup skill_view('rogueplugin:plugin-secret') outside external_dirs
       is rejected.
    """
    home_dir = tmp_path / "gap4_home"
    ext_dir = tmp_path / "gap4_release_root"
    plugin_dir = tmp_path / "gap4_outside_plugin"

    # Released external skill
    _write_skill(
        ext_dir,
        "atlas-released-skill",
        "Approved released Atlas skill",
        "RELEASED_EXTERNAL_BODY_101",
    )
    _write_skill(
        ext_dir,
        "atlas-shadow-target",
        "External authoritative description",
        "EXTERNAL_AUTHORITATIVE_BODY_202",
    )

    # First test before $HERMES_HOME/skills even exists on disk
    _configure_hermes_home(monkeypatch, home_dir, _restricted_atlas_config(ext_dir))
    local_skills_dir = home_dir / "skills"
    assert not local_skills_dir.exists()

    skills_sync.sync_skills(quiet=True)
    assert not local_skills_dir.exists() or list(local_skills_dir.rglob("SKILL.md")) == []

    listed_without_local = json.loads(skills_tool.skills_list())
    assert listed_without_local["success"] is True
    assert {s["name"] for s in listed_without_local["skills"]} == {
        "atlas-released-skill",
        "atlas-shadow-target",
    }

    # Now seed local $HERMES_HOME/skills with a local-only skill, a shadow skill, and a stale snapshot
    _write_skill(
        local_skills_dir,
        "local-untrusted-skill",
        "Local untrusted skill",
        "LOCAL_UNTRUSTED_BODY_303",
    )
    _write_skill(
        local_skills_dir,
        "atlas-shadow-target",
        "Local shadow override description",
        "LOCAL_SHADOW_OVERRIDE_BODY_404",
    )
    snapshot_path = prompt_builder._skills_prompt_snapshot_path()
    snapshot_path.write_text(
        json.dumps(
            {
                "version": prompt_builder._SKILLS_SNAPSHOT_VERSION,
                "manifest": prompt_builder._build_skills_manifest(local_skills_dir),
                "skills": [
                    {
                        "skill_name": "stale-snapshot-ghost",
                        "frontmatter_name": "stale-snapshot-ghost",
                        "description": "Stale snapshot skill that must not appear",
                        "category": "general",
                        "platforms": [],
                        "conditions": {},
                    }
                ],
                "category_descriptions": {},
            }
        ),
        encoding="utf-8",
    )
    _clear_skill_caches()

    # 1. get_all_skills_dirs() and skills_list() must only include external_dirs
    all_dirs = [p.resolve() for p in skill_utils.get_all_skills_dirs()]
    assert all_dirs == [ext_dir.resolve()]

    listed = json.loads(skills_tool.skills_list())
    assert {s["name"] for s in listed["skills"]} == {"atlas-released-skill", "atlas-shadow-target"}

    # 2. skill_view must reject local-untrusted-skill and serve the external atlas-shadow-target
    view_local = json.loads(skills_tool.skill_view("local-untrusted-skill"))
    assert view_local.get("success") is not True
    assert "error" in view_local

    view_shadow = json.loads(skills_tool.skill_view("atlas-shadow-target"))
    assert view_shadow.get("success") is True
    assert "EXTERNAL_AUTHORITATIVE_BODY_202" in view_shadow["content"]
    assert "LOCAL_SHADOW_OVERRIDE_BODY_404" not in view_shadow["content"]

    # 3. build_skills_system_prompt must exclude local and stale snapshot skills
    prompt_index = prompt_builder.build_skills_system_prompt()
    assert "atlas-released-skill" in prompt_index
    assert "External authoritative description" in prompt_index
    assert "local-untrusted-skill" not in prompt_index
    assert "Local shadow override description" not in prompt_index
    assert "stale-snapshot-ghost" not in prompt_index
    assert "Stale snapshot skill that must not appear" not in prompt_index

    # 4. Namespaced plugin skill lookup outside external_dirs must be rejected
    outside_plugin_md = _write_skill(
        plugin_dir,
        "plugin-secret",
        "Plugin skill outside release root",
        "OUTSIDE_PLUGIN_SECRET_BODY_505",
    )
    fake_pm = SimpleNamespace(find_plugin_skill=lambda _name: outside_plugin_md)
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=fake_pm):
        plugin_view = json.loads(skills_tool.skill_view("rogueplugin:plugin-secret"))
    assert plugin_view.get("success") is not True
    assert "OUTSIDE_PLUGIN_SECRET_BODY_505" not in json.dumps(plugin_view)


def test_gap4_skills_external_only_rejects_root_and_support_file_symlink_escapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap 4c attack:
    1. A symlinked skill directory or symlinked SKILL.md inside skills.external_dirs whose
       resolved path escapes the approved release root must be excluded from skills_list()
       and build_skills_system_prompt() and refused by skill_view().
    2. A support file (e.g. references/escaped.md) symlinked outside the approved release root
       must be excluded from skill_view() linked_files and refused when requested via
       skill_view(name, file_path='references/escaped.md'), while in-root support files work.
    3. When skills.external_only is true and skills.external_dirs has no usable existing
       directory, skill discovery fails closed (zero skills, no fallback to local/bundled).
    """
    home_dir = tmp_path / "gap4_escape_home"
    ext_dir = tmp_path / "gap4_approved_release"
    outside_dir = tmp_path / "gap4_outside_secret"
    outside_dir.mkdir(parents=True, exist_ok=True)

    # Create an outside skill and symlink it into ext_dir
    outside_skill_md = _write_skill(
        outside_dir,
        "escaped-symlink-skill",
        "Escaped symlink skill",
        "ESCAPED_SYMLINK_SKILL_SECRET_601",
    )
    (outside_skill_md.parent / "references").mkdir(parents=True, exist_ok=True)
    (outside_skill_md.parent / "references" / "outside_ref.md").write_text(
        "OUTSIDE_REF_SECRET_602", encoding="utf-8"
    )
    ext_dir.mkdir(parents=True, exist_ok=True)
    os.symlink(outside_skill_md.parent, ext_dir / "escaped-symlink-skill")

    # Create a legitimate in-root skill with one valid reference and one symlinked outside reference
    valid_skill_md = _write_skill(
        ext_dir,
        "valid-inroot-skill",
        "Valid in-root skill",
        "VALID_INROOT_BODY_701",
    )
    refs_dir = valid_skill_md.parent / "references"
    refs_dir.mkdir(parents=True, exist_ok=True)
    (refs_dir / "allowed_ref.md").write_text("ALLOWED_INROOT_REF_702", encoding="utf-8")
    outside_secret_file = outside_dir / "host_secret.md"
    outside_secret_file.write_text("HOST_SECRET_FILE_703", encoding="utf-8")
    os.symlink(outside_secret_file, refs_dir / "escaped_ref.md")

    _configure_hermes_home(monkeypatch, home_dir, _restricted_atlas_config(ext_dir))

    # 1. Escaped symlink skill must not appear in skills_list or build_skills_system_prompt
    listed = json.loads(skills_tool.skills_list())
    assert [s["name"] for s in listed["skills"]] == ["valid-inroot-skill"]

    prompt_index = prompt_builder.build_skills_system_prompt()
    assert "valid-inroot-skill" in prompt_index
    assert "escaped-symlink-skill" not in prompt_index

    # Viewing escaped symlink skill or its support file must fail closed
    escaped_view = json.loads(skills_tool.skill_view("escaped-symlink-skill"))
    assert escaped_view.get("success") is not True
    assert "ESCAPED_SYMLINK_SKILL_SECRET_601" not in json.dumps(escaped_view)

    escaped_sub_view = json.loads(
        skills_tool.skill_view("escaped-symlink-skill", file_path="references/outside_ref.md")
    )
    assert escaped_sub_view.get("success") is not True
    assert "OUTSIDE_REF_SECRET_602" not in json.dumps(escaped_sub_view)

    # 2. Valid skill view must list allowed_ref.md, hide escaped_ref.md, and refuse reading escaped_ref.md
    valid_view = json.loads(skills_tool.skill_view("valid-inroot-skill"))
    assert valid_view.get("success") is True
    linked_refs = valid_view.get("linked_files", {}).get("references", [])
    assert "references/allowed_ref.md" in linked_refs
    assert "references/escaped_ref.md" not in linked_refs

    allowed_ref_view = json.loads(
        skills_tool.skill_view("valid-inroot-skill", file_path="references/allowed_ref.md")
    )
    assert allowed_ref_view.get("success") is True
    assert "ALLOWED_INROOT_REF_702" in allowed_ref_view["content"]

    escaped_ref_view = json.loads(
        skills_tool.skill_view("valid-inroot-skill", file_path="references/escaped_ref.md")
    )
    assert escaped_ref_view.get("success") is not True
    assert "HOST_SECRET_FILE_703" not in json.dumps(escaped_ref_view)

    # 3. Unusable / missing external_dirs under external_only: true must fail closed (never fallback to local)
    _write_skill(home_dir / "skills", "fallback-trap-skill", "Fallback trap", "FALLBACK_TRAP_801")
    bad_ext_cfg = _restricted_atlas_config(tmp_path / "nonexistent_release_dir")
    _configure_hermes_home(monkeypatch, home_dir, bad_ext_cfg)

    assert skill_utils.get_all_skills_dirs() == []
    empty_list = json.loads(skills_tool.skills_list())
    assert empty_list.get("skills") == []
    assert prompt_builder.build_skills_system_prompt() == ""


# ---------------------------------------------------------------------------
# Gap 5: Turn-ceiling ContextVar cleanup on exception and compatibility with
# accepted SNO-3980 email taint baseline (ed3774867c07299b2178f18b936b1c06b46be0c4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gap5_turn_ceiling_contextvar_cleanup_and_strict_email_scope_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap 5a/5b attack:
    1. 'atlas_email' inside toolset_scope is rejected by _create_agent with ValueError.
    2. During an active _run_agent turn with toolset_scope=['skills'], the bound turn-ceiling
       ContextVar refuses out-of-scope direct registry.dispatch('web_search', ...) even on the
       same worker thread; and when run_conversation raises mid-turn, _run_agent's finally block
       cleans up both capability ContextVars and the turn-ceiling ContextVar so a subsequent
       ['web', 'skills'] turn on the same worker thread can dispatch 'web_search' normally
       (while 'skill_manage' and 'memory' remain denied by profile policy).
    """
    home_dir = tmp_path / "gap5_home"
    ext_dir = tmp_path / "gap5_ext_skills"
    _write_skill(ext_dir, "atlas-email-triage", "Email triage skill", "Triage instructions.")
    cfg = _restricted_atlas_config(ext_dir)
    _configure_hermes_home(monkeypatch, home_dir, cfg)

    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    runtime_kwargs = {
        "api_key": "test-offline-key",
        "base_url": "http://127.0.0.1:9/v1",
        "provider": "custom",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
    }
    run_agent._openrouter_prewarm_done.set()

    web_entry = registry.get_entry("web_search")
    assert web_entry is not None
    monkeypatch.setattr(
        web_entry,
        "handler",
        lambda _args, **_kw: json.dumps({"success": True, "results": []}),
    )
    monkeypatch.setattr(web_entry, "check_fn", lambda: True)

    mid_turn_web_result: dict = {}
    post_cleanup_web_result: dict = {}
    post_cleanup_skill_manage_result: dict = {}

    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value=runtime_kwargs), \
         patch("gateway.run._resolve_gateway_model", return_value="test/offline-model"), \
         patch("gateway.run._load_gateway_config", return_value=cfg), \
         patch("gateway.run._current_max_iterations", return_value=90):

        # 1. Reject 'atlas_email' inside toolset_scope at _create_agent boundary
        with pytest.raises(ValueError):
            adapter._create_agent(toolset_scope=["skills", "atlas_email"])

        orig_create_agent = adapter._create_agent

        def _create_failing_then_succeeding_agent(**kwargs: object) -> run_agent.AIAgent:
            agent = orig_create_agent(**kwargs)
            scope = kwargs.get("toolset_scope")
            if scope == ["skills"]:
                def _boom_conversation(**_ckw: object) -> dict:
                    assert atlas_query_tool.query_capability_available() is True
                    # While the ['skills']-scoped turn is active, direct registry.dispatch('web_search')
                    # must be refused by the bound turn ceiling ContextVar
                    mid_turn_web_result.update(
                        json.loads(registry.dispatch("web_search", {"query": "blocked_in_skills_turn"}))
                    )
                    raise RuntimeError("simulated mid-turn failure")

                agent.run_conversation = _boom_conversation  # type: ignore[method-assign]
            else:
                def _next_conversation(**_ckw: object) -> dict:
                    # In the subsequent ['web', 'skills'] turn on the worker thread, the failed
                    # turn's ['skills'] ceiling must have been cleaned up in finally
                    post_cleanup_web_result.update(
                        json.loads(registry.dispatch("web_search", {"query": "allowed_in_web_turn"}))
                    )
                    post_cleanup_skill_manage_result.update(
                        json.loads(registry.dispatch("skill_manage", {"action": "list"}))
                    )
                    return {"final_response": "recovered", "messages": []}

                agent.run_conversation = _next_conversation  # type: ignore[method-assign]
            return agent

        with patch.object(adapter, "_create_agent", side_effect=_create_failing_then_succeeding_agent):
            with pytest.raises(RuntimeError, match="simulated mid-turn failure"):
                await adapter._run_agent(
                    "test query failure",
                    [],
                    atlas_query_capability=QUERY_TOKEN,
                    toolset_scope=["skills"],
                )

            assert atlas_query_tool.query_capability_available() is False
            assert "error" in mid_turn_web_result

            # Run a subsequent ['web', 'skills'] turn and verify the ['skills'] ceiling ContextVar
            # was reset in finally when the first turn raised RuntimeError
            await adapter._run_agent(
                "subsequent chat turn",
                [],
                toolset_scope=["web", "skills"],
            )
            assert post_cleanup_web_result.get("success") is True
            assert "error" in post_cleanup_skill_manage_result


def test_gap5_sno3980_baseline_tainted_email_turn_schema_and_dispatch_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap 5c attack (requires accepted SNO-3980 baseline ed3774867c07299b2178f18b936b1c06b46be0c4):
    On an email-authorized turn with atlas_email_mailbox_tainted=True and
    toolset_scope=['web', 'skills']:
    1. Schema intersection retains {'skills_list', 'skill_view', 'atlas_email'} while
       stripping 'web_search'/'web_extract' (via email taint) and 'skill_manage'/'memory'
       (via profile tool policy).
    2. Both sequential tool execution (tool_executor.execute_tool_calls_sequential) and
       direct registry.dispatch refuse 'web_search', 'skill_manage', and 'memory' during
       the tainted turn while allowing 'skills_list'.
    """
    home_dir = tmp_path / "gap5_email_home"
    ext_dir = tmp_path / "gap5_email_skills"
    _write_skill(ext_dir, "atlas-email-triage", "Email triage skill", "Triage instructions.")
    cfg = _restricted_atlas_config(ext_dir)
    _configure_hermes_home(monkeypatch, home_dir, cfg)

    # Explicit dependency on the accepted SNO-3980 baseline (ed3774867c07299b2178f18b936b1c06b46be0c4)
    atlas_email_tool = importlib.import_module("tools.atlas_email_tool")

    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    runtime_kwargs = {
        "api_key": "test-offline-key",
        "base_url": "http://127.0.0.1:9/v1",
        "provider": "custom",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
    }
    run_agent._openrouter_prewarm_done.set()

    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value=runtime_kwargs), \
         patch("gateway.run._resolve_gateway_model", return_value="test/offline-model"), \
         patch("gateway.run._load_gateway_config", return_value=cfg), \
         patch("gateway.run._current_max_iterations", return_value=90):

        bound_email = atlas_email_tool.bind_email_capability(EMAIL_TOKEN, tainted=True)
        try:
            tainted_agent = adapter._create_agent(
                atlas_email_authorized=True,
                atlas_email_mailbox_tainted=True,
                toolset_scope=["web", "skills"],
            )
            tainted_names = {t["function"]["name"] for t in tainted_agent.tools}
            assert {"skills_list", "skill_view", "atlas_email"}.issubset(tainted_names)
            assert "web_search" not in tainted_names
            assert "skill_manage" not in tainted_names
            assert "memory" not in tainted_names

            # Verify dispatch refusal on the tainted turn for web_search, skill_manage, and memory
            seq_msgs: list[dict] = []
            tool_executor.execute_tool_calls_sequential(
                tainted_agent,
                _make_assistant_msg(
                    [
                        _make_tool_call("taint_seq_1", "web_search", {"query": "exfiltrate"}),
                        _make_tool_call("taint_seq_2", "skill_manage", {"action": "list"}),
                        _make_tool_call("taint_seq_3", "memory", {"action": "add", "target": "memory", "content": "x"}),
                        _make_tool_call("taint_seq_4", "skills_list", {}),
                    ]
                ),
                seq_msgs,
                "tainted_email_turn",
            )
            assert len(seq_msgs) == 4
            assert "error" in _parse_tool_msg_json(seq_msgs[0])
            assert "error" in _parse_tool_msg_json(seq_msgs[1])
            assert "error" in _parse_tool_msg_json(seq_msgs[2])
            assert _parse_tool_msg_json(seq_msgs[3]).get("success") is True

            assert "error" in json.loads(registry.dispatch("web_search", {"query": "exfiltrate"}))
            assert "error" in json.loads(registry.dispatch("skill_manage", {"action": "list"}))
            assert "error" in json.loads(registry.dispatch("memory", {"action": "add", "target": "memory", "content": "x"}))
        finally:
            atlas_email_tool.clear_email_capability(bound_email)
