"""SNO-4168 integration-correction attack tests for SNO-3974 (ebe1c516 + d74466816b).

Covers combined email/import admission on both session chat endpoints, mailbox taint
surviving executor/session/replayed/ancestor history and denying import invocation across
all dispatch and handler boundaries even without a fresh email capability, and unchanged
outreach save-only isolation using inert synthetic clients and offline loopback transports.
"""

import copy
import json
import socket
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent.tool_executor import (
    execute_tool_calls_concurrent,
    execute_tool_calls_sequential,
)
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB
from model_tools import get_tool_definitions, handle_function_call
from tools import (
    atlas_email_tool as email_tool,
    atlas_import_tool as import_tool,
    atlas_outreach_tool as outreach_tool,
)
from tools.registry import registry

EMAIL_TOKEN = "atmail_" + "a" * 32
IMP_TOKEN = "atimp_" + "c" * 32
OWN_TOKEN = "atown_" + "b" * 32
QRY_TOKEN = "atqry_" + "d" * 32
OUT_TOKEN = "atout_" + "e" * 48

OUTREACH_PROTOCOL = "atlas-outreach-drafts-v1"
OUTREACH_UNAVAILABLE = {"error": "Outreach operation unavailable"}
TAINT_REFUSED = {"error": "Web tools are switched off after reading email in this turn"}

# Source-derived email request/response shapes (tests/gateway/test_atlas_email_tool.py:21-24, 348, 479-480).
EMAIL_PROPOSE = {"to": ["a@example.com"], "subject": "Hi", "body": "Hello"}
EMAIL_APPROVAL = {
    "status": "waiting_for_approval",
    "approval_id": "ap_1",
    "outside_recipients": ["a@example.com"],
}
EMAIL_READ = {"operation": "read_message", "arguments": {"message_id": "m1"}}
EMAIL_LIST = {"operation": "list_messages", "arguments": {}}

# Source-derived import request/response shapes (tests/gateway/test_atlas_import_tool.py:25-26, 83-88, 115-117, 243-244).
IMPORT_LOOKUP = {"operation": "lookup", "arguments": {"property_query": "Wheatland"}}
IMPORT_EVIDENCE = {"operation": "evidence", "arguments": {"offset": 0, "limit": 50}}
IMPORT_CATALOG = {"operation": "catalog", "arguments": {}}
IMPORT_CHANGE_SET = {
    "operation": "change_set",
    "property_id": "PRO-" + "a" * 36,
    "change_set": [{"id": "a"}],
}
IMPORT_OK = {"accepted": True}

# Source-derived synthetic outreach payload (tests/gateway/test_atlas_outreach_attack.py:28-38).
OUTREACH_PAYLOAD = {
    "subject_property_id": "PRO-00000000-0000-0000-0000-000000000001",
    "owner_id": "1204AAAAAAAAAAAAAAAAAA",
    "email_subject": "Synthetic outreach",
    "email_body": "Synthetic draft for review.",
    "call_script": "Synthetic script for review.",
    "facts": [
        {
            "source_ref": "lightbox_contacts.synthetic.json",
            "path": "/data/0/contacts/1/firstName",
            "value": "ALEX",
        }
    ],
}
OUTREACH_SUCCESS = {
    "protocol": OUTREACH_PROTOCOL,
    "ok": True,
    "result": {"id": "draft-offline", "run_id": "run-offline"},
}


def _tc(name, arguments, call_id="tc_1"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments=arguments if isinstance(arguments, str) else json.dumps(arguments),
        ),
    )


def _tool_names(scopes):
    return {
        item["function"]["name"]
        for item in get_tool_definitions(
            enabled_toolsets=scopes, quiet_mode=True, skip_tool_search_assembly=True
        )
    }


@pytest.fixture(autouse=True)
def no_external_network_or_process(monkeypatch):
    attempts = []
    real_connect = socket.socket.connect

    def connect(sock, address):
        if (
            isinstance(address, tuple)
            and address[0] in {"127.0.0.1", "::1"}
            and address[1] not in {8243, 8093}
        ):
            return real_connect(sock, address)
        attempts.append(("socket", address))
        raise AssertionError("Unexpected non-test-server network")

    def process(*args, **kwargs):
        attempts.append(("process", args))
        raise AssertionError("Unexpected subprocess")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(subprocess, "Popen", process)
    yield
    assert attempts == []


