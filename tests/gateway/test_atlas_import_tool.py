"""The Atlas import tool stays fixed, bounded, and private to its turn."""

import json
from unittest.mock import patch

import httpx

from tools import atlas_import_tool as tool


def test_import_tool_uses_fixed_loopback_and_out_of_band_token():
    observed = {}

    def callback(request):
        observed["url"] = str(request.url)
        observed["authorization"] = request.headers["Authorization"]
        observed["body"] = json.loads(request.content)
        return httpx.Response(200, json={"records": [{"id": "one"}]})

    client = httpx.Client(transport=httpx.MockTransport(callback), follow_redirects=False)
    secret = "atimp_" + "a" * 32
    bound = tool.bind_import_capability(secret)
    try:
        with patch.object(tool.httpx, "Client", return_value=client):
            result = json.loads(tool._handle_atlas_import({
                "operation": "lookup", "arguments": {"query": "Wheatland"}}))
    finally:
        tool.clear_import_capability(bound)

    assert result == {"records": [{"id": "one"}]}
    assert observed == {
        "url": "http://127.0.0.1:8243/api/v1/internal/import-tool",
        "authorization": "Bearer " + secret,
        "body": {"operation": "lookup", "arguments": {"query": "Wheatland"}},
    }
    assert secret not in json.dumps(result)
    assert json.loads(tool._handle_atlas_import({"operation": "evidence", "arguments": {}})) == {
        "error": "Import capability unavailable"}


def test_import_tool_rejects_bad_payload_and_redirect_without_leaking_token():
    requests = []

    def callback(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://example.invalid"})

    client = httpx.Client(transport=httpx.MockTransport(callback), follow_redirects=False)
    bound = tool.bind_import_capability("atimp_" + "b" * 32)
    try:
        assert "error" in json.loads(tool._handle_atlas_import({"operation": "execute", "arguments": {}}))
        assert "error" in json.loads(tool._handle_atlas_import({"operation": "propose", "arguments": {}}))
        assert "error" in json.loads(tool._handle_atlas_import_proposal({
            "operation": "propose", "property_id": "PRO-test", "proposal": {"data": "x" * (128 * 1024)}}))
        with patch.object(tool.httpx, "Client", return_value=client):
            assert json.loads(tool._handle_atlas_import({"operation": "catalog", "arguments": {}})) == {
                "error": "Import operation unavailable"}
    finally:
        tool.clear_import_capability(bound)
    assert len(requests) == 1


def test_import_tool_accepts_a_bounded_multirow_proposal():
    seen = []
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: (seen.append(json.loads(request.content)), httpx.Response(200, json={"accepted": True}))[1]),
        follow_redirects=False)
    bound = tool.bind_import_capability("atimp_" + "e" * 32)
    try:
        with patch.object(tool.httpx, "Client", return_value=client):
            result = json.loads(tool._handle_atlas_import_proposal({
                "operation": "propose", "property_id": "PRO-" + "a" * 36,
                "proposal": {"rows": [{"cited_rows": "x" * 20_000}], "unmapped": [], "warnings": []}}))
    finally:
        tool.clear_import_capability(bound)
    assert result == {"accepted": True}
    assert seen[0]["operation"] == "propose"
    assert seen[0]["arguments"]["property_id"] == "PRO-" + "a" * 36
    assert 20_000 < len(json.dumps(seen[0])) < 128 * 1024


def test_import_tool_timeout_covers_bounded_filemaker_lookups():
    captured = {}
    bound = tool.bind_import_capability("atimp_" + "f" * 32)
    try:
        real_client = httpx.Client(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"accepted": True})))
        def client_factory(**kwargs):
            captured["timeout"] = kwargs["timeout"]
            return real_client
        with patch.object(tool.httpx, "Client", side_effect=client_factory):
            assert json.loads(tool._handle_atlas_import_proposal({
                "operation": "propose", "property_id": "PRO-" + "a" * 36,
                "proposal": {}})) == {"accepted": True}
    finally:
        tool.clear_import_capability(bound)
    assert captured["timeout"].read == 90


def test_import_tool_schema_requires_an_explicit_bound_turn():
    from model_tools import get_tool_definitions

    def names():
        return {item["function"]["name"] for item in get_tool_definitions(
            enabled_toolsets=["atlas_import"], quiet_mode=True)}

    assert "atlas_import" not in names()
    assert "atlas_import_proposal" not in names()
    bound = tool.bind_import_capability("atimp_" + "c" * 32)
    try:
        assert "atlas_import" in names()
        assert "atlas_import_proposal" in names()
    finally:
        tool.clear_import_capability(bound)
    assert "atlas_import" not in names()
    assert "atlas_import_proposal" not in names()


def test_proposal_tool_exposes_required_typed_payload_to_the_model():
    from model_tools import get_tool_definitions
    bound = tool.bind_import_capability("atimp_" + "a" * 32)
    try:
        definitions = {item["function"]["name"]: item["function"]["parameters"]
                       for item in get_tool_definitions(enabled_toolsets=["atlas_import"], quiet_mode=True)}
    finally:
        tool.clear_import_capability(bound)
    read = definitions["atlas_import"]
    proposal = definitions["atlas_import_proposal"]
    assert read["properties"]["operation"]["enum"] == ["catalog", "evidence", "lookup"]
    assert proposal["required"] == ["operation", "property_id", "proposal"]
    assert proposal["properties"]["proposal"]["required"] == ["rows", "unmapped", "warnings"]
    assert proposal["properties"]["proposal"]["properties"]["rows"]["items"]["properties"]["fields"]["items"]["required"] == [
        "field", "value", "source_value", "source_evidence", "transformation"]


def test_proposal_tool_refuses_empty_property_and_payload_before_callback():
    assert "error" in json.loads(tool._handle_atlas_import_proposal({"operation": "propose"}))
    assert "error" in json.loads(tool._handle_atlas_import_proposal({
        "operation": "revise", "property_id": "PRO-" + "a" * 36, "proposal": {}}))


def test_import_tool_rejects_oversized_callback_response():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, content=b"x" * 32_769)),
        follow_redirects=False)
    bound = tool.bind_import_capability("atimp_" + "d" * 32)
    try:
        with patch.object(tool.httpx, "Client", return_value=client):
            assert json.loads(tool._handle_atlas_import({
                "operation": "evidence", "arguments": {}})) == {
                    "error": "Import response exceeds limit"}
    finally:
        tool.clear_import_capability(bound)
