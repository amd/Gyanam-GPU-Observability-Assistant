# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Extra route coverage for targets.py: update/delete/test-connection, trigger-poll
error branches, CSV import validation branches, HTML add/edit forms, and the
placement helpers.
"""

import httpx
from src.api.csrf import generate_csrf_token
from src.api.routes import targets as targets_mod
from src.api.routes.targets import (
    COLLECTOR_POLL_URL,
    _placement_from_csv_row,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create(client, name="gpu-a", host="10.0.0.5", **extra):
    body = {"name": name, "host": host, "username": "admin", "password": "pw"}
    body.update(extra)
    r = await client.post("/targets/api", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _csv_files(content: str, filename: str = "targets.csv"):
    return {"file": (filename, content, "text/csv")}


# ---------------------------------------------------------------------------
# TargetUpdate validators (lines 225-265) + update/delete 404 (827, 839)
# ---------------------------------------------------------------------------


async def test_update_target_runs_all_validators(client):
    tid = await _create(client)
    # All validators run with valid values (name/host/port/connection_mode).
    r = await client.put(
        f"/targets/api/{tid}",
        json={
            "name": "renamed-1",
            "host": "10.0.0.6",
            "port": 8443,
            "connection_mode": "direct",
        },
    )
    assert r.status_code == 200, r.text


async def test_update_target_invalid_port_rejected(client):
    tid = await _create(client)
    r = await client.put(f"/targets/api/{tid}", json={"port": 0})
    assert r.status_code == 422


async def test_update_target_invalid_connection_mode_rejected(client):
    tid = await _create(client)
    r = await client.put(f"/targets/api/{tid}", json={"connection_mode": "bogus"})
    assert r.status_code == 422


async def test_update_target_not_found(client):
    r = await client.put("/targets/api/999999", json={"name": "x"})
    assert r.status_code == 404


async def test_delete_target_not_found(client):
    r = await client.delete("/targets/api/999999")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Test-connection endpoint (lines 847-898)
# ---------------------------------------------------------------------------


async def test_test_connection_target_not_found(client):
    r = await client.post("/targets/api/999999/test")
    assert r.status_code == 404


async def test_test_connection_direct_failure_status(client, httpx_mock):
    # token set -> RedfishClient.connect() skips the SessionService POST.
    tid = await _create(client, host="10.0.0.40", token="tok")
    httpx_mock.add_response(method="GET", url="https://10.0.0.40/redfish/v1/", status_code=401)
    r = await client.post(f"/targets/api/{tid}/test")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is False
    assert "401" in body["message"]


async def test_test_connection_direct_success(client, httpx_mock, monkeypatch):
    # Avoid the opportunistic inventory fetch making un-mocked network calls.
    async def _no_inventory(*a, **k):
        return None

    monkeypatch.setattr(targets_mod, "collect_inventory", _no_inventory)

    tid = await _create(client, host="10.0.0.41", token="tok")
    httpx_mock.add_response(
        method="GET",
        url="https://10.0.0.41/redfish/v1/",
        status_code=200,
        json={"Vendor": "AMD", "Product": "MI300", "RedfishVersion": "1.6.0"},
    )
    r = await client.post(f"/targets/api/{tid}/test")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert "AMD" in body["message"]


# ---------------------------------------------------------------------------
# trigger_poll error branches (lines 904-937)
# ---------------------------------------------------------------------------


async def test_trigger_poll_success(client, httpx_mock):
    tid = await _create(client, host="10.0.0.60")
    httpx_mock.add_response(
        method="POST",
        url=COLLECTOR_POLL_URL.format(target_id=tid),
        status_code=200,
        json={"status": "polled"},
    )
    r = await client.post(f"/targets/api/{tid}/poll")
    assert r.status_code == 200
    assert r.json() == {"status": "polled"}


async def test_trigger_poll_collector_404(client, httpx_mock):
    tid = await _create(client, host="10.0.0.61")
    httpx_mock.add_response(
        method="POST", url=COLLECTOR_POLL_URL.format(target_id=tid), status_code=404
    )
    r = await client.post(f"/targets/api/{tid}/poll")
    assert r.status_code == 404


async def test_trigger_poll_collector_503(client, httpx_mock):
    tid = await _create(client, host="10.0.0.62")
    httpx_mock.add_response(
        method="POST", url=COLLECTOR_POLL_URL.format(target_id=tid), status_code=503
    )
    r = await client.post(f"/targets/api/{tid}/poll")
    assert r.status_code == 503


async def test_trigger_poll_collector_other_status(client, httpx_mock):
    tid = await _create(client, host="10.0.0.63")
    httpx_mock.add_response(
        method="POST",
        url=COLLECTOR_POLL_URL.format(target_id=tid),
        status_code=500,
        text="boom",
    )
    r = await client.post(f"/targets/api/{tid}/poll")
    assert r.status_code == 500


async def test_trigger_poll_timeout(client, httpx_mock):
    tid = await _create(client, host="10.0.0.64")
    httpx_mock.add_exception(
        httpx.TimeoutException("slow"), url=COLLECTOR_POLL_URL.format(target_id=tid)
    )
    r = await client.post(f"/targets/api/{tid}/poll")
    assert r.status_code == 504


async def test_trigger_poll_request_error(client, httpx_mock):
    tid = await _create(client, host="10.0.0.65")
    httpx_mock.add_exception(
        httpx.ConnectError("no route"), url=COLLECTOR_POLL_URL.format(target_id=tid)
    )
    r = await client.post(f"/targets/api/{tid}/poll")
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# CSV import validation/error branches (lines 1040-1112 in the import loop)
# ---------------------------------------------------------------------------


async def test_import_rejects_non_csv(client):
    r = await client.post("/targets/api/import", files=_csv_files("x", filename="t.txt"))
    assert r.status_code == 400


async def test_import_rejects_non_utf8(client):
    files = {"file": ("t.csv", b"\xff\xfe\x00bad", "text/csv")}
    r = await client.post("/targets/api/import", files=files)
    assert r.status_code == 400


async def test_import_rejects_empty_file(client):
    r = await client.post("/targets/api/import", files=_csv_files(""))
    assert r.status_code == 400


async def test_import_bad_port(client):
    csv = "host name,bmc address,username,password,port\ngpu-a,10.0.0.70,u,p,99999\n"
    r = await client.post("/targets/api/import", files=_csv_files(csv))
    assert r.status_code == 200, r.text
    assert r.json()["errors"] == 1


async def test_import_missing_username(client):
    csv = "host name,bmc address,password\ngpu-a,10.0.0.71,p\n"
    r = await client.post("/targets/api/import", files=_csv_files(csv))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["errors"] == 1
    assert "sername" in body["details"]["errors"][0]["error"]


async def test_import_missing_password(client):
    csv = "host name,bmc address,username\ngpu-a,10.0.0.72,u\n"
    r = await client.post("/targets/api/import", files=_csv_files(csv))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["errors"] == 1
    assert "assword" in body["details"]["errors"][0]["error"]


async def test_import_invalid_connection_mode(client):
    csv = "host name,bmc address,username,password,connection_mode\ngpu-a,10.0.0.73,u,p,bogus\n"
    r = await client.post("/targets/api/import", files=_csv_files(csv))
    assert r.status_code == 200, r.text
    assert r.json()["errors"] == 1


async def test_import_duplicate_existing_host_skipped(client):
    await _create(client, host="10.0.0.80")
    csv = "host name,bmc address,username,password\ndup,10.0.0.80,u,p\n"
    r = await client.post("/targets/api/import", files=_csv_files(csv))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] == 1
    assert body["details"]["skipped"][0]["reason"] == "Host already exists"


async def test_import_within_file_dedup(client):
    csv = "host name,bmc address,username,password\ngpu-a,10.0.0.81,u,p\ngpu-a2,10.0.0.81,u,p\n"
    r = await client.post("/targets/api/import", files=_csv_files(csv))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 1
    assert body["skipped"] == 1


async def test_import_tags_parsed_and_invalid(client):
    # Valid JSON tags create the target; invalid JSON tags fail that row.
    csv = (
        "host name,bmc address,username,password,tags\n"
        'gpu-good,10.0.0.82,u,p,"{""env"": ""lab""}"\n'
        "gpu-bad,10.0.0.83,u,p,notjson\n"
    )
    r = await client.post("/targets/api/import", files=_csv_files(csv))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 1
    assert body["errors"] == 1


async def test_import_empty_row_skipped(client):
    # A blank row (no name/host) is silently skipped; the good row is created.
    csv = "host name,bmc address,username,password\n,,,\ngpu-a,10.0.0.84,u,p\n"
    r = await client.post("/targets/api/import", files=_csv_files(csv))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 1


async def test_import_with_location_columns(client):
    # loc_* columns produce a manual placement that is stored for the target.
    csv = (
        "host name,bmc address,username,password,loc_site,loc_rack,loc_rack_u\n"
        "gpu-a,10.0.0.85,u,p,site1,r1,10\n"
    )
    r = await client.post("/targets/api/import", files=_csv_files(csv))
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1


# ---------------------------------------------------------------------------
# Placement helper (lines 339-366)
# ---------------------------------------------------------------------------


def test_placement_from_csv_row_valid():
    placement = _placement_from_csv_row({"loc_site": "site1", "loc_rack": "r1", "loc_rack_u": "10"})
    assert placement is not None
    assert placement.has_location()


def test_placement_from_csv_row_blank_returns_none():
    assert _placement_from_csv_row({"loc_site": "", "loc_rack": ""}) is None


def test_placement_from_csv_row_bad_int_returns_none():
    assert _placement_from_csv_row({"loc_rack_u": "not-an-int"}) is None


# ---------------------------------------------------------------------------
# HTML page endpoints (lines 969-994) and add/edit forms (1040-1112, 1160-1260)
# ---------------------------------------------------------------------------


async def test_add_target_page_renders(client):
    r = await client.get("/targets/add")
    assert r.status_code == 200


async def test_edit_target_page_renders(client):
    tid = await _create(client, host="10.0.0.90")
    r = await client.get(f"/targets/{tid}/edit")
    assert r.status_code == 200


async def test_edit_target_page_not_found(client):
    r = await client.get("/targets/999999/edit")
    assert r.status_code == 404


async def test_add_target_form_success_direct(client):
    data = {
        "name": "form-direct",
        "host": "10.0.0.91",
        "port": "443",
        "use_ssl": "true",
        "username": "admin",
        "password": "pw",
        "enabled": "on",
        "connection_mode": "direct",
        # A non-default metric report exercises the override builder.
        "metric_report_processor": "/redfish/v1/TelemetryService/MetricReports/Custom",
        "csrf_token": generate_csrf_token(),
    }
    r = await client.post("/targets/add", data=data, follow_redirects=False)
    assert r.status_code == 303


async def test_add_target_form_validation_error(client):
    data = {
        "name": "bad name!!",  # invalid characters -> ValueError -> error template
        "host": "10.0.0.92",
        "username": "admin",
        "password": "pw",
        "csrf_token": generate_csrf_token(),
    }
    r = await client.post("/targets/add", data=data, follow_redirects=False)
    assert r.status_code == 200  # re-renders the form with an error


async def test_edit_target_form_success(client):
    tid = await _create(client, host="10.0.0.95")
    data = {
        "name": "edited",
        "host": "10.0.0.96",
        "port": "443",
        "use_ssl": "true",
        "username": "admin",
        # password blank -> keeps existing stored password
        "connection_mode": "direct",
        "csrf_token": generate_csrf_token(),
    }
    r = await client.post(f"/targets/{tid}/edit", data=data, follow_redirects=False)
    assert r.status_code == 303


async def test_edit_target_form_validation_error(client):
    tid = await _create(client, host="10.0.0.97")
    data = {
        "name": "ok-name",
        "host": "10.0.0.98",
        "port": "70000",  # invalid port -> error template
        "username": "admin",
        "password": "pw",
        "connection_mode": "direct",
        "csrf_token": generate_csrf_token(),
    }
    r = await client.post(f"/targets/{tid}/edit", data=data, follow_redirects=False)
    assert r.status_code == 200


async def test_edit_page_reflects_metric_override(client):
    # Create a target with a non-default metric report via the add form so the
    # stored override exercises _build_metric_reports_map on the edit page.
    data = {
        "name": "override-sys",
        "host": "10.0.0.101",
        "username": "admin",
        "password": "pw",
        "connection_mode": "direct",
        "metric_report_memory": "/redfish/v1/TelemetryService/MetricReports/MyMem",
        "csrf_token": generate_csrf_token(),
    }
    r = await client.post("/targets/add", data=data, follow_redirects=False)
    assert r.status_code == 303
    listing = (await client.get("/targets/api")).json()
    tid = next(t["id"] for t in listing if t["host"] == "10.0.0.101")
    r = await client.get(f"/targets/{tid}/edit")
    assert r.status_code == 200


async def test_delete_target_form(client):
    tid = await _create(client, host="10.0.0.102")
    r = await client.post(
        f"/targets/{tid}/delete",
        data={"csrf_token": generate_csrf_token()},
        follow_redirects=False,
    )
    assert r.status_code == 303


async def test_delete_target_form_not_found(client):
    r = await client.post(
        "/targets/999999/delete",
        data={"csrf_token": generate_csrf_token()},
        follow_redirects=False,
    )
    assert r.status_code == 404