@pytest.fixture
def callbacks(monkeypatch):
    observed = {"email": [], "import": [], "outreach": []}
    responders = {
        "email": lambda req: httpx.Response(
            200,
            json=EMAIL_APPROVAL
            if json.loads(req.content).get("operation") == "propose"
            else (
                {"messages": [{"id": "m1"}]}
                if json.loads(req.content).get("operation") == "list_messages"
                else {"message": {"id": "m1", "body": "Untrusted mailbox text"}}
            ),
        ),
        "import": lambda req: httpx.Response(200, json=IMPORT_OK),
        "outreach": lambda req: httpx.Response(200, json=OUTREACH_SUCCESS),
    }
    real_client = httpx.Client

    def _install(target_mod, kind, expected_url, expected_read_timeout):
        def receive(request):
            assert request.method == "POST"
            assert str(request.url) == expected_url
            observed[kind].append(request)
            return responders[kind](request)

        def factory(**kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False
            assert kwargs["timeout"].connect == 2
            assert kwargs["timeout"].read == expected_read_timeout
            return real_client(transport=httpx.MockTransport(receive), **kwargs)

        monkeypatch.setattr(
            target_mod,
            "httpx",
            SimpleNamespace(
                Client=factory, Timeout=httpx.Timeout, HTTPError=httpx.HTTPError
            ),
        )

    _install(
        email_tool,
        "email",
        "http://127.0.0.1:8243/api/v1/internal/email-tool",
        60,
    )
    _install(
        import_tool,
        "import",
        "http://127.0.0.1:8243/api/v1/internal/import-tool",
        90,
    )
    _install(
        outreach_tool,
        "outreach",
        "http://127.0.0.1:8243/api/v1/internal/outreach-draft-tool",
        30,
    )
    return observed, responders


@pytest.fixture
def adapter(tmp_path):
    db = SessionDB(tmp_path / "sessions.db")
    value = APIServerAdapter(PlatformConfig(enabled=True))
    value._session_db = db
    yield value
    db.close()


def _app_for(value):
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/chat", value._handle_session_chat)
    app.router.add_post(
        "/api/sessions/{session_id}/chat/stream", value._handle_session_chat_stream
    )
    return app


def _extract_stream_completed(raw_text: str) -> dict:
    lines = raw_text.splitlines()
    for idx, line in enumerate(lines):
        if line.strip() == "event: run.completed" and idx + 1 < len(lines):
            data_line = lines[idx + 1]
            if data_line.startswith("data: "):
                return json.loads(data_line[len("data: ") :])
    for line in lines:
        if line.startswith("data: ") and '"run.completed"' in line:
            return json.loads(line[len("data: ") :])
    raise AssertionError(f"Missing run.completed SSE event in stream:\n{raw_text}")


@pytest.mark.asyncio
async def test_control_ordinary_single_capability_and_outreach_isolation_baseline(
    adapter, callbacks, monkeypatch, caplog
):
    """Passing baseline control: single-capability import/email admission and save-only outreach isolation."""
    observed, _ = callbacks
    sid = adapter._session_db.create_session("control-baseline", "api_server")

    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"platform_toolsets": {"api_server": ["web", "todo"]}},
    )
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda *_a, **_k: {
            "web",
            "todo",
            "atlas_import",
            "atlas_ownership",
            "atlas_query",
            "atlas_email",
            "atlas_outreach",
        },
    )

    # 1. Mixed outreach authority (including outreach + email + import together) and email + ownership/query
    #    are rejected with HTTP 400 on both /chat and /chat/stream without echoing any token.
    for endpoint in ("/chat", "/chat/stream"):
        for invalid_mix in (
            {
                "atlas_outreach_capability": OUT_TOKEN,
                "atlas_email_capability": EMAIL_TOKEN,
                "atlas_import_capability": IMP_TOKEN,
            },
            {
                "atlas_outreach_capability": OUT_TOKEN,
                "atlas_email_capability": EMAIL_TOKEN,
            },
            {
                "atlas_outreach_capability": OUT_TOKEN,
                "atlas_import_capability": IMP_TOKEN,
            },
            {
                "atlas_email_capability": EMAIL_TOKEN,
                "atlas_import_capability": IMP_TOKEN,
                "atlas_ownership_capability": OWN_TOKEN,
            },
            {
                "atlas_email_capability": EMAIL_TOKEN,
                "atlas_import_capability": IMP_TOKEN,
                "atlas_query_capability": QRY_TOKEN,
            },
        ):
            with patch.object(adapter, "_run_agent") as run_mock:
                async with TestClient(TestServer(_app_for(adapter))) as client:
                    resp = await client.post(
                        f"/api/sessions/{sid}{endpoint}",
                        json={"message": "mixed check", **invalid_mix},
                    )
                    text = await resp.text()
                assert resp.status == 400
                run_mock.assert_not_called()
            for secret in (EMAIL_TOKEN, IMP_TOKEN, OWN_TOKEN, QRY_TOKEN, OUT_TOKEN):
                assert secret not in text + caplog.text

    # 2. Single-capability import turn and single-capability email turn are admitted on both endpoints.
    single_seen = []

    class _SingleProbeAgent:
        def __init__(self, **kwargs):
            self.enabled_toolsets = sorted(kwargs.get("enabled_toolsets") or [])

        def run_conversation(self, **kwargs):
            single_seen.append(
                {
                    "enabled_toolsets": self.enabled_toolsets,
                    "visible": _tool_names(self.enabled_toolsets),
                }
            )
            return {"final_response": "ok", "messages": []}

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
        "gateway.run._current_max_iterations", return_value=90
    ), patch(
        "run_agent.AIAgent", side_effect=lambda **kw: _SingleProbeAgent(**kw)
    ):
        async with TestClient(TestServer(_app_for(adapter))) as client:
            r_imp = await client.post(
                f"/api/sessions/{sid}/chat",
                json={"message": "import control", "atlas_import_capability": IMP_TOKEN},
            )
            assert r_imp.status == 200
            r_em = await client.post(
                f"/api/sessions/{sid}/chat/stream",
                json={"message": "email control", "atlas_email_capability": EMAIL_TOKEN},
            )
            assert r_em.status == 200
            _ = await r_em.text()

    assert len(single_seen) == 2
    assert {"atlas_import", "atlas_import_proposal"} <= single_seen[0]["visible"]
    assert "atlas_email" not in single_seen[0]["visible"]
    assert "atlas_outreach_save" not in single_seen[0]["visible"]
    assert "atlas_email" in single_seen[1]["visible"]
    assert "atlas_import" not in single_seen[1]["visible"]
    assert "atlas_outreach_save" not in single_seen[1]["visible"]

    # 3. Save-only outreach isolation holds even when mailbox taint flag and broad toolset_scope are supplied.
    outreach_seen = []

    class _OutreachProbeAgent:
        def __init__(self, **kwargs):
            self.enabled_toolsets = list(kwargs.get("enabled_toolsets") or [])
            self.skip_memory = kwargs.get("skip_memory")

        def run_conversation(self, **kwargs):
            visible = _tool_names(self.enabled_toolsets)
            save_res = json.loads(
                handle_function_call(
                    "atlas_outreach_save",
                    copy.deepcopy(OUTREACH_PAYLOAD),
                    enabled_tools=["atlas_outreach_save"],
                    enabled_toolsets=self.enabled_toolsets,
                )
            )
            forbidden_results = {
                name: json.loads(
                    handle_function_call(
                        name,
                        args,
                        enabled_tools=[name],
                        enabled_toolsets=["atlas_import", "atlas_email", "web"],
                    )
                )
                for name, args in (
                    ("atlas_import", IMPORT_LOOKUP),
                    ("atlas_import_proposal", IMPORT_CHANGE_SET),
                    ("atlas_email", {"operation": "propose", "arguments": EMAIL_PROPOSE}),
                    ("web_search", {"query": "synthetic"}),
                    (
                        "tool_call",
                        {"name": "atlas_import", "arguments": IMPORT_LOOKUP},
                    ),
                )
            }
            outreach_seen.append(
                {
                    "enabled_toolsets": self.enabled_toolsets,
                    "skip_memory": self.skip_memory,
                    "visible": visible,
                    "save_res": save_res,
                    "forbidden_results": forbidden_results,
                }
            )
            return {"final_response": "saved", "messages": []}

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
        "gateway.run._current_max_iterations", return_value=90
    ), patch(
        "run_agent.AIAgent", side_effect=lambda **kw: _OutreachProbeAgent(**kw)
    ):
        async with TestClient(TestServer(_app_for(adapter))) as client:
            resp = await client.post(
                f"/api/sessions/{sid}/chat",
                json={
                    "message": "Save outreach draft",
                    "atlas_outreach_capability": OUT_TOKEN,
                    "atlas_email_mailbox_tainted": True,
                    "toolset_scope": ["web", "todo"],
                },
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["atlas_email_mailbox_tainted"] is True

    assert len(outreach_seen) == 1
    assert outreach_seen[0]["enabled_toolsets"] == ["atlas_outreach"]
    assert outreach_seen[0]["skip_memory"] is True
    assert outreach_seen[0]["visible"] == {"atlas_outreach_save"}
    assert outreach_seen[0]["save_res"] == OUTREACH_SUCCESS
    assert all(
        res == OUTREACH_UNAVAILABLE
        for res in outreach_seen[0]["forbidden_results"].values()
    )
    assert len(observed["outreach"]) == 1
    assert observed["outreach"][0].headers["Authorization"] == "Bearer " + OUT_TOKEN
    assert observed["import"] == [] and observed["email"] == []
    assert not outreach_tool.outreach_capability_available()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/chat", "/chat/stream"])
@pytest.mark.parametrize("scope", [None, ["web"]])
async def test_session_chat_and_stream_admit_combined_email_and_import_and_wire_both_tools(
    adapter, callbacks, monkeypatch, caplog, endpoint, scope
):
    """Defeating test 1 (Fails at ebe1c516): combined email+import turn is admitted on /chat and /chat/stream
    and wires both atlas_import (+ atlas_import_proposal) and atlas_email through _run_agent/_create_agent."""
    observed, _ = callbacks
    slug = endpoint.strip("/").replace("/", "-")
    sid = adapter._session_db.create_session(
        f"combined-email-import-{slug}", "api_server"
    )
    turn_observations = []

    class _CombinedTurnAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id") or sid
            self.enabled_toolsets = list(kwargs.get("enabled_toolsets") or [])
            self.skip_memory = kwargs.get("skip_memory")

        def run_conversation(self, **kwargs):
            visible = _tool_names(self.enabled_toolsets)
            lookup_res = json.loads(
                handle_function_call(
                    "atlas_import",
                    copy.deepcopy(IMPORT_LOOKUP),
                    enabled_toolsets=self.enabled_toolsets,
                )
            )
            proposal_res = json.loads(
                handle_function_call(
                    "atlas_import_proposal",
                    copy.deepcopy(IMPORT_CHANGE_SET),
                    enabled_toolsets=self.enabled_toolsets,
                )
            )
            email_res = json.loads(
                handle_function_call(
                    "atlas_email",
                    {"operation": "propose", "arguments": copy.deepcopy(EMAIL_PROPOSE)},
                    enabled_toolsets=self.enabled_toolsets,
                )
            )
            outreach_res = json.loads(
                handle_function_call(
                    "atlas_outreach_save",
                    copy.deepcopy(OUTREACH_PAYLOAD),
                    enabled_toolsets=self.enabled_toolsets,
                )
            )
            turn_observations.append(
                {
                    "enabled_toolsets": sorted(self.enabled_toolsets),
                    "visible": visible,
                    "lookup_res": lookup_res,
                    "proposal_res": proposal_res,
                    "email_res": email_res,
                    "outreach_res": outreach_res,
                    "tainted_in_run": email_tool.mailbox_read_this_run(),
                }
            )
            return {
                "final_response": "Combined import and email draft complete",
                "messages": [
                    {"role": "user", "content": kwargs.get("user_message", "")},
                    {
                        "role": "assistant",
                        "content": "Combined import and email draft complete",
                    },
                ],
                "session_id": self.session_id,
            }

    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs",
        lambda: {
            "api_key": "synthetic",
            "base_url": None,
            "provider": None,
            "api_mode": None,
            "command": None,
            "args": [],
        },
    )
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "test/model")
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda *_a, **_k: {
            "web",
            "todo",
            "atlas_import",
            "atlas_ownership",
            "atlas_query",
            "atlas_email",
            "atlas_outreach",
        },
    )
    monkeypatch.setattr("gateway.run._current_max_iterations", lambda: 90)
    monkeypatch.setattr(
        "run_agent.AIAgent", lambda **kw: _CombinedTurnAgent(**kw)
    )

    req_body = {
        "message": "Lookup property, submit change set, and draft email",
        "atlas_email_capability": EMAIL_TOKEN,
        "atlas_import_capability": IMP_TOKEN,
    }
    if scope is not None:
        req_body["toolset_scope"] = scope

    async with TestClient(TestServer(_app_for(adapter))) as client:
        resp = await client.post(f"/api/sessions/{sid}{endpoint}", json=req_body)
        raw_text = await resp.text()
        assert (
            resp.status == 200
        ), f"Expected 200 on {endpoint}, got {resp.status}: {raw_text}"

    if endpoint == "/chat":
        payload = json.loads(raw_text)
        assert payload["atlas_email_mailbox_tainted"] is False
    else:
        completed = _extract_stream_completed(raw_text)
        assert completed["atlas_email_mailbox_tainted"] is False

    assert len(turn_observations) == 1
    obs = turn_observations[0]
    expected_toolsets = (
        ["atlas_email", "atlas_import", "web"]
        if scope == ["web"]
        else ["atlas_email", "atlas_import", "todo", "web"]
    )
    assert obs["enabled_toolsets"] == expected_toolsets
    assert {"atlas_email", "atlas_import", "atlas_import_proposal"} <= obs["visible"]
    assert not (
        obs["visible"]
        & {
            "atlas_outreach_save",
            "atlas_ownership",
            "atlas_ownership_proposal",
            "atlas_query",
        }
    )
    assert obs["lookup_res"] == IMPORT_OK
    assert obs["proposal_res"] == IMPORT_OK
    assert obs["email_res"] == EMAIL_APPROVAL
    assert obs["outreach_res"] == OUTREACH_UNAVAILABLE
    assert obs["tainted_in_run"] is False

    assert len(observed["import"]) == 2
    assert all(
        r.headers["Authorization"] == "Bearer " + IMP_TOKEN for r in observed["import"]
    )
    assert [json.loads(r.content)["operation"] for r in observed["import"]] == [
        "lookup",
        "change_set",
    ]
    assert len(observed["email"]) == 1
    assert observed["email"][0].headers["Authorization"] == "Bearer " + EMAIL_TOKEN
    assert json.loads(observed["email"][0].content)["operation"] == "propose"
    assert observed["outreach"] == []
    assert EMAIL_TOKEN not in raw_text + caplog.text
    assert IMP_TOKEN not in raw_text + caplog.text
    assert not import_tool.import_capability_available()
    assert not email_tool.email_capability_available()


