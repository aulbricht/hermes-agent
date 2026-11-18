"""SNO-3930 offline mounted Atlas contract; provision .contract/acre-atlas first.

Only Lightbox HTTPS is stubbed. Hermes and ASGI transports, registered handlers,
Atlas's mounted app/router, real token mint/verify/revoke, SQLite and ledger run.
"""
import asyncio
import copy
import importlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from tools import atlas_ownership_tool as tool
from model_tools import handle_function_call

OP = "parcels_within_radius"
ARGS = {"latitude": 47.6572, "longitude": -117.2912, "radius": 500.5, "unit": "ft"}
ADDRESS = " 4502 E Sprague Ave, Spokane Valley, WA "
PROPERTY = "PRO-00000000-0000-0000-0000-000000000001"
ERROR = {"error": "Ownership operation unavailable"}


# Recorded source facts from SNO-3384's synthetic page, with the documented
# $metadata envelope and nested Geocoding shape recorded in its review evidence.
# Independent new samples: no import, mutation or compatibility with SNO-3829 tests.
def parcels():
    return {"$metadata": {"recordSet": {"totalRecords": 7, "limit": 100, "offset": 0}},
            "parcels": [
                {"id": "0202P9GBKVNO0LIUTDCJ47",
                 "location": {"streetAddress": "4502 E SPRAGUE AVE", "locality": "SPOKANE VALLEY",
                              "regionCode": "WA", "postalCode": "99212", "postalCodeExt": "0401",
                              "countryCode": "US",
                              "representativePoint": {"latitude": 47.6572, "longitude": -117.2912}},
                 "owner": {"ownerNameStd": "SPRAGUE VALLEY HOLDINGS LLC",
                           "names": [{"fullName": None}, {"fullName": "CHRIS SAMPLE"}],
                           "mailingAddress": {"streetAddress": "777 PRIVATE MAILING LN"}},
                 "transaction": {"lastMarketSale": {"transferDate": "2024-06-15", "value": 1850000,
                                                     "documentNumber": "PRIVATE-DOC-123"}},
                 "assessment": {"privateAssessorNote": "do-not-cross-seam"}},
                {"id": "0202VACANTLOTPARCEL003",
                 "location": {"streetAddress": None, "locality": "SPOKANE VALLEY", "regionCode": "WA",
                              "postalCode": "99212", "postalCodeExt": None, "countryCode": "US",
                              "representativePoint": {"latitude": 47.6575, "longitude": -117.2908}},
                 "owner": None, "transaction": None},
            ]}


def addresses():
    return {"addresses": [{
        "id": "0606ADDRESSID000000001",
        "location": {"streetAddress": "4502 E SPRAGUE AVE", "locality": "SPOKANE VALLEY",
                     "regionCode": "WA", "postalCode": "99212", "countryCode": "US",
                     "representativePoint": {"latitude": 47.6572, "longitude": -117.2912}},
        "$metadata": {"geocode": {
            "confidence": {"score": 0.98, "streetNumber": 1, "streetName": 1,
                           "locality": 1, "postalCode": 1},
            "precisionCode": 10, "changeFlag": None, "addressComponents": None, "aliases": None}},
        "parcels": [{"id": "0202P9GBKVNO0LIUTDCJ47"}],
    }]}


