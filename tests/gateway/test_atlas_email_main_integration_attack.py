"""SNO-3978 baseline-integration attack tests for SNO-3977 (pinned Hermes ed377 + main 976e25fc).

Tests integration/authority/egress boundaries and preservation of main ownership/query fixes
using disposable transports and real email/ownership/query/gateway input shapes.
"""

import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from model_tools import get_tool_definitions, handle_function_call
from tools import (
    atlas_email_tool as email_tool,
    atlas_import_tool as import_tool,
    atlas_ownership_tool as ownership_tool,
    atlas_query_tool as query_tool,
)
from tools.registry import registry
from test_atlas_ownership_tool import CONTRACT

EMAIL_TOKEN = "atmail_" + "a" * 32
OWN_TOKEN = "atown_" + "b" * 32
IMP_TOKEN = "atimp_" + "c" * 32
QRY_TOKEN = "atqry_" + "d" * 32

EMAIL_UNAVAILABLE = {"error": "Email tool unavailable"}
OWNERSHIP_UNAVAILABLE = {"error": "Ownership operation unavailable"}
QUERY_UNAVAILABLE = {"error": "Query operation unavailable"}
IMPORT_UNAVAILABLE = {"error": "Import operation unavailable"}
TAINT_REFUSED = {"error": "Web tools are switched off after reading email in this turn"}

PROPOSE = {"to": ["a@example.com"], "subject": "Hi", "body": "Hello"}
APPROVAL = {
    "status": "waiting_for_approval",
    "approval_id": "ap_1",
    "outside_recipients": ["a@example.com"],
}


def _tc(name, arguments):
    return SimpleNamespace(
        id="t",
        function=SimpleNamespace(
            name=name,
            arguments=arguments if isinstance(arguments, str) else json.dumps(arguments),
        ),
    )


def _run_tool(name, args=None):
    return json.loads(registry.dispatch(name, args or {}))


