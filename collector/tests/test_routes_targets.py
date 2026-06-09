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
    files = {"file": ("t.csv", "host,username\n10.0.0.5,admin\n", "text/csv")}
    r = await client.post("/targets/api/import", files=files)
    assert r.status_code == 400  # 'name' column missing
