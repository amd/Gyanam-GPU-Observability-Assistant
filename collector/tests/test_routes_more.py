# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Additional route tests: target get/update/delete/export, logs, schemas, alerts clear."""


async def _create_target(client, host="10.0.0.9"):
    body = {"name": "gpu1", "host": host, "username": "admin", "password": "pw"}
    r = await client.post("/targets/api", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_target_get_update_delete(client):
    tid = await _create_target(client)
    # get
    r = await client.get(f"/targets/api/{tid}")
    assert r.status_code == 200 and r.json()["host"] == "10.0.0.9"
    # update
    r = await client.put(f"/targets/api/{tid}", json={"name": "renamed"})
    assert r.status_code == 200
    assert (await client.get(f"/targets/api/{tid}")).json()["name"] == "renamed"
    # delete
    assert (await client.delete(f"/targets/api/{tid}")).status_code == 200
    assert (await client.get(f"/targets/api/{tid}")).status_code == 404


async def test_target_export_csv(client):
    await _create_target(client, host="10.0.0.21")
    r = await client.get("/targets/api/export")
    assert r.status_code == 200
    assert "10.0.0.21" in r.text
    assert "name" in r.text.splitlines()[0]  # header row


async def test_logs_list_and_delete(client, repo):
    log = await repo.create_collected_log(
        target_id=1,
        target_name="n",
        target_host="10.0.0.1",
        filename="b.gz",
        file_path="/data/b.gz",
        status="completed",
    )
    listing = await client.get("/logs/api")
    assert listing.status_code == 200
    assert any(item["filename"] == "b.gz" for item in listing.json())
    r = await client.delete(f"/logs/api/{log.id}")
    assert r.status_code == 200


async def test_schemas_list(client):
    r = await client.get("/schemas/api")
    assert r.status_code == 200
    assert len(r.json()) >= 30


async def test_schemas_auto_discovery(client):
    r = await client.get("/schemas/api/auto-discovery")
    assert r.status_code == 200


async def test_alert_deletion_routes_removed(client):
    # Alert delete/clear actions are intentionally unsupported.
    assert (await client.request("DELETE", "/alerts/api/clear")).status_code == 404