@pytest.fixture
def email_callback(monkeypatch):
    observed = []
    response = [lambda request: httpx.Response(200, json=APPROVAL)]
    real_client = httpx.Client

    def receive(request):
        observed.append(request)
        return response[0](request)

    def factory(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return real_client(transport=httpx.MockTransport(receive), **kwargs)

    monkeypatch.setattr(
        email_tool,
        "httpx",
        SimpleNamespace(Client=factory, Timeout=httpx.Timeout, HTTPError=httpx.HTTPError),
    )
    bound = email_tool.bind_email_capability(EMAIL_TOKEN)
    yield observed, response
    email_tool.clear_email_capability(bound)


@pytest.fixture
def ownership_callback(monkeypatch):
    observed = []
    response = [
        lambda request: httpx.Response(
            200,
            json={
                "protocol": CONTRACT["protocol"],
                "ok": True,
                "operation": json.loads(request.content)["operation"],
                "result": {},
            },
        )
    ]
    real_client = httpx.Client

    def receive(request):
        observed.append(request)
        return response[0](request)

    def factory(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return real_client(transport=httpx.MockTransport(receive), **kwargs)

    monkeypatch.setattr(
        ownership_tool,
        "httpx",
        SimpleNamespace(Client=factory, Timeout=httpx.Timeout, HTTPError=httpx.HTTPError),
    )
    yield observed, response


@pytest.fixture
def import_callback(monkeypatch):
    observed = []
    response = [lambda request: httpx.Response(200, json={"ok": True, "items": []})]
    real_client = httpx.Client

    def receive(request):
        observed.append(request)
        return response[0](request)

    def factory(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return real_client(transport=httpx.MockTransport(receive), **kwargs)

    monkeypatch.setattr(
        import_tool,
        "httpx",
        SimpleNamespace(Client=factory, Timeout=httpx.Timeout, HTTPError=httpx.HTTPError),
    )
    yield observed, response


# ---- (a.1) Synchronous /api/sessions/{session_id}/chat capability & taint parity ------------


async def _post_session_chat(tmp_path, path_suffix, fields):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "sessions.db")
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._session_db = db
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    app.router.add_post(
        "/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream
    )
    sid = db.create_session("session-parity", "api_server")
    captured = []

    async def fake_run(*args, **kwargs):
        captured.append(kwargs)
        return {"final_response": "ok", "messages": []}, {}

    try:
        with patch.object(adapter, "_run_agent", side_effect=fake_run), patch(
            "gateway.run._load_gateway_config", return_value={}
        ), patch(
            "hermes_cli.tools_config._get_platform_tools",
            return_value={
                "web",
                "terminal",
                "todo",
                "file",
                "atlas_import",
                "atlas_ownership",
                "atlas_query",
                "atlas_email",
            },
        ):
            async with TestClient(TestServer(app)) as client:
                response = await client.post(
                    f"/api/sessions/{sid}{path_suffix}",
                    json={"message": "test", **fields},
                )
                body_text = await response.text()
                return response.status, body_text, captured
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/chat", "/chat/stream"])
@pytest.mark.parametrize(
    "fields",
    [
        {"atlas_email_capability": "bad"},
        {"atlas_email_capability": 42},
        {"atlas_email_capability": QRY_TOKEN},
        {"atlas_email_capability": EMAIL_TOKEN, "atlas_import_capability": IMP_TOKEN},
        {"atlas_email_capability": EMAIL_TOKEN, "atlas_ownership_capability": OWN_TOKEN},
        {"atlas_email_capability": EMAIL_TOKEN, "atlas_query_capability": QRY_TOKEN},
        {"atlas_email_mailbox_tainted": False},
        {"atlas_email_mailbox_tainted": "true"},
        {"atlas_email_mailbox_tainted": 1},
        {"toolset_scope": ["atlas_email"]},
    ],
)
async def test_session_chat_endpoints_refuse_malformed_or_combined_capabilities(
    tmp_path, endpoint, fields
):
    pytest.importorskip("aiohttp")
    status, body_text, captured = await _post_session_chat(tmp_path, endpoint, fields)
    assert status == 400
    for secret in (EMAIL_TOKEN, OWN_TOKEN, IMP_TOKEN, QRY_TOKEN):
        assert secret not in body_text
    assert captured == []


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/chat", "/chat/stream"])
@pytest.mark.parametrize(
    "fields,expected_cap_key,expected_cap_val,expected_tainted,expected_scope",
    [
        (
            {
                "atlas_email_capability": EMAIL_TOKEN,
                "toolset_scope": ["web"],
            },
            "atlas_email_capability",
            EMAIL_TOKEN,
            False,
            ["web"],
        ),
        (
            {
                "atlas_email_capability": EMAIL_TOKEN,
                "atlas_email_mailbox_tainted": True,
                "toolset_scope": ["web", "todo"],
            },
            "atlas_email_capability",
            EMAIL_TOKEN,
            True,
            ["web", "todo"],
        ),
        (
            {"atlas_email_mailbox_tainted": True},
            "atlas_email_capability",
            None,
            True,
            None,
        ),
        (
            {
                "atlas_ownership_capability": OWN_TOKEN,
                "atlas_email_mailbox_tainted": True,
            },
            "atlas_ownership_capability",
            OWN_TOKEN,
            True,
            None,
        ),
        (
            {
                "atlas_query_capability": QRY_TOKEN,
                "atlas_email_mailbox_tainted": True,
            },
            "atlas_query_capability",
            QRY_TOKEN,
            True,
            None,
        ),
    ],
)
async def test_session_chat_endpoints_forward_valid_capabilities_taint_and_scope(
    tmp_path,
    endpoint,
    fields,
    expected_cap_key,
    expected_cap_val,
    expected_tainted,
    expected_scope,
):
    pytest.importorskip("aiohttp")
    status, _, captured = await _post_session_chat(tmp_path, endpoint, fields)
    assert status == 200 and len(captured) == 1
    call_kwargs = captured[0]
    assert call_kwargs.get(expected_cap_key) == expected_cap_val
    assert call_kwargs.get("atlas_email_mailbox_tainted") is expected_tainted
    assert call_kwargs.get("toolset_scope") == expected_scope


# ---- (a.2) MCP, external-network, and composite toolsets blocked when tainted ---------------


@pytest.fixture
def extended_egress_probes():
    ran = []
    probes = (
        ("zz_mcp_github_probe", "mcp-github"),
        ("zz_search_probe", "search"),
        ("zz_tts_probe", "tts"),
        ("zz_spotify_probe", "spotify"),
        ("zz_discord_probe", "discord"),
        ("zz_discord_admin_probe", "discord_admin"),
        ("zz_yuanbao_probe", "yuanbao"),
        ("zz_feishu_doc_probe", "feishu_doc"),
        ("zz_feishu_drive_probe", "feishu_drive"),
        ("zz_computer_use_probe", "computer_use"),
        ("zz_todo_control_probe", "todo"),
    )
    for name, toolset in probes:
        registry.register(
            name=name,
            toolset=toolset,
            schema={
                "name": name,
                "description": "probe",
                "parameters": {"type": "object", "properties": {}},
            },
            handler=lambda a, _n=name, **k: (ran.append(_n), json.dumps({"ok": _n}))[1],
        )
    yield ran, [name for name, ts in probes if ts != "todo"]
    for name, _ in probes:
        registry.deregister(name)


def test_tainted_run_refuses_mcp_and_external_toolsets_while_preserving_local_control(
    email_callback, extended_egress_probes
):
    _, response = email_callback
    ran, blocked_names = extended_egress_probes

    # Control before any mailbox read: propose does not taint, and probes can run.
    assert json.loads(
        handle_function_call("atlas_email", {"operation": "propose", "arguments": PROPOSE})
    ) == APPROVAL
    assert _run_tool("zz_mcp_github_probe") == {"ok": "zz_mcp_github_probe"}
    assert _run_tool("zz_todo_control_probe") == {"ok": "zz_todo_control_probe"}
    ran.clear()

    # Reading mailbox content taints the run.
    response[0] = lambda request: httpx.Response(
        200, json={"message": {"id": "m1", "body": "Untrusted body"}}
    )
    read_result = json.loads(
        handle_function_call(
            "atlas_email", {"operation": "read_message", "arguments": {"message_id": "m1"}}
        )
    )
    assert "untrusted_email_content" in read_result

    for blocked_name in blocked_names:
        assert _run_tool(blocked_name) == TAINT_REFUSED
    assert _run_tool("zz_todo_control_probe") == {"ok": "zz_todo_control_probe"}
    assert ran == ["zz_todo_control_probe"]


def test_create_agent_strips_mcp_composite_and_external_toolsets_when_mailbox_tainted():
    pytest.importorskip("aiohttp")
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    configured_toolsets = {
        "web",
        "search",
        "tts",
        "spotify",
        "discord",
        "discord_admin",
        "yuanbao",
        "feishu_doc",
        "feishu_drive",
        "computer_use",
        "debugging",
        "safe",
        "coding",
        "mcp-github",
        "github",
        "file",
        "memory",
        "skills",
        "session_search",
        "kanban",
        "project",
        "google_meet",
        "todo",
    }
    with patch(
        "gateway.run._resolve_runtime_agent_kwargs",
        return_value={
            "api_key": "synthetic",
            "base_url": None,
            "provider": None,
            "api_mode": None,
            "command": None,
            "args": [],
        },
    ), patch("gateway.run._resolve_gateway_model", return_value="test/model"), patch(
        "gateway.run._load_gateway_config",
        return_value={"mcp_servers": {"github": {"enabled": True}}},
    ), patch(
        "hermes_cli.tools_config._get_platform_tools", return_value=configured_toolsets
    ), patch(
        "gateway.run._current_max_iterations", return_value=90
    ), patch(
        "run_agent.AIAgent"
    ) as agent:
        # Control: untainted run retains configured non-atlas toolsets (including file/memory/skills/session_search).
        adapter._create_agent(atlas_email_authorized=True)
        untainted = set(agent.call_args.kwargs["enabled_toolsets"])
        assert {
            "atlas_email",
            "web",
            "mcp-github",
            "todo",
            "file",
            "memory",
            "skills",
            "session_search",
        } <= untainted

        # Tainted run strips egress, persistence (file, memory, skills), transcript-reader (session_search),
        # multi-agent/plugin (kanban, project, google_meet), composite, and MCP toolsets, keeping only allow-listed toolsets.
        adapter._create_agent(
            atlas_email_authorized=True, atlas_email_mailbox_tainted=True
        )
        tainted = sorted(agent.call_args.kwargs["enabled_toolsets"])
        assert tainted == ["atlas_email", "todo"]

        # Pinned ed377 control (not d744): tainted import turn keeps atlas_import and todo only.
        adapter._create_agent(
            atlas_import_authorized=True, atlas_email_mailbox_tainted=True
        )
        tainted_import = sorted(agent.call_args.kwargs["enabled_toolsets"])
        assert tainted_import == ["atlas_import", "todo"]


# ---- (a.3) Same-batch taint pre-scan under Tool Search bridge & single-line message_id ------


@pytest.fixture
def web_probe():
    ran = []
    registry.register(
        name="zz_web_probe",
        toolset="web",
        schema={
            "name": "zz_web_probe",
            "description": "probe",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda a, **k: (ran.append("zz_web_probe"), json.dumps({"ok": "zz_web_probe"}))[1],
    )
    yield ran
    registry.deregister("zz_web_probe")


@pytest.mark.parametrize(
    "bridge_read_call",
    [
        _tc(
            "tool_call",
            {
                "name": " atlas_email ",
                "arguments": {"operation": "read_message", "arguments": {"message_id": "m1"}},
            },
        ),
        _tc(
            "tool_call",
            {
                "name": "atlas_email\n",
                "arguments": json.dumps({"operation": "list_messages", "arguments": {}}),
            },
        ),
        _tc(
            "tool_call",
            {
                "name": "tool_call",
                "arguments": {
                    "name": "atlas_email",
                    "arguments": {"operation": "read_message", "arguments": {"message_id": "m1"}},
                },
            },
        ),
        _tc("tool_call", "{not valid json"),
    ],
)
def test_batch_pre_scan_taints_whitespace_padded_and_malformed_bridge_reads(
    email_callback, web_probe, bridge_read_call
):
    from tools import tool_search

    # Control: resolve_underlying_call accepts whitespace-padded " atlas_email ".
    resolved_name, _, err = tool_search.resolve_underlying_call(
        {"name": " atlas_email ", "arguments": {"operation": "propose", "arguments": PROPOSE}}
    )
    assert err is None and resolved_name == "atlas_email"

    # Control: whitespace-padded bridge propose does NOT taint the batch.
    propose_bridge = _tc(
        "tool_call",
        {"name": " atlas_email ", "arguments": {"operation": "propose", "arguments": PROPOSE}},
    )
    assert (
        email_tool.taint_batch_if_it_reads_mailbox(
            [_tc("zz_web_probe", {}), propose_bridge]
        )
        is False
    )
    assert _run_tool("zz_web_probe") == {"ok": "zz_web_probe"}
    web_probe.clear()

    # Defeating batch: egress tool followed by whitespace-padded or malformed bridge read.
    assert (
        email_tool.taint_batch_if_it_reads_mailbox(
            [_tc("zz_web_probe", {}), bridge_read_call]
        )
        is True
    )
    assert _run_tool("zz_web_probe") == TAINT_REFUSED
    assert web_probe == []


@pytest.mark.parametrize(
    "bad_args",
    [
        {"operation": "read_message", "arguments": {"message_id": "m1\r\nX-Injected: 1"}},
        {"operation": "read_message", "arguments": {"message_id": "m1\nsecond"}},
        {"operation": "read_message", "arguments": {"message_id": "m1\rsecond"}},
        {
            "operation": "propose",
            "arguments": {**PROPOSE, "in_reply_to": {"message_id": "m1\r\nBcc: x@y.z"}},
        },
        {
            "operation": "propose",
            "arguments": {**PROPOSE, "in_reply_to": {"message_id": "m1\nsecond"}},
        },
    ],
)
def test_multiline_message_id_is_refused_before_callback(email_callback, bad_args):
    observed, response = email_callback
    assert json.loads(handle_function_call("atlas_email", bad_args)) == EMAIL_UNAVAILABLE
    assert observed == []

    # Control: single-line message_id still reaches the callback.
    response[0] = lambda request: httpx.Response(200, json={"message": {"id": "m1"}})
    ok = json.loads(
        handle_function_call(
            "atlas_email", {"operation": "read_message", "arguments": {"message_id": "m1"}}
        )
    )
    assert ok["untrusted_email_content"] == {"message": {"id": "m1"}}
    assert len(observed) == 1


# ---- (a.4) Cross-tool token scrubbing, authority keys, and main SNO-2018 R1 preservation ----


@pytest.mark.parametrize("leaked_token", [EMAIL_TOKEN, OWN_TOKEN, IMP_TOKEN, QRY_TOKEN])
def test_all_atlas_tools_scrub_every_capability_prefix_in_requests_and_responses(
    leaked_token, email_callback, ownership_callback, import_callback, monkeypatch
):
    email_obs, email_resp = email_callback
    own_obs, own_resp = ownership_callback
    imp_obs, imp_resp = import_callback

    # 1. Ownership tool refuses every token prefix in request and in 200 response.
    own_bound = ownership_tool.bind_ownership_capability(OWN_TOKEN)
    try:
        req = copy.deepcopy(CONTRACT["transformation_case"]["requests"][0])
        req["arguments"]["proposal"]["transformations"][0][
            "proposed_value"
        ] = leaked_token
        res = json.loads(
            handle_function_call(
                "atlas_ownership_proposal", {"operation": "propose", **req["arguments"]}
            )
        )
        assert "error" in res and leaked_token not in json.dumps(res)
        assert own_obs == []

        own_resp[0] = lambda request: httpx.Response(
            200,
            json={
                "protocol": CONTRACT["protocol"],
                "ok": True,
                "operation": "catalog",
                "result": {"leak": leaked_token},
            },
        )
        assert (
            json.loads(
                ownership_tool._handle_atlas_ownership(
                    {"operation": "catalog", "arguments": {}}
                )
            )
            == OWNERSHIP_UNAVAILABLE
        )
    finally:
        ownership_tool.clear_ownership_capability(own_bound)

    # 2. Import tool refuses every token prefix in request, 200 response, and 422 detail.
    imp_bound = import_tool.bind_import_capability(IMP_TOKEN)
    try:
        imp_obs.clear()
        bad_lookup = json.loads(
            import_tool._handle_atlas_import(
                {"operation": "lookup", "arguments": {"property_query": leaked_token}}
            )
        )
        assert "error" in bad_lookup and leaked_token not in json.dumps(bad_lookup)
        assert imp_obs == []

        imp_resp[0] = lambda request: httpx.Response(200, json={"leak": leaked_token})
        res_200 = json.loads(
            import_tool._handle_atlas_import({"operation": "evidence", "arguments": {}})
        )
        assert res_200 == IMPORT_UNAVAILABLE and leaked_token not in json.dumps(res_200)

        imp_resp[0] = lambda request: httpx.Response(
            422, json={"detail": f" rejected {leaked_token}"}
        )
        res_422 = json.loads(
            import_tool._handle_atlas_import({"operation": "evidence", "arguments": {}})
        )
        assert res_422 == IMPORT_UNAVAILABLE and leaked_token not in json.dumps(res_422)

        # Control: valid short printable 422 detail without a token still passes through.
        imp_resp[0] = lambda request: httpx.Response(
            422, json={"detail": "property_id was not read in this turn"}
        )
        assert json.loads(
            import_tool._handle_atlas_import({"operation": "evidence", "arguments": {}})
        ) == {"error": "property_id was not read in this turn"}
    finally:
        import_tool.clear_import_capability(imp_bound)


@pytest.mark.parametrize(
    "authority_field",
    [
        "atlas_email_capability",
        "atlas_email_mailbox_tainted",
        "atlas_query_capability",
        "atlas_ownership_capability",
        "atlas_import_capability",
    ],
)
def test_ownership_proposal_refuses_all_atlas_capability_authority_keys(
    authority_field, ownership_callback
):
    own_obs, _ = ownership_callback
    request = copy.deepcopy(CONTRACT["transformation_case"]["requests"][0])
    request["arguments"]["proposal"]["transformations"][0]["field"] = authority_field
    bound = ownership_tool.bind_ownership_capability(OWN_TOKEN)
    try:
        result = json.loads(
            handle_function_call(
                "atlas_ownership_proposal", {"operation": "propose", **request["arguments"]}
            )
        )
        assert result == {
            "error": "arguments.proposal.transformations[].field: not accepted"
        }
    finally:
        ownership_tool.clear_ownership_capability(bound)
    assert own_obs == []


def test_ownership_and_query_single_tool_boundaries_exclude_atlas_email():
    pytest.importorskip("aiohttp")
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    with patch(
        "gateway.run._resolve_runtime_agent_kwargs",
        return_value={
            "api_key": "synthetic",
            "base_url": None,
            "provider": None,
            "api_mode": None,
            "command": None,
            "args": [],
        },
    ), patch("gateway.run._resolve_gateway_model", return_value="test/model"), patch(
        "gateway.run._load_gateway_config", return_value={}
    ), patch(
        "hermes_cli.tools_config._get_platform_tools",
        return_value={"web", "terminal", "atlas_ownership", "atlas_query", "atlas_email"},
    ), patch(
        "gateway.run._current_max_iterations", return_value=90
    ), patch(
        "run_agent.AIAgent"
    ) as agent:
        adapter._create_agent(atlas_ownership_authorized=True, atlas_email_authorized=True)
        assert agent.call_args.kwargs["enabled_toolsets"] == ["atlas_ownership"]

        adapter._create_agent(atlas_query_authorized=True, atlas_email_authorized=True)
        assert agent.call_args.kwargs["enabled_toolsets"] == ["atlas_query"]


def test_main_sno2018_r1_ownership_contract_and_query_fixes_survive_email_integration(
    ownership_callback,
):
    own_obs, own_resp = ownership_callback
    request = copy.deepcopy(CONTRACT["transformation_case"]["requests"][0])
    for item in request["arguments"]["proposal"]["transformations"]:
        for optional_key in ("depends_on", "guidance_revision", "catalog_revision"):
            item.pop(optional_key, None)

    email_bound = email_tool.bind_email_capability("", tainted=True)
    own_bound = ownership_tool.bind_ownership_capability(OWN_TOKEN)
    try:
        # Even when the session is mailbox-tainted, ownership R1 semantic ingress and rule envelopes work.
        defs = get_tool_definitions(
            enabled_toolsets=["atlas_ownership"], quiet_mode=True, skip_tool_search_assembly=True
        )
        assert {d["function"]["name"] for d in defs} == {
            "atlas_ownership",
            "atlas_ownership_proposal",
        }
        assert json.loads(
            handle_function_call(
                "atlas_ownership_proposal", {"operation": "propose", **request["arguments"]}
            )
        ) == {
            "protocol": CONTRACT["protocol"],
            "ok": True,
            "operation": "propose",
            "result": {},
        }
        for code, rule in (
            ("OWNERSHIP_EVIDENCE_MISMATCH", "plan_compiled_by_atlas"),
            ("OWNERSHIP_SHAPE_INVALID", "record_targets_shape"),
            ("OWNERSHIP_TOO_LARGE", "json_bytes_max_65536"),
        ):
            envelope = {
                "protocol": CONTRACT["protocol"],
                "ok": False,
                "error": {"code": code, "rule": rule},
            }
            own_resp[0] = lambda req, _env=envelope: httpx.Response(200, json=_env)
            assert (
                json.loads(
                    handle_function_call(
                        "atlas_ownership_proposal",
                        {"operation": "propose", **request["arguments"]},
                    )
                )
                == envelope
            )
    finally:
        ownership_tool.clear_ownership_capability(own_bound)
        email_tool.clear_email_capability(email_bound)
    assert len(own_obs) == 4

# ---- SNO-3978 Defensive Addendum (SNO-3980 repair coverage) ---------------------------------


def test_tainted_run_refuses_persistence_writes_across_all_dispatch_paths_while_preserving_read_only_controls(
    tmp_path, monkeypatch, email_callback
):
    from agent.agent_runtime_helpers import invoke_tool
    from agent.memory_manager import MemoryManager
    from agent.memory_provider import MemoryProvider
    from agent.tool_executor import execute_tool_calls_sequential
    from tools.file_tools import patch_tool, write_file_tool
    from tools.memory_tool import MemoryStore, memory_tool
    from tools.skill_manager_tool import skill_manage

    _, response = email_callback
    hermes_home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    store = MemoryStore()
    store.load_from_disk()
    notes_file = tmp_path / "workspace_notes.txt"

    skill_doc = (
        "---\n"
        "name: clean-skill\n"
        "description: Disposable clean skill fixture\n"
        "---\n"
        "# Clean Skill\n"
        "Step 1: Inspect local state.\n"
    )

    # 1. Clean-run controls BEFORE mailbox taint: persistence writes and read-only tools succeed.
    assert json.loads(
        memory_tool(action="add", target="memory", content="clean memory entry", store=store)
    )["success"] is True
    assert store.memory_entries == ["clean memory entry"]

    assert json.loads(
        handle_function_call(
            "skill_manage",
            {"action": "create", "name": "clean-skill", "content": skill_doc},
        )
    )["success"] is True

    assert "error" not in json.loads(
        handle_function_call(
            "write_file",
            {"path": str(notes_file), "content": "alpha\nbeta\n"},
            task_id="persist-control",
        )
    )
    assert "error" not in json.loads(
        handle_function_call(
            "patch",
            {
                "mode": "replace",
                "path": str(notes_file),
                "old_string": "beta",
                "new_string": "gamma",
            },
            task_id="persist-control",
        )
    )
    assert notes_file.read_text(encoding="utf-8") == "alpha\ngamma\n"

    # 2. Reading mailbox content taints the current run.
    response[0] = lambda request: httpx.Response(
        200, json={"message": {"id": "m1", "body": "Untrusted mailbox text"}}
    )
    read_out = json.loads(
        handle_function_call(
            "atlas_email", {"operation": "read_message", "arguments": {"message_id": "m1"}}
        )
    )
    assert "untrusted_email_content" in read_out
    assert email_tool.mailbox_read_this_run() is True

    # 3. Direct, registry, and handle_function_call persistence writes are refused while tainted.
    # Note: _handle_memory pops `store` from `kw` (not `args`), so `registry.dispatch("memory", ...)`
    # must be refused by `refuse_if_tainted` at the registry dispatch boundary before `_handle_memory` runs.
    for mem_kwargs in (
        {"action": "add", "target": "memory", "content": "tainted entry"},
        {"action": "add", "target": "user", "content": "tainted user entry"},
        {
            "action": "replace",
            "target": "memory",
            "old_text": "clean memory entry",
            "content": "tainted replacement",
        },
        {"action": "remove", "target": "memory", "old_text": "clean memory entry"},
        {
            "target": "memory",
            "operations": [{"action": "add", "content": "tainted batch entry"}],
        },
    ):
        assert json.loads(memory_tool(**mem_kwargs, store=store)) == TAINT_REFUSED
        assert json.loads(registry.dispatch("memory", {**mem_kwargs, "store": store})) == TAINT_REFUSED

    for skill_args in (
        {"action": "create", "name": "tainted-skill", "content": skill_doc.replace("clean-skill", "tainted-skill")},
        {
            "action": "patch",
            "name": "clean-skill",
            "old_string": "Step 1: Inspect local state.",
            "new_string": "Step 1: Overwritten.",
        },
        {"action": "edit", "name": "clean-skill", "content": skill_doc + "\nExtra\n"},
        {
            "action": "write_file",
            "name": "clean-skill",
            "file_path": "references/note.md",
            "file_content": "tainted reference",
        },
        {
            "action": "remove_file",
            "name": "clean-skill",
            "file_path": "references/note.md",
        },
        {"action": "delete", "name": "clean-skill", "absorbed_into": ""},
    ):
        assert json.loads(handle_function_call("skill_manage", skill_args)) == TAINT_REFUSED
        assert json.loads(skill_manage(**skill_args)) == TAINT_REFUSED

    assert (
        json.loads(
            handle_function_call(
                "write_file",
                {"path": str(notes_file), "content": "overwritten\n"},
                task_id="persist-control",
            )
        )
        == TAINT_REFUSED
    )
    assert (
        json.loads(
            write_file_tool(
                path=str(notes_file), content="overwritten\n", task_id="persist-control"
            )
        )
        == TAINT_REFUSED
    )
    assert (
        json.loads(
            handle_function_call(
                "patch",
                {
                    "mode": "replace",
                    "path": str(notes_file),
                    "old_string": "gamma",
                    "new_string": "delta",
                },
                task_id="persist-control",
            )
        )
        == TAINT_REFUSED
    )
    v4a_patch = (
        f"*** Begin Patch\n*** Update File: {notes_file}\n@@\n-alpha\n+omega\n*** End Patch"
    )
    assert (
        json.loads(
            handle_function_call(
                "patch",
                {"mode": "patch", "patch": v4a_patch},
                task_id="persist-control",
            )
        )
        == TAINT_REFUSED
    )
    assert (
        json.loads(
            patch_tool(mode="patch", patch=v4a_patch, task_id="persist-control")
        )
        == TAINT_REFUSED
    )

    # 4. Agent-intercepted sequential and concurrent (invoke_tool) paths + external MemoryManager tools.
    ext_writes = []

    class _FakeExternalMemoryProvider(MemoryProvider):
        @property
        def name(self) -> str:
            return "fake_ext"

        def is_available(self) -> bool:
            return True

        def initialize(self, session_id: str, **kwargs) -> None:
            pass

        def get_tool_schemas(self):
            return [
                {
                    "name": "ext_memory_retain",
                    "description": "External memory write probe",
                    "parameters": {"type": "object", "properties": {}},
                }
            ]

        def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
            ext_writes.append((tool_name, args))
            return json.dumps({"success": True})

        def on_memory_write(self, action: str, target: str, content: str, **kwargs) -> None:
            ext_writes.append(("mirror", action, target, content))

    mm = MemoryManager()
    mm.add_provider(_FakeExternalMemoryProvider())

    class _DummyGuardrails:
        def before_call(self, name, args):
            return SimpleNamespace(allows_execution=True)

        def after_call(self, name, args, result, *, failed=False):
            return SimpleNamespace(action="allow", should_halt=False)

    dummy_agent = SimpleNamespace(
        session_id="sess-taint-agent",
        _current_turn_id="turn-1",
        _current_api_request_id="req-1",
        _memory_store=store,
        _memory_manager=mm,
        _context_engine_tool_names=set(),
        _turns_since_memory=5,
        _iters_since_skill=5,
        _interrupt_requested=False,
        quiet_mode=True,
        verbose_logging=False,
        tool_progress_mode="off",
        tool_progress_callback=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        _checkpoint_mgr=SimpleNamespace(enabled=False),
        _tool_guardrails=_DummyGuardrails(),
        _subdirectory_hints=SimpleNamespace(check_tool_call=lambda n, a: ""),
        _should_emit_quiet_tool_messages=lambda: False,
        _should_start_quiet_spinner=lambda: False,
        _touch_activity=lambda desc: None,
        _record_file_mutation_result=lambda *a, **k: None,
        _append_guardrail_observation=lambda n, a, r, failed=False: r,
        _tool_result_content_for_active_model=lambda n, r: r,
        _flush_messages_to_session_db=lambda msgs: None,
        _apply_pending_steer_to_tool_results=lambda msgs, n: None,
        _build_memory_write_metadata=lambda **k: {},
        valid_tool_names={"memory", "ext_memory_retain", "skill_manage", "write_file", "patch"},
        enabled_toolsets=["memory", "skills", "file"],
        disabled_toolsets=None,
        tool_delay=0,
    )

    assert (
        json.loads(
            invoke_tool(
                dummy_agent,
                "memory",
                {"action": "add", "target": "memory", "content": "concurrent tainted"},
                "persist-control",
            )
        )
        == TAINT_REFUSED
    )
    assert (
        json.loads(
            invoke_tool(
                dummy_agent,
                "ext_memory_retain",
                {"content": "concurrent ext tainted"},
                "persist-control",
            )
        )
        == TAINT_REFUSED
    )

    seq_messages = []
    seq_assistant = SimpleNamespace(
        tool_calls=[
            _tc("memory", {"action": "add", "target": "memory", "content": "seq tainted"}),
            _tc("ext_memory_retain", {"content": "seq ext tainted"}),
        ]
    )
    execute_tool_calls_sequential(
        dummy_agent, seq_assistant, seq_messages, "persist-control", api_call_count=1
    )
    assert [json.loads(m["content"]) for m in seq_messages] == [
        TAINT_REFUSED,
        TAINT_REFUSED,
    ]
    assert ext_writes == []
    assert store.memory_entries == ["clean memory entry"]
    assert notes_file.read_text(encoding="utf-8") == "alpha\ngamma\n"
    assert not (hermes_home / "skills" / "tainted-skill").exists()

    # 5. Legitimate read-only controls remain usable in a mid-turn tainted run.
    read_file_res = json.loads(
        handle_function_call(
            "read_file", {"path": str(notes_file)}, task_id="persist-control"
        )
    )
    assert "error" not in read_file_res and "gamma" in json.dumps(read_file_res)

    search_res = json.loads(
        handle_function_call(
            "search_files",
            {"pattern": "gamma", "path": str(tmp_path)},
            task_id="persist-control",
        )
    )
    assert "error" not in search_res

    skills_list_res = json.loads(handle_function_call("skills_list", {}))
    assert "error" not in skills_list_res

    skill_view_res = json.loads(
        handle_function_call("skill_view", {"name": "clean-skill"})
    )
    assert skill_view_res.get("success") is True


def test_tainted_turn_suppresses_background_review_and_external_memory_sync():
    from agent.turn_finalizer import finalize_turn

    spawned_reviews = []
    synced_turns = []

    def _make_agent():
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

    # 1. Clean untainted control: background review and external memory sync both run.
    clean_bound = email_tool.bind_email_capability("", tainted=False)
    try:
        clean_agent = _make_agent()
        clean_res = finalize_turn(
            clean_agent,
            final_response="Clean reply",
            api_call_count=1,
            interrupted=False,
            failed=False,
            messages=[
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Clean reply"},
            ],
            conversation_history=[],
            effective_task_id="task-clean",
            turn_id="turn-clean",
            user_message="Hello",
            original_user_message="Hello",
            _should_review_memory=True,
            _turn_exit_reason="text_response(finish_reason=stop)",
        )
        assert clean_res.get("atlas_email_mailbox_tainted") is False
        assert len(spawned_reviews) == 1
        assert spawned_reviews[0]["review_memory"] is True
        assert spawned_reviews[0]["review_skills"] is True
        assert len(synced_turns) == 1
    finally:
        email_tool.clear_email_capability(clean_bound)

    spawned_reviews.clear()
    synced_turns.clear()

    # 2. Tainted run: background review and external memory sync are suppressed, and result reports taint.
    tainted_bound = email_tool.bind_email_capability(EMAIL_TOKEN, tainted=True)
    try:
        tainted_agent = _make_agent()
        tainted_res = finalize_turn(
            tainted_agent,
            final_response="Tainted reply",
            api_call_count=1,
            interrupted=False,
            failed=False,
            messages=[
                {"role": "user", "content": "Read my email"},
                {"role": "assistant", "content": "Tainted reply"},
            ],
            conversation_history=[],
            effective_task_id="task-tainted",
            turn_id="turn-tainted",
            user_message="Read my email",
            original_user_message="Read my email",
            _should_review_memory=True,
            _turn_exit_reason="text_response(finish_reason=stop)",
        )
        assert tainted_res.get("atlas_email_mailbox_tainted") is True
        assert spawned_reviews == []
        assert synced_turns == []
    finally:
        email_tool.clear_email_capability(tainted_bound)


def test_session_search_taints_batch_and_run_across_all_dispatch_paths(
    tmp_path, web_probe
):
    from agent.agent_runtime_helpers import invoke_tool
    from hermes_state import SessionDB
    from tools.session_search_tool import session_search

    db = SessionDB(tmp_path / "sessions.db")
    try:
        past_sid = db.create_session("past-mailbox-session", "api_server")
        db.append_message(
            past_sid,
            role="user",
            content="Please summarize the neighbor parcel boundary email thread",
        )
        db.append_message(
            past_sid,
            role="assistant",
            content="The email thread discusses parcel 42 boundary survey details.",
        )

        # 1. Same-batch pre-scan on an ordinary NON-EMAIL turn (empty capability token):
        #    both direct session_search and Tool Search bridge-wrapped session_search taint the batch
        #    before an earlier egress tool in the same batch can run.
        for search_call in (
            _tc("session_search", {"query": "parcel"}),
            _tc("session_search", {}),
            _tc(
                "tool_call",
                {"name": " session_search ", "arguments": {"query": "parcel"}},
            ),
        ):
            bound = email_tool.bind_email_capability("", tainted=False)
            try:
                assert email_tool.mailbox_read_this_run() is False
                assert (
                    email_tool.taint_batch_if_it_reads_mailbox(
                        [_tc("zz_web_probe", {}), search_call]
                    )
                    is True
                )
                assert email_tool.mailbox_read_this_run() is True
                assert _run_tool("zz_web_probe") == TAINT_REFUSED
                assert web_probe == []
            finally:
                email_tool.clear_email_capability(bound)

        # 2. Executing session_search directly, via registry.dispatch, or via agent invoke_tool
        #    in discovery, read, and browse shapes taints the run before returning transcript content.
        for call_fn in (
            lambda: json.loads(
                session_search(query="parcel", db=db, current_session_id="cur-sess")
            ),
            lambda: json.loads(
                session_search(session_id=past_sid, db=db, current_session_id="cur-sess")
            ),
            lambda: json.loads(
                session_search(db=db, current_session_id="cur-sess")
            ),
            lambda: json.loads(
                registry.dispatch(
                    "session_search",
                    {"query": "parcel", "db": db, "current_session_id": "cur-sess"},
                )
            ),
            lambda: json.loads(
                invoke_tool(
                    SimpleNamespace(
                        session_id="cur-sess",
                        _current_turn_id="t1",
                        _current_api_request_id="r1",
                        _get_session_db_for_recall=lambda: db,
                    ),
                    "session_search",
                    {"query": "parcel"},
                    "task-recall",
                )
            ),
        ):
            bound = email_tool.bind_email_capability("", tainted=False)
            try:
                assert email_tool.mailbox_read_this_run() is False
                search_res = call_fn()
                assert search_res.get("success") is True
                assert email_tool.mailbox_read_this_run() is True
                # Outbound egress and persistence writes are immediately refused in the same run.
                assert _run_tool("zz_web_probe") == TAINT_REFUSED
                assert (
                    json.loads(
                        handle_function_call(
                            "write_file",
                            {"path": str(tmp_path / "out.txt"), "content": "x"},
                            task_id="task-recall",
                        )
                    )
                    == TAINT_REFUSED
                )
            finally:
                email_tool.clear_email_capability(bound)

        # 3. Reset control: after clearing the tainted binding, a fresh untainted run can use web tools.
        fresh_bound = email_tool.bind_email_capability("", tainted=False)
        try:
            assert email_tool.mailbox_read_this_run() is False
            assert _run_tool("zz_web_probe") == {"ok": "zz_web_probe"}
            assert web_probe == ["zz_web_probe"]
        finally:
            email_tool.clear_email_capability(fresh_bound)
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/chat", "/chat/stream"])
@pytest.mark.parametrize("taint_during_turn", [False, True])
async def test_session_chat_and_stream_return_observed_mailbox_taint_in_turn_result(
    tmp_path, endpoint, taint_during_turn
):
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "sessions.db")
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._session_db = db
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    app.router.add_post(
        "/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream
    )
    sid = db.create_session("session-taint-result", "api_server")

    class _FakeAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id") or sid
            self.model = "test/model"
            self.provider = "openrouter"
            self.session_prompt_tokens = 1
            self.session_completion_tokens = 1
            self.session_total_tokens = 2

        def run_conversation(self, **kwargs):
            if taint_during_turn:
                email_tool._mark_tainted()
            # Intentionally omit "atlas_email_mailbox_tainted" so the test verifies
            # that APIServerAdapter._run_agent computes it from mailbox_read_this_run().
            return {
                "final_response": "Turn complete",
                "messages": [
                    {"role": "user", "content": kwargs.get("user_message", "")},
                    {"role": "assistant", "content": "Turn complete"},
                ],
                "session_id": self.session_id,
            }

    try:
        with patch.object(
            adapter, "_create_agent", side_effect=lambda **kw: _FakeAgent(**kw)
        ), patch("gateway.run._load_gateway_config", return_value={}), patch(
            "hermes_cli.tools_config._get_platform_tools",
            return_value={"web", "todo", "atlas_email"},
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    f"/api/sessions/{sid}{endpoint}",
                    json={"message": "hello", "atlas_email_capability": EMAIL_TOKEN},
                )
                assert resp.status == 200
                raw_text = await resp.text()
                if endpoint == "/chat":
                    payload = json.loads(raw_text)
                    assert (
                        payload.get("atlas_email_mailbox_tainted") is taint_during_turn
                    )
                else:
                    completed_events = [
                        json.loads(line[len("data: ") :])
                        for line in raw_text.splitlines()
                        if line.startswith("data: ")
                        and '"run.completed"' in line
                    ]
                    if not completed_events:
                        # Parse SSE event/data pairs when event name is on the preceding `event:` line
                        lines = raw_text.splitlines()
                        for idx, line in enumerate(lines):
                            if line.strip() == "event: run.completed" and idx + 1 < len(lines):
                                data_line = lines[idx + 1]
                                if data_line.startswith("data: "):
                                    completed_events.append(json.loads(data_line[len("data: ") :]))
                    assert len(completed_events) == 1
                    assert (
                        completed_events[0].get("atlas_email_mailbox_tainted")
                        is taint_during_turn
                    )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_replayed_session_history_and_responses_chaining_propagate_mailbox_taint_while_clean_sessions_keep_tools(
    tmp_path, web_probe
):
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "sessions.db")
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "test-api-key"})
    )
    adapter._session_db = db
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    app.router.add_post(
        "/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream
    )
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    app.router.add_post("/v1/responses", adapter._handle_responses)

    # 1. Tainted session in SessionDB with a saved atlas_email read_message tool call + tool result.
    tainted_sid = db.create_session("session-replayed-tainted", "api_server")
    db.append_message(tainted_sid, role="user", content="Read message m1")
    db.append_message(
        tainted_sid,
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "call_read_m1",
                "type": "function",
                "function": {
                    "name": "atlas_email",
                    "arguments": json.dumps(
                        {"operation": "read_message", "arguments": {"message_id": "m1"}}
                    ),
                },
            }
        ],
    )
    db.append_message(
        tainted_sid,
        role="tool",
        tool_name="atlas_email",
        tool_call_id="call_read_m1",
        content=json.dumps(
            {
                "untrusted_email_content": {
                    "message": {"id": "m1", "body": "Untrusted mailbox text"}
                }
            }
        ),
    )
    db.append_message(
        tainted_sid, role="assistant", content="Here is the message summary."
    )

    # 2. Clean session in SessionDB with normal prose and an untainted atlas_email propose call.
    clean_sid = db.create_session("session-replayed-clean", "api_server")
    db.append_message(clean_sid, role="user", content="Draft an email to Alice")
    db.append_message(
        clean_sid,
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "call_propose_1",
                "type": "function",
                "function": {
                    "name": "atlas_email",
                    "arguments": json.dumps(
                        {"operation": "propose", "arguments": PROPOSE}
                    ),
                },
            }
        ],
    )
    db.append_message(
        clean_sid,
        role="tool",
        tool_name="atlas_email",
        tool_call_id="call_propose_1",
        content=json.dumps(APPROVAL),
    )
    db.append_message(
        clean_sid, role="assistant", content="The draft is waiting for approval."
    )

    observed_turns = []
    taint_next_turn = [False]
    configured_toolsets = {
        "web",
        "file",
        "memory",
        "skills",
        "session_search",
        "todo",
    }

    class _RecordingAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id")
            self.enabled_toolsets = list(kwargs.get("enabled_toolsets") or [])
            self.model = "test/model"
            self.provider = "openrouter"
            self.session_prompt_tokens = 1
            self.session_completion_tokens = 1
            self.session_total_tokens = 2

        def run_conversation(self, **kwargs):
            if taint_next_turn[0]:
                email_tool._mark_tainted()
            history = list(kwargs.get("conversation_history") or [])
            user_msg = kwargs.get("user_message", "")
            tainted_now = email_tool.mailbox_read_this_run()
            probe_out = _run_tool("zz_web_probe")
            web_probe.clear()
            observed_turns.append(
                {
                    "session_id": self.session_id,
                    "enabled_toolsets": sorted(self.enabled_toolsets),
                    "tainted_in_run": tainted_now,
                    "probe_out": probe_out,
                    "history_len": len(history),
                }
            )
            messages = list(history)
            messages.append({"role": "user", "content": user_msg})
            if taint_next_turn[0]:
                messages.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_ss_1",
                                "type": "function",
                                "function": {
                                    "name": "session_search",
                                    "arguments": json.dumps({"query": "m1"}),
                                },
                            }
                        ],
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "name": "session_search",
                        "tool_name": "session_search",
                        "tool_call_id": "call_ss_1",
                        "content": json.dumps({"success": True, "results": []}),
                    }
                )
            messages.append({"role": "assistant", "content": "Turn complete"})
            return {
                "final_response": "Turn complete",
                "messages": messages,
                "session_id": self.session_id,
            }

    try:
        with patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value={
                "api_key": "synthetic",
                "base_url": None,
                "provider": None,
                "api_mode": None,
                "command": None,
                "args": [],
            },
        ), patch(
            "gateway.run._resolve_gateway_model", return_value="test/model"
        ), patch(
            "gateway.run._load_gateway_config", return_value={}
        ), patch(
            "hermes_cli.tools_config._get_platform_tools",
            return_value=configured_toolsets,
        ), patch(
            "gateway.run._current_max_iterations", return_value=90
        ), patch(
            "run_agent.AIAgent", side_effect=lambda **kw: _RecordingAgent(**kw)
        ):
            async with TestClient(TestServer(app)) as client:
                auth_headers = {"Authorization": "Bearer test-api-key"}
                # 1. Tainted stored history replayed on /api/sessions/{id}/chat without taint flag in request.
                r_chat = await client.post(
                    f"/api/sessions/{tainted_sid}/chat",
                    headers=auth_headers,
                    json={"message": "Follow up", "atlas_email_capability": EMAIL_TOKEN},
                )
                assert r_chat.status == 200
                assert (await r_chat.json()).get("atlas_email_mailbox_tainted") is True
                assert observed_turns[-1]["enabled_toolsets"] == ["atlas_email", "todo"]
                assert observed_turns[-1]["tainted_in_run"] is True
                assert observed_turns[-1]["probe_out"] == TAINT_REFUSED

                # 2. Tainted stored history replayed on /api/sessions/{id}/chat/stream without taint flag.
                r_stream = await client.post(
                    f"/api/sessions/{tainted_sid}/chat/stream",
                    headers=auth_headers,
                    json={"message": "Follow up stream", "atlas_email_capability": EMAIL_TOKEN},
                )
                assert r_stream.status == 200
                _ = await r_stream.text()
                assert observed_turns[-1]["enabled_toolsets"] == ["atlas_email", "todo"]
                assert observed_turns[-1]["tainted_in_run"] is True
                assert observed_turns[-1]["probe_out"] == TAINT_REFUSED

                # 3. Tainted stored history replayed on /v1/chat/completions via X-Hermes-Session-Id.
                r_cc = await client.post(
                    "/v1/chat/completions",
                    headers={
                        **auth_headers,
                        "X-Hermes-Session-Id": tainted_sid,
                    },
                    json={"messages": [{"role": "user", "content": "Continue on /v1/chat/completions"}]},
                )
                assert r_cc.status == 200
                assert observed_turns[-1]["enabled_toolsets"] == ["todo"]
                assert observed_turns[-1]["tainted_in_run"] is True
                assert observed_turns[-1]["probe_out"] == TAINT_REFUSED

                # 4. Clean session controls (including prior atlas_email propose) keep all tools enabled.
                r_clean_chat = await client.post(
                    f"/api/sessions/{clean_sid}/chat",
                    headers=auth_headers,
                    json={"message": "Clean chat", "atlas_email_capability": EMAIL_TOKEN},
                )
                assert r_clean_chat.status == 200
                assert (await r_clean_chat.json()).get("atlas_email_mailbox_tainted") is False
                assert {
                    "atlas_email",
                    "web",
                    "file",
                    "memory",
                    "skills",
                    "session_search",
                    "todo",
                } <= set(observed_turns[-1]["enabled_toolsets"])
                assert observed_turns[-1]["tainted_in_run"] is False
                assert observed_turns[-1]["probe_out"] == {"ok": "zz_web_probe"}

                r_clean_stream = await client.post(
                    f"/api/sessions/{clean_sid}/chat/stream",
                    headers=auth_headers,
                    json={"message": "Clean stream", "atlas_email_capability": EMAIL_TOKEN},
                )
                assert r_clean_stream.status == 200
                _ = await r_clean_stream.text()
                assert {
                    "atlas_email",
                    "web",
                    "file",
                    "memory",
                    "skills",
                    "session_search",
                    "todo",
                } <= set(observed_turns[-1]["enabled_toolsets"])
                assert observed_turns[-1]["tainted_in_run"] is False
                assert observed_turns[-1]["probe_out"] == {"ok": "zz_web_probe"}

                r_clean_cc = await client.post(
                    "/v1/chat/completions",
                    headers={
                        **auth_headers,
                        "X-Hermes-Session-Id": clean_sid,
                    },
                    json={"messages": [{"role": "user", "content": "Clean chat completions"}]},
                )
                assert r_clean_cc.status == 200
                assert {
                    "web",
                    "file",
                    "memory",
                    "skills",
                    "session_search",
                    "todo",
                } <= set(observed_turns[-1]["enabled_toolsets"])
                assert observed_turns[-1]["tainted_in_run"] is False
                assert observed_turns[-1]["probe_out"] == {"ok": "zz_web_probe"}

                # 5. /v1/responses chaining via previous_response_id and conversation:
                #    tainted turn 1 taints chained turn 2 on both paths; clean chain keeps tools.
                taint_next_turn[0] = True
                r_resp1 = await client.post(
                    "/v1/responses",
                    headers=auth_headers,
                    json={"input": "Turn 1 reads transcript", "conversation": "conv-tainted"},
                )
                assert r_resp1.status == 200
                resp1_id = (await r_resp1.json())["id"]
                taint_next_turn[0] = False

                r_resp2_prev = await client.post(
                    "/v1/responses",
                    headers=auth_headers,
                    json={
                        "input": "Turn 2 via previous_response_id",
                        "previous_response_id": resp1_id,
                    },
                )
                assert r_resp2_prev.status == 200
                assert observed_turns[-1]["enabled_toolsets"] == ["todo"]
                assert observed_turns[-1]["tainted_in_run"] is True
                assert observed_turns[-1]["probe_out"] == TAINT_REFUSED

                r_resp2_conv = await client.post(
                    "/v1/responses",
                    headers=auth_headers,
                    json={
                        "input": "Turn 2 via conversation",
                        "conversation": "conv-tainted",
                    },
                )
                assert r_resp2_conv.status == 200
                assert observed_turns[-1]["enabled_toolsets"] == ["todo"]
                assert observed_turns[-1]["tainted_in_run"] is True
                assert observed_turns[-1]["probe_out"] == TAINT_REFUSED

                # Clean /v1/responses chain control
                r_clean_resp1 = await client.post(
                    "/v1/responses",
                    headers=auth_headers,
                    json={"input": "Clean responses turn 1", "conversation": "conv-clean"},
                )
                assert r_clean_resp1.status == 200
                clean_resp1_id = (await r_clean_resp1.json())["id"]

                r_clean_resp2_prev = await client.post(
                    "/v1/responses",
                    headers=auth_headers,
                    json={
                        "input": "Clean responses turn 2 via previous_response_id",
                        "previous_response_id": clean_resp1_id,
                    },
                )
                assert r_clean_resp2_prev.status == 200
                assert {
                    "web",
                    "file",
                    "memory",
                    "skills",
                    "session_search",
                    "todo",
                } <= set(observed_turns[-1]["enabled_toolsets"])
                assert observed_turns[-1]["tainted_in_run"] is False
                assert observed_turns[-1]["probe_out"] == {"ok": "zz_web_probe"}

                r_clean_resp2_conv = await client.post(
                    "/v1/responses",
                    headers=auth_headers,
                    json={
                        "input": "Clean responses turn 2 via conversation",
                        "conversation": "conv-clean",
                    },
                )
                assert r_clean_resp2_conv.status == 200
                assert {
                    "web",
                    "file",
                    "memory",
                    "skills",
                    "session_search",
                    "todo",
                } <= set(observed_turns[-1]["enabled_toolsets"])
                assert observed_turns[-1]["tainted_in_run"] is False
                assert observed_turns[-1]["probe_out"] == {"ok": "zz_web_probe"}
    finally:
        db.close()