def test_mid_turn_and_same_batch_mailbox_taint_denies_import_tools_at_invocation_boundary(
    callbacks, tmp_path
):
    """Defeating test 2 (Fails at ebe1c516): mailbox taint blocks atlas_import in TAINT_ALLOWED_TOOLSETS /
    TAINT_BLOCKED_TOOLSETS and refuses atlas_import and atlas_import_proposal across same-batch pre-scan,
    mid-turn read, session_search without email capability, registry.dispatch, handle_function_call,
    Tool Search bridge, sequential/concurrent executors, and direct handler entry."""
    observed, responders = callbacks

    # 1. Static policy invariants: atlas_import must be excluded from allow-list and included in blocked set.
    assert "atlas_import" not in email_tool.TAINT_ALLOWED_TOOLSETS
    assert "atlas_import" in email_tool.TAINT_BLOCKED_TOOLSETS
    assert email_tool.taint_blocks_toolset("atlas_import") is True

    # 2. Untainted combined turn control: before any mailbox read, import read/proposal and email propose work.
    imp_bound = import_tool.bind_import_capability(IMP_TOKEN)
    email_bound = email_tool.bind_email_capability(EMAIL_TOKEN, tainted=False)
    try:
        assert json.loads(handle_function_call("atlas_import", IMPORT_EVIDENCE)) == IMPORT_OK
        assert json.loads(handle_function_call("atlas_import", IMPORT_CATALOG)) == IMPORT_OK
        assert json.loads(handle_function_call("atlas_import", IMPORT_LOOKUP)) == IMPORT_OK
        assert (
            json.loads(handle_function_call("atlas_import_proposal", IMPORT_CHANGE_SET))
            == IMPORT_OK
        )
        assert (
            json.loads(
                handle_function_call(
                    "atlas_email", {"operation": "propose", "arguments": EMAIL_PROPOSE}
                )
            )
            == EMAIL_APPROVAL
        )
        assert email_tool.mailbox_read_this_run() is False
        assert len(observed["import"]) == 4
        observed["import"].clear()
        observed["email"].clear()
    finally:
        email_tool.clear_email_capability(email_bound)
        import_tool.clear_import_capability(imp_bound)

    # 3. Same-batch pre-scan race: import call placed BEFORE a mailbox read (or bridge read, or session_search
    #    on an import turn with NO email capability) is refused in the same batch before callback execution.
    for has_email_cap, batch in (
        (
            True,
            [
                _tc("atlas_import_proposal", IMPORT_CHANGE_SET, "c1"),
                _tc("atlas_email", EMAIL_READ, "c2"),
            ],
        ),
        (
            True,
            [
                _tc("atlas_import", IMPORT_LOOKUP, "c1"),
                _tc(
                    "tool_call",
                    {"name": " atlas_email ", "arguments": EMAIL_LIST},
                    "c2",
                ),
            ],
        ),
        (
            False,
            [
                _tc("atlas_import_proposal", IMPORT_CHANGE_SET, "c1"),
                _tc("session_search", {"query": "Wheatland"}, "c2"),
            ],
        ),
    ):
        imp_bound = import_tool.bind_import_capability(IMP_TOKEN)
        email_bound = email_tool.bind_email_capability(
            EMAIL_TOKEN if has_email_cap else "", tainted=False
        )
        try:
            assert email_tool.taint_batch_if_it_reads_mailbox(batch) is True
            assert email_tool.mailbox_read_this_run() is True
            assert (
                json.loads(handle_function_call("atlas_import", IMPORT_LOOKUP))
                == TAINT_REFUSED
            )
            assert (
                json.loads(
                    handle_function_call("atlas_import_proposal", IMPORT_CHANGE_SET)
                )
                == TAINT_REFUSED
            )
            assert observed["import"] == []
        finally:
            email_tool.clear_email_capability(email_bound)
            import_tool.clear_import_capability(imp_bound)

    # 4. Mid-turn mailbox read (including a failed 500 read) and session_search without email capability
    #    deny atlas_import and atlas_import_proposal across every invocation boundary.
    db = SessionDB(tmp_path / "taint_search.db")
    try:
        past_sid = db.create_session("past-mail", "api_server")
        db.append_message(past_sid, role="user", content="Wheatland rent roll email")
        db.append_message(past_sid, role="assistant", content="Summary of Wheatland thread")

        for trigger in ("read_200", "read_500", "session_search_no_email_cap"):
            imp_bound = import_tool.bind_import_capability(IMP_TOKEN)
            email_bound = email_tool.bind_email_capability(
                "" if trigger == "session_search_no_email_cap" else EMAIL_TOKEN,
                tainted=False,
            )
            try:
                assert email_tool.mailbox_read_this_run() is False
                if trigger == "read_200":
                    res = json.loads(handle_function_call("atlas_email", EMAIL_READ))
                    assert "untrusted_email_content" in res
                elif trigger == "read_500":
                    prev_email_resp = responders["email"]
                    responders["email"] = lambda req: httpx.Response(500)
                    try:
                        res = json.loads(handle_function_call("atlas_email", EMAIL_READ))
                        assert res == {"error": "Email tool unavailable"}
                    finally:
                        responders["email"] = prev_email_resp
                else:
                    from tools.session_search_tool import session_search

                    res = json.loads(
                        session_search(
                            query="Wheatland", db=db, current_session_id="cur-import-sess"
                        )
                    )
                    assert res.get("success") is True

                assert email_tool.mailbox_read_this_run() is True
                observed["import"].clear()

                # (a) handle_function_call direct + Tool Search bridge
                for op_args in (IMPORT_EVIDENCE, IMPORT_CATALOG, IMPORT_LOOKUP):
                    assert (
                        json.loads(
                            handle_function_call(
                                "atlas_import",
                                op_args,
                                enabled_toolsets=["atlas_import", "atlas_email"],
                            )
                        )
                        == TAINT_REFUSED
                    )
                    assert (
                        json.loads(
                            handle_function_call(
                                "tool_call",
                                {"name": "atlas_import", "arguments": op_args},
                                enabled_toolsets=["atlas_import", "atlas_email"],
                            )
                        )
                        == TAINT_REFUSED
                    )
                assert (
                    json.loads(
                        handle_function_call(
                            "atlas_import_proposal",
                            IMPORT_CHANGE_SET,
                            enabled_toolsets=["atlas_import", "atlas_email"],
                        )
                    )
                    == TAINT_REFUSED
                )
                assert (
                    json.loads(
                        handle_function_call(
                            "tool_call",
                            {
                                "name": "atlas_import_proposal",
                                "arguments": IMPORT_CHANGE_SET,
                            },
                            enabled_toolsets=["atlas_import", "atlas_email"],
                        )
                    )
                    == TAINT_REFUSED
                )

                # (b) registry.dispatch
                assert (
                    json.loads(registry.dispatch("atlas_import", IMPORT_LOOKUP))
                    == TAINT_REFUSED
                )
                assert (
                    json.loads(
                        registry.dispatch("atlas_import_proposal", IMPORT_CHANGE_SET)
                    )
                    == TAINT_REFUSED
                )

                # (c) direct handler entrypoints in tools/atlas_import_tool.py
                assert (
                    json.loads(import_tool._handle_atlas_import(IMPORT_LOOKUP))
                    == TAINT_REFUSED
                )
                assert (
                    json.loads(
                        import_tool._handle_atlas_import_proposal(IMPORT_CHANGE_SET)
                    )
                    == TAINT_REFUSED
                )

                # (d) sequential and concurrent tool executors
                class _DummyGuardrails:
                    def before_call(self, name, args):
                        return SimpleNamespace(allows_execution=True)

                    def after_call(self, name, args, result, *, failed=False):
                        return SimpleNamespace(action="allow", should_halt=False)

                dummy_agent = SimpleNamespace(
                    session_id="sess-import-taint",
                    _current_turn_id="turn-1",
                    _current_api_request_id="req-1",
                    _context_engine_tool_names=set(),
                    _memory_manager=None,
                    _turns_since_memory=0,
                    _iters_since_skill=0,
                    _interrupt_requested=False,
                    quiet_mode=True,
                    verbose_logging=False,
                    log_prefix="",
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
                    valid_tool_names={"atlas_import", "atlas_import_proposal"},
                    enabled_toolsets=["atlas_import"],
                    disabled_toolsets=None,
                    tool_delay=0,
                    _tool_worker_threads=set(),
                    _tool_worker_threads_lock=__import__("threading").Lock(),
                    _invoke_tool=lambda fn_name, fn_args, task_id, tc_id=None, **kw: handle_function_call(
                        fn_name,
                        fn_args,
                        task_id,
                        tool_call_id=tc_id or "",
                        enabled_toolsets=["atlas_import"],
                    ),
                )
                batch_msg = SimpleNamespace(
                    tool_calls=[
                        _tc("atlas_import", IMPORT_LOOKUP, "seq_1"),
                        _tc("atlas_import_proposal", IMPORT_CHANGE_SET, "seq_2"),
                    ]
                )
                seq_out = []
                execute_tool_calls_sequential(
                    dummy_agent, batch_msg, seq_out, "task-seq", api_call_count=1
                )
                assert [json.loads(m["content"]) for m in seq_out] == [
                    TAINT_REFUSED,
                    TAINT_REFUSED,
                ]

                conc_out = []
                execute_tool_calls_concurrent(
                    dummy_agent, batch_msg, conc_out, "task-conc", api_call_count=1
                )
                assert [json.loads(m["content"]) for m in conc_out] == [
                    TAINT_REFUSED,
                    TAINT_REFUSED,
                ]

                assert observed["import"] == []

                # Control: on a tainted email turn, drafting an email proposal remains permitted.
                if trigger != "session_search_no_email_cap":
                    observed["email"].clear()
                    assert (
                        json.loads(
                            handle_function_call(
                                "atlas_email",
                                {"operation": "propose", "arguments": EMAIL_PROPOSE},
                            )
                        )
                        == EMAIL_APPROVAL
                    )
                    assert len(observed["email"]) == 1
            finally:
                email_tool.clear_email_capability(email_bound)
                import_tool.clear_import_capability(imp_bound)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cross_turn_taint_survives_executor_session_replayed_history_and_outreach_to_deny_import_without_fresh_email_capability(
    adapter, callbacks, monkeypatch
):
    """Defeating test 3 (Fails at ebe1c516): mailbox taint from an earlier turn survives executor cleanup,
    in-memory session state, an intervening outreach turn, and replayed/ancestor SessionDB history on a fresh
    adapter, stripping atlas_import at agent creation and refusing atlas_import and atlas_import_proposal at the
    real invocation boundary even when the subsequent import turn carries NO fresh atlas_email_capability."""
    import run_agent
    from agent.model_metadata import MINIMUM_CONTEXT_LENGTH
    from openai.types.chat import ChatCompletion

    observed, _ = callbacks
    db = adapter._session_db
    tainted_sid = db.create_session("cross-turn-tainted", "api_server")
    clean_sid = db.create_session("cross-turn-clean", "api_server")

    provider_calls = []
    next_tool_sequence = []
    call_seq = [0]

    def complete(**kwargs):
        provider_calls.append(copy.deepcopy(kwargs))
        if next_tool_sequence:
            calls = next_tool_sequence.pop(0)
            call_seq[0] += 1
            batch_no = call_seq[0]
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{batch_no}_{idx}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                    for idx, (name, args) in enumerate(calls, 1)
                ],
            }
            reason = "tool_calls"
        else:
            message, reason = {"role": "assistant", "content": "turn done"}, "stop"
        return ChatCompletion(
            id="synthetic",
            created=0,
            model="synthetic-model",
            object="chat.completion",
            choices=[{"index": 0, "finish_reason": reason, "message": message}],
        )

    class _Client:
        def __init__(self, **kwargs):
            self._client = SimpleNamespace(is_closed=False)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=complete))

        def close(self):
            self._client.is_closed = True

    real_agent = run_agent.AIAgent
    executor_boundary_probes = []

    class _SyntheticAgent(real_agent):
        def __init__(self, **kwargs):
            super().__init__(
                **{
                    **kwargs,
                    "skip_memory": True,
                    "skip_context_files": True,
                    "max_iterations": 4,
                }
            )
            self._disable_streaming = True

        def run_conversation(self, **kwargs):
            if email_tool.mailbox_read_this_run() and import_tool.import_capability_available():
                executor_boundary_probes.append(
                    {
                        "registry_import": json.loads(
                            registry.dispatch("atlas_import", copy.deepcopy(IMPORT_LOOKUP))
                        ),
                        "registry_proposal": json.loads(
                            registry.dispatch(
                                "atlas_import_proposal", copy.deepcopy(IMPORT_CHANGE_SET)
                            )
                        ),
                        "hfc_import": json.loads(
                            handle_function_call(
                                "atlas_import",
                                copy.deepcopy(IMPORT_LOOKUP),
                                enabled_toolsets=["atlas_import", "todo"],
                            )
                        ),
                        "hfc_proposal": json.loads(
                            handle_function_call(
                                "atlas_import_proposal",
                                copy.deepcopy(IMPORT_CHANGE_SET),
                                enabled_toolsets=["atlas_import", "todo"],
                            )
                        ),
                        "handler_import": json.loads(
                            import_tool._handle_atlas_import(copy.deepcopy(IMPORT_LOOKUP))
                        ),
                        "handler_proposal": json.loads(
                            import_tool._handle_atlas_import_proposal(
                                copy.deepcopy(IMPORT_CHANGE_SET)
                            )
                        ),
                    }
                )
            return super().run_conversation(**kwargs)

    monkeypatch.setattr(run_agent, "OpenAI", _Client)
    monkeypatch.setattr(run_agent, "AIAgent", _SyntheticAgent)
    monkeypatch.setattr("agent.agent_init.fetch_model_metadata", lambda *a, **k: None)
    monkeypatch.setattr(
        "agent.context_compressor.get_model_context_length",
        lambda *a, **k: MINIMUM_CONTEXT_LENGTH * 2,
    )
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs",
        lambda: {
            "api_key": "synthetic",
            "base_url": "http://example.invalid/v1",
            "provider": "openai",
            "api_mode": "chat_completions",
            "command": None,
            "args": [],
        },
    )
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "synthetic-model")
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"platform_toolsets": {"api_server": ["web", "todo"]}},
    )
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda *_a, **_k: {
            "web",
            "todo",
            "atlas_import",
            "atlas_ownership",
            "atlas_query",
            "atlas_email",
            "atlas_outreach",
        },
    )

    async with TestClient(TestServer(_app_for(adapter))) as client:
        # Turn 1 on tainted_sid: read_message taints the session and persists transcript to SessionDB.
        provider_calls.clear()
        next_tool_sequence[:] = [[("atlas_email", EMAIL_READ)]]
        r1 = await client.post(
            f"/api/sessions/{tainted_sid}/chat",
            json={"message": "Read message m1", "atlas_email_capability": EMAIL_TOKEN},
        )
        assert r1.status == 200
        assert (await r1.json())["atlas_email_mailbox_tainted"] is True
        assert len(observed["email"]) == 1
        assert not email_tool.mailbox_read_this_run()

        # Persist the Turn 1 read transcript into SessionDB (mirroring gateway session persistence)
        # so a fresh adapter instance and child branched session also recover taint from SessionDB.
        if not db.get_messages_as_conversation(tainted_sid):
            db.append_message(tainted_sid, role="user", content="Read message m1")
            db.append_message(
                tainted_sid,
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "atlas_email",
                            "arguments": json.dumps(EMAIL_READ),
                        },
                    }
                ],
            )
            db.append_message(
                tainted_sid,
                role="tool",
                tool_name="atlas_email",
                tool_call_id="call_1",
                content=json.dumps(
                    {
                        "untrusted_email_content": {
                            "message": {"id": "m1", "body": "Untrusted"}
                        }
                    }
                ),
            )
            db.append_message(tainted_sid, role="assistant", content="turn done")

        # Turn 2 on tainted_sid: intervening save-only outreach turn works in isolation and preserves session taint.
        provider_calls.clear()
        observed["outreach"].clear()
        next_tool_sequence[:] = [[("atlas_outreach_save", OUTREACH_PAYLOAD)]]
        r2 = await client.post(
            f"/api/sessions/{tainted_sid}/chat/stream",
            json={
                "message": "Save outreach draft",
                "atlas_outreach_capability": OUT_TOKEN,
                "toolset_scope": ["web", "todo"],
            },
        )
        assert r2.status == 200
        r2_completed = _extract_stream_completed(await r2.text())
        assert r2_completed["atlas_email_mailbox_tainted"] is True
        assert all(
            {t["function"]["name"] for t in call["tools"]} == {"atlas_outreach_save"}
            for call in provider_calls
        )
        assert len(observed["outreach"]) == 1
        assert not outreach_tool.outreach_capability_available()

        # Turn 3 on tainted_sid: import-only turn with NO fresh atlas_email_capability and NO taint flag in body.
        for endpoint in ("/chat", "/chat/stream"):
            provider_calls.clear()
            observed["import"].clear()
            next_tool_sequence[:] = [
                [
                    ("atlas_import", IMPORT_LOOKUP),
                    ("atlas_import_proposal", IMPORT_CHANGE_SET),
                ]
            ]
            r3 = await client.post(
                f"/api/sessions/{tainted_sid}{endpoint}",
                json={
                    "message": "Attempt import lookup and proposal after earlier mail read",
                    "atlas_import_capability": IMP_TOKEN,
                },
            )
            assert r3.status == 200
            raw3 = await r3.text()
            if endpoint == "/chat":
                assert json.loads(raw3)["atlas_email_mailbox_tainted"] is True
            else:
                assert (
                    _extract_stream_completed(raw3)["atlas_email_mailbox_tainted"]
                    is True
                )

            assert len(provider_calls) >= 2
            offered_tools = {t["function"]["name"] for t in provider_calls[0]["tools"]}
            assert "atlas_import" not in offered_tools
            assert "atlas_import_proposal" not in offered_tools
            assert "atlas_outreach_save" not in offered_tools
            raw_tool_replies = [
                m["content"]
                for m in provider_calls[-1]["messages"]
                if m.get("role") == "tool"
            ][-2:]
            assert len(raw_tool_replies) == 2
            for reply, expected_name in zip(
                raw_tool_replies, ("atlas_import", "atlas_import_proposal")
            ):
                parsed_reply = None
                try:
                    parsed_reply = json.loads(reply)
                except ValueError:
                    pass
                assert parsed_reply != IMPORT_OK
                assert "accepted" not in reply
                assert (
                    parsed_reply == TAINT_REFUSED
                    or f"Tool '{expected_name}' does not exist" in reply
                ), f"Unexpected reply for {expected_name}: {reply!r}"
            assert executor_boundary_probes[-1] == {
                "registry_import": TAINT_REFUSED,
                "registry_proposal": TAINT_REFUSED,
                "hfc_import": TAINT_REFUSED,
                "hfc_proposal": TAINT_REFUSED,
                "handler_import": TAINT_REFUSED,
                "handler_proposal": TAINT_REFUSED,
            }
            assert observed["import"] == []

    # Turn 4: Fresh APIServerAdapter instance sharing the same SessionDB (no in-memory _tainted_session_ids),
    # plus a child session branched from tainted_sid (ancestor history taint), plus explicit taint flag on clean_sid.
    fresh_adapter = APIServerAdapter(PlatformConfig(enabled=True))
    fresh_adapter._session_db = db
    child_sid = db.create_session(
        "child-of-tainted", "api_server", parent_session_id=tainted_sid
    )

    async with TestClient(TestServer(_app_for(fresh_adapter))) as client:
        for target_sid, extra_fields in (
            (tainted_sid, {}),
            (child_sid, {}),
            (clean_sid, {"atlas_email_mailbox_tainted": True}),
        ):
            provider_calls.clear()
            observed["import"].clear()
            next_tool_sequence[:] = [
                [
                    ("atlas_import", IMPORT_LOOKUP),
                    ("atlas_import_proposal", IMPORT_CHANGE_SET),
                ]
            ]
            resp = await client.post(
                f"/api/sessions/{target_sid}/chat",
                json={
                    "message": "Import attempt without email capability",
                    "atlas_import_capability": IMP_TOKEN,
                    **extra_fields,
                },
            )
            assert resp.status == 200
            assert (await resp.json())["atlas_email_mailbox_tainted"] is True
            offered_tools = {t["function"]["name"] for t in provider_calls[0]["tools"]}
            assert "atlas_import" not in offered_tools
            assert "atlas_import_proposal" not in offered_tools
            raw_tool_replies = [
                m["content"]
                for m in provider_calls[-1]["messages"]
                if m.get("role") == "tool"
            ][-2:]
            assert len(raw_tool_replies) == 2
            for reply, expected_name in zip(
                raw_tool_replies, ("atlas_import", "atlas_import_proposal")
            ):
                parsed_reply = None
                try:
                    parsed_reply = json.loads(reply)
                except ValueError:
                    pass
                assert parsed_reply != IMPORT_OK
                assert "accepted" not in reply
                assert (
                    parsed_reply == TAINT_REFUSED
                    or f"Tool '{expected_name}' does not exist" in reply
                ), f"Unexpected reply for {expected_name}: {reply!r}"
            assert executor_boundary_probes[-1] == {
                "registry_import": TAINT_REFUSED,
                "registry_proposal": TAINT_REFUSED,
                "hfc_import": TAINT_REFUSED,
                "hfc_proposal": TAINT_REFUSED,
                "handler_import": TAINT_REFUSED,
                "handler_proposal": TAINT_REFUSED,
            }
            assert observed["import"] == []

        # Clean session control: a separate untainted session that only proposed an email draft can run import tools.
        untainted_sid = db.create_session("clean-import-after-propose", "api_server")
        provider_calls.clear()
        observed["email"].clear()
        next_tool_sequence[:] = [
            [("atlas_email", {"operation": "propose", "arguments": EMAIL_PROPOSE})]
        ]
        r_clean_email = await client.post(
            f"/api/sessions/{untainted_sid}/chat",
            json={"message": "Propose draft only", "atlas_email_capability": EMAIL_TOKEN},
        )
        assert r_clean_email.status == 200
        assert (await r_clean_email.json())["atlas_email_mailbox_tainted"] is False

        provider_calls.clear()
        observed["import"].clear()
        next_tool_sequence[:] = [
            [
                ("atlas_import", IMPORT_LOOKUP),
                ("atlas_import_proposal", IMPORT_CHANGE_SET),
            ]
        ]
        r_clean_imp = await client.post(
            f"/api/sessions/{untainted_sid}/chat",
            json={
                "message": "Clean import turn after email propose",
                "atlas_import_capability": IMP_TOKEN,
            },
        )
        assert r_clean_imp.status == 200
        assert (await r_clean_imp.json())["atlas_email_mailbox_tainted"] is False
        offered_tools = {t["function"]["name"] for t in provider_calls[0]["tools"]}
        assert {"atlas_import", "atlas_import_proposal"} <= offered_tools
        tool_msgs = [
            json.loads(m["content"])
            for m in provider_calls[-1]["messages"]
            if m.get("role") == "tool"
        ][-2:]
        assert tool_msgs == [IMPORT_OK, IMPORT_OK]
        assert len(observed["import"]) == 2