@pytest.fixture
def seam(tmp_path, monkeypatch):
    hermes_root = Path(__file__).resolve().parents[2]
    atlas_root = hermes_root / ".contract" / "acre-atlas"
    backend = atlas_root / "apps" / "atlas-dashboard" / "backend"
    assert (backend / "atlas").is_dir(), (
        "Mount the reviewed Atlas source at .contract/acre-atlas; do not skip this contract")
    print("Hermes source:", subprocess.check_output(
        ["git", "-C", str(hermes_root), "rev-parse", "HEAD"], text=True).strip())
    print("Atlas source:", subprocess.check_output(
        ["git", "-C", str(atlas_root), "rev-parse", "HEAD"], text=True).strip())
    monkeypatch.syspath_prepend(str(backend))
    monkeypatch.chdir(tmp_path)  # Settings must not read any workstation .env.
    for name, value in {
        "ATLAS_ENV": "test", "ATLAS_DB_PATH": str(tmp_path / "atlas.db"),
        "ATLAS_STATE_ROOT": str(tmp_path / "state"), "ATLAS_STATIC_DIR": str(tmp_path / "dist"),
        "ATLAS_DEV_AUTH": "1", "ATLAS_SEED_DATA": "0",
        "ATLAS_ALLOWED_ORIGIN": "http://atlas.test",
        "ATLAS_LIGHTBOX_API_KEY": "synthetic-lightbox-trial-key-do-not-log",
        "ATLAS_OWNERSHIP_TOOL_ENABLED": "1", "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": str(tmp_path / "no-aws-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "no-aws-credentials"),
    }.items():
        monkeypatch.setenv(name, value)
    from atlas.config import get_settings
    get_settings.cache_clear()
    from atlas.db import Database
    from atlas import ownership_work as work, ownership_tool_capability as capability
    from atlas import ownership_lightbox as ledger
    from atlas.util import utc_now
    app_module = importlib.import_module("atlas.app")
    app = app_module.app
    app_module.RATE_BUCKETS.clear()
    db = Database(tmp_path / "atlas.db")
    db.migrate()
    now = utc_now()
    with db.transaction(immediate=True) as c:
        c.execute("""INSERT INTO users(id,email,display_name,role,created_at,updated_at)
                     VALUES('owner','owner@example.invalid','Owner','admin',?,?)""", (now, now))
        c.execute("""INSERT INTO chat_session_bindings
                     (id,user_id,hermes_session_id,title,archived,created_at,updated_at)
                     VALUES('session','owner','synthetic-hermes','Offline',0,?,?)""", (now, now))
        c.execute("""INSERT INTO chat_turn_status
                     (id,user_id,session_binding_id,client_turn_id,status,created_at,updated_at)
                     VALUES('turn-1','owner','session','synthetic-turn','pending',?,?)""", (now, now))
    evidence = {"property_id": PROPERTY,
                "observations": [{"source_ref": "saved:1", "text": "Company Alpha manages this Property."},
                                 {"source_ref": "saved:2", "text": "Company Beta is another contact."}],
                "contacts": [{"id": "c1", "display_name": "Company Alpha"},
                             {"id": "c2", "display_name": "Company Beta"}],
                "existing_associations": [{"contact_id": "c1", "role": "Property Manager"}]}
    owner = SimpleNamespace(id="owner")
    row = work.create(db, owner, "session", PROPERTY, "Find contacts", evidence)
    work.bind_turn(db, owner, "session", row["id"], "turn-1")
    token = capability.mint_ownership_tool_capability(
        db, user_id="owner", work_id=row["id"], session_id="session", turn_id="turn-1")
    monkeypatch.setattr(app.state, "database", db, raising=False)

    class CRMTripwire:
        calls = 0
        def __getattr__(self, name):
            self.calls += 1
            raise AssertionError("Contract attempted CRM access: " + name)
    crm = CRMTripwire()
    monkeypatch.setattr(app.state, "filemaker", crm, raising=False)
    wire = []
    calls = []
    behavior = {"status": 200, "stop": False, "timeout": False}
    payloads = {"parcels": parcels(), "addresses": addresses()}

    async def request_app(method, url, headers, content=b""):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
            follow_redirects=False, trust_env=False,
        ) as client:
            response = await client.request(method, url, headers=headers, content=content)
            return response.status_code, dict(response.headers), response.content

    def stop():
        status, _, raw = asyncio.run(request_app(
            "POST", "http://atlas.test/api/v1/chat/turns/turn-1/cancel",
            {"X-Atlas-Dev-User": "owner@example.invalid", "Origin": "http://atlas.test",
             "X-Atlas-CSRF": "1"}))
        assert status == 200, raw
        assert db.fetch_one("SELECT status FROM chat_turn_status WHERE id='turn-1'")["status"] == "cancelled"

    class MountedTransport(httpx.BaseTransport):
        def handle_request(self, request):
            assert str(request.url) == "http://127.0.0.1:8243/api/v1/internal/ownership-tool"
            assert request.headers["authorization"] == "Bearer " + token
            wire.append(json.loads(request.content))
            status, headers, raw = asyncio.run(request_app(
                request.method, str(request.url), dict(request.headers), request.content))
            return httpx.Response(status, headers=headers, content=raw, request=request)

    client_type = httpx.Client
    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return client_type(transport=MountedTransport(), **kwargs)
    monkeypatch.setattr(tool, "httpx", SimpleNamespace(
        Client=factory, Timeout=httpx.Timeout, HTTPError=httpx.HTTPError))

    class ProviderResponse:
        def __init__(self, status, data):
            self.status, self.data = status, data
        def read(self, size):
            assert size == 1024 * 1024 + 1
            return self.data[:size]

    class ProviderConnection:
        def __init__(self, host, timeout=None):
            assert host == ledger.HOST
            assert timeout == 15
        def request(self, method, url, body=None, headers=None):
            assert method == "GET"
            assert headers["x-api-key"] == "synthetic-lightbox-trial-key-do-not-log"
            self.record = {"path": urlsplit(url).path, "query": parse_qs(urlsplit(url).query),
                           "reserved": db.fetch_one("SELECT COUNT(*) n FROM ownership_lightbox_calls")["n"],
                           "closed": False}
            calls.append(self.record)
            assert self.record["reserved"] >= len(calls)
            if behavior["stop"] and self.record["path"] == "/v1/addresses/search":
                stop()
            if behavior["timeout"]:
                raise TimeoutError("synthetic private provider failure")
            payload = payloads["addresses" if self.record["path"] == "/v1/addresses/search" else "parcels"]
            self.response = ProviderResponse(behavior["status"], json.dumps(payload).encode())
        def getresponse(self):
            return self.response
        def close(self):
            if hasattr(self, "record"):
                self.record["closed"] = True

    import http.client
    monkeypatch.setattr(http.client, "HTTPSConnection", ProviderConnection)
    reset = tool.bind_ownership_capability(token)

    def invoke(arguments):
        return json.loads(handle_function_call("atlas_ownership",
            {"operation": OP, "arguments": arguments}))

    value = SimpleNamespace(db=db, calls=calls, wire=wire, payloads=payloads,
                            behavior=behavior, invoke=invoke, stop=stop, token=token)
    try:
        yield value
    finally:
        tool.clear_ownership_capability(reset)
        get_settings.cache_clear()
        assert crm.calls == 0
        for table in ["approval_snapshots", "document_import_actions", "ownership_work_revisions"]:
            assert db.fetch_one("SELECT COUNT(*) n FROM " + table)["n"] == 0
        assert all(item["closed"] for item in calls)


