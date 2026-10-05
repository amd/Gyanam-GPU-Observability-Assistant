# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Route tests for target CRUD and bulk CSV import."""


async def test_list_targets_empty(client):
    r = await client.get("/targets/api")
    assert r.status_code == 200
    assert r.json() == []


async def test_create_and_list_target(client):
    body = {"name": "gpu1", "host": "10.0.0.9", "username": "admin", "password": "pw"}
    r = await client.post("/targets/api", json=body)
    assert r.status_code == 200, r.text
    listing = (await client.get("/targets/api")).json()
    assert len(listing) == 1
    assert listing[0]["host"] == "10.0.0.9"
    # password must never be returned
    assert "password" not in listing[0]


async def test_create_target_rejects_loopback_host(client):
    body = {"name": "bad", "host": "127.0.0.1", "username": "a", "password": "b"}
    r = await client.post("/targets/api", json=body)
    assert r.status_code in (400, 422)  # validation rejects loopback


async def test_create_duplicate_host_rejected(client):
    body = {"name": "gpu1", "host": "10.0.0.9", "username": "admin", "password": "pw"}
    assert (await client.post("/targets/api", json=body)).status_code == 200
    dup = await client.post("/targets/api", json={**body, "name": "gpu1b"})
    assert dup.status_code == 400


async def test_bulk_csv_import(client):
    # connection_mode omitted -> defaults to "direct". The loopback row is
    # rejected by validate_host; the good row is created.
    csv = (
        "name,host,username,password\n"
        "good-1,10.0.0.11,admin,pw\n"
        "bad-1,127.0.0.1,admin,pw\n"  # loopback -> validation error
    )
    files = {"file": ("targets.csv", csv, "text/csv")}
    r = await client.post("/targets/api/import", files=files)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["created"] == 1
    assert result["errors"] == 1
    assert result["details"]["errors"][0]["host"] == "127.0.0.1"

    listing = (await client.get("/targets/api")).json()
    assert any(t["host"] == "10.0.0.11" for t in listing)
    assert not any(t["host"] == "127.0.0.1" for t in listing)


async def test_bulk_csv_missing_required_columns(client):
    files = {"file": ("t.csv", "bmc address,username\n10.0.0.5,admin\n", "text/csv")}
    r = await client.post("/targets/api/import", files=files)
    assert r.status_code == 400  # 'host name' column missing
    assert "host name" in r.json()["detail"]


async def test_bulk_csv_import_friendly_headers(client):
    # Export headers ("host name"/"bmc address") must import round-trip.
    csv = "host name,bmc address,username,password\ngpu-a,10.0.0.21,admin,pw\n"
    files = {"file": ("targets.csv", csv, "text/csv")}
    r = await client.post("/targets/api/import", files=files)
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1
    listing = (await client.get("/targets/api")).json()
    assert any(t["host"] == "10.0.0.21" for t in listing)


async def test_bulk_csv_import_minimal_columns_defaults(client):
    # Only the identity columns present; everything else defaults. The direct-mode
    # row still needs credentials, so it fails per-row (not a whole-file 400).
    csv = "host name,bmc address\ngpu-min,10.0.0.22\n"
    files = {"file": ("targets.csv", csv, "text/csv")}
    r = await client.post("/targets/api/import", files=files)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 0 and body["errors"] == 1


async def test_export_uses_friendly_headers_without_location(client):
    await client.post(
        "/targets/api",
        json={"name": "gpu-x", "host": "10.0.0.23", "username": "admin", "password": "pw"},
    )
    r = await client.get("/targets/api/export")
    assert r.status_code == 200
    header_line = r.text.splitlines()[0]
    assert "host name" in header_line and "bmc address" in header_line
    # Internal identity names and all location columns are gone from the export.
    assert "loc_" not in header_line
    assert ",name," not in f",{header_line},"


async def test_systems_page_lists_offline_last(client, repo):
    # Names chosen so pure alphabetical order would put the ONLINE one last;
    # the status sort must override that and surface connected systems first.
    online = await repo.create_target(
        name="zzz-online", host="10.0.0.1", username="u", password="p"
    )
    offline = await repo.create_target(
        name="aaa-offline", host="10.0.0.2", username="u", password="p"
    )
    await repo.create_target(name="mmm-neverpolled", host="10.0.0.3", username="u", password="p")
    await repo.update_poll_status(online.id, "success")
    await repo.update_poll_status(offline.id, "error", "unreachable")
    # `never` is left never-polled (no status).

    html = (await client.get("/targets")).text
    p_online = html.find("zzz-online")
    assert p_online != -1
    # Connected system appears before both the failed and the never-polled ones.
    assert p_online < html.find("aaa-offline")
    assert p_online < html.find("mmm-neverpolled")
    # "Never Polled" is no longer surfaced as a distinct status in the view.
    assert "Never Polled" not in html
