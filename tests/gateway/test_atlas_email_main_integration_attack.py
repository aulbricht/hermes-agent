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
        "todo",
        "file",
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
        # Control: untainted run retains configured non-atlas toolsets.
        adapter._create_agent(atlas_email_authorized=True)
        untainted = set(agent.call_args.kwargs["enabled_toolsets"])
        assert {"atlas_email", "web", "mcp-github", "todo", "file"} <= untainted

        # Tainted run strips every egress, composite, and MCP toolset, keeping only local/email.
        adapter._create_agent(
            atlas_email_authorized=True, atlas_email_mailbox_tainted=True
        )
        tainted = sorted(agent.call_args.kwargs["enabled_toolsets"])
        assert tainted == ["atlas_email", "file", "todo"]

        # Pinned ed377 control (not d744): tainted import turn keeps atlas_import.
        adapter._create_agent(
            atlas_import_authorized=True, atlas_email_mailbox_tainted=True
        )
        tainted_import = sorted(agent.call_args.kwargs["enabled_toolsets"])
        assert tainted_import == ["atlas_import", "file", "todo"]


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