@pytest.mark.parametrize("address", [False, True])
def test_documented_shapes_sparse_facts_single_page_and_exact_address(seam, address):
    args = {"address": ADDRESS, "radius": 500.5, "unit": "ft"} if address else dict(ARGS)
    args.update(limit=100, property_id=PROPERTY)
    original = copy.deepcopy(seam.payloads)
    response = seam.invoke(args)
    assert response["ok"] is True, response
    result = response["result"]
    assert result["center"]["property_id"] == PROPERTY
    assert result["center"]["address"] == (ADDRESS if address else None)
    assert result["truncated"] is True
    assert result["total_records"] == 7
    assert result["returned_count"] == 2
    by_id = {p["id"]: p for p in result["parcels"]}
    first = by_id["0202P9GBKVNO0LIUTDCJ47"]
    assert first["location"]["streetAddress"] == "4502 E SPRAGUE AVE"
    assert first["owner"] == {"owner_name_std": "SPRAGUE VALLEY HOLDINGS LLC",
                              "names": [None, "CHRIS SAMPLE"]}
    assert first["last_market_sale"] == {"transfer_date": "2024-06-15", "value": 1850000,
                                          "market_sale_only": True}
    vacant = by_id["0202VACANTLOTPARCEL003"]
    assert vacant["location"]["streetAddress"] is None
    assert vacant["owner"] == {"owner_name_std": None, "names": []}
    assert vacant["last_market_sale"]["transfer_date"] is None
    assert vacant["last_market_sale"]["value"] is None
    assert all(p["distance_feet"] == pytest.approx(p["distance_miles"] * 5280)
               for p in result["parcels"])
    assert "PRIVATE" not in json.dumps(result)
    assert "do-not-cross-seam" not in json.dumps(result)
    assert seam.payloads == original
    expected_paths = ["/v1/addresses/search", "/v1/parcels/us/geometry"] if address else ["/v1/parcels/us/geometry"]
    assert [r["path"] for r in seam.calls] == expected_paths
    assert [r["reserved"] for r in seam.calls] == list(range(1, len(expected_paths) + 1))
    assert seam.calls[-1]["query"]["offset"] == ["0"]
    assert seam.calls[-1]["query"]["limit"] == ["100"]
    if address:
        assert seam.calls[0]["query"]["text"] == [ADDRESS]
    assert len(seam.wire) == 1


@pytest.mark.parametrize("status", [401, 403, 429])
@pytest.mark.parametrize("address", [False, True])
def test_provider_denial_is_fixed_one_attempt_and_reservation_retained(seam, status, address, caplog):
    seam.behavior["status"] = status
    args = {"address": ADDRESS, "radius": 1, "unit": "mi"} if address else ARGS
    result = seam.invoke(args)
    code = "LIGHTBOX_TRIAL_KEY_DENIED" if status in [401, 403] else "OWNERSHIP_LIGHTBOX_FETCH_FAILED"
    assert result == {"protocol": "acre.ownership-tool.v1", "ok": False, "error": {"code": code}}
    assert len(seam.calls) == 1
    assert seam.calls[0]["reserved"] == 1
    assert seam.db.fetch_one("SELECT COUNT(*) n FROM ownership_lightbox_calls")["n"] == 1
    assert seam.token not in caplog.text
    assert "synthetic-lightbox-trial-key-do-not-log" not in caplog.text


def test_timeout_keeps_one_reservation_without_retry(seam):
    seam.behavior["timeout"] = True
    response = seam.invoke(ARGS)
    assert response["error"] == {"code": "OWNERSHIP_LIGHTBOX_FETCH_FAILED"}
    assert len(seam.calls) == 1
    assert seam.db.fetch_one("SELECT COUNT(*) n FROM ownership_lightbox_calls")["n"] == 1


def test_actual_stop_before_callback_spends_nothing(seam):
    seam.stop()
    assert seam.invoke(ARGS) == ERROR
    assert seam.calls == []
    assert len(seam.wire) == 1
    assert seam.db.fetch_one("SELECT COUNT(*) n FROM ownership_lightbox_calls")["n"] == 0


def test_actual_stop_between_geocode_and_parcels(seam):
    seam.behavior["stop"] = True
    assert seam.invoke({"address": ADDRESS, "radius": 1, "unit": "mi"}) == ERROR
    assert [r["path"] for r in seam.calls] == ["/v1/addresses/search"]
    assert seam.calls[0]["reserved"] == 1
    assert seam.db.fetch_one("SELECT COUNT(*) n FROM ownership_lightbox_calls")["n"] == 1


def test_budget_boundary_reserves_geocode_then_refuses_parcels(seam):
    from atlas import ownership_lightbox as ledger
    now = datetime.now(timezone.utc)
    for _ in range(999):
        ledger.reserve(seam.db, SimpleNamespace(id="owner"), "session", PROPERTY, now)
    response = seam.invoke({"address": ADDRESS, "radius": 1, "unit": "mi"})
    assert response["error"] == {"code": "OWNERSHIP_LIGHTBOX_KEY_BUDGET"}
    assert [r["path"] for r in seam.calls] == ["/v1/addresses/search"]
    assert seam.calls[0]["reserved"] == 1000
    assert seam.db.fetch_one("SELECT COUNT(*) n FROM ownership_lightbox_calls")["n"] == 1000


def test_atlas_enforces_converted_radius_before_provider_dispatch(seam):
    response = seam.invoke({**ARGS, "radius": 101, "unit": "mi"})
    assert response == {"protocol": "acre.ownership-tool.v1", "ok": False,
                        "error": {"code": "LIGHTBOX_INPUT_INVALID"}}
    assert len(seam.wire) == 1
    assert seam.calls == []
    assert seam.db.fetch_one("SELECT COUNT(*) n FROM ownership_lightbox_calls")["n"] == 0
