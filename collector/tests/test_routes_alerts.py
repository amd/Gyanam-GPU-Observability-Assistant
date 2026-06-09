# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Route tests for the alerts endpoints (repo-backed via SQLite)."""

from datetime import UTC, datetime

from src.redfish.alert_subscriber import AlertEvent


def _alert(msg="over temp", sev="Critical", source="/e/1"):
    return AlertEvent(
        target_id=1,
        target_name="n1",
        target_bmc="10.0.0.1",
        severity=sev,
        message=msg,
        message_id="T.1",
        event_type="Alert",
        origin_of_condition=None,
        event_timestamp=datetime.now(UTC),
        received_at=datetime.now(UTC),
        source_id=source,
        raw={"Message": msg, "MessageArgs": ["x"]},
    )


async def _seed(repo, n=3):
    await repo.create_alerts_batch([_alert(msg=f"m{i}", source=f"/e/{i}") for i in range(n)])


async def test_alerts_page_renders(client, repo):
    await _seed(repo)
    r = await client.get("/alerts?hours=0")
    assert r.status_code == 200
    assert "Recent Critical and Warning Alerts" in r.text


async def test_alerts_page_empty_target_id_ok(client, repo):
    # The filter form submits target_id="" for "All Targets" — must not 422.
    await _seed(repo, 1)
    r = await client.get("/alerts?hours=0&severity=&target_id=&q=")
    assert r.status_code == 200


async def test_alerts_page_specific_target_id(client, repo):
    await _seed(repo, 2)
    r = await client.get("/alerts?hours=0&target_id=1")
    assert r.status_code == 200


async def test_alerts_api_list(client, repo):
    await _seed(repo, 2)
    r = await client.get("/alerts/api?hours=0")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    assert data[0]["raw_data"] is not None


async def test_alerts_api_stats(client, repo):
    await _seed(repo)
    r = await client.get("/alerts/api/stats")
    assert r.status_code == 200
    assert r.json()["total"] == 3


async def test_alert_raw_endpoint(client, repo):
    await _seed(repo, 1)
    aid = (await repo.get_alerts(limit=1))[0].id
    r = await client.get(f"/alerts/api/{aid}/raw")
    assert r.status_code == 200
    assert r.json()["raw_data"]["Message"] == "m0"


async def test_alert_raw_404(client, repo):
    r = await client.get("/alerts/api/999999/raw")
    assert r.status_code == 404


async def test_alert_delete_routes_removed(client, repo):
    # Manual alert deletion is intentionally unsupported now.
    await _seed(repo, 1)
    aid = (await repo.get_alerts(limit=1))[0].id
    assert (await client.post(f"/alerts/{aid}/delete", data={})).status_code == 404
    assert (await client.request("DELETE", f"/alerts/api/{aid}")).status_code == 404
    assert (await client.request("DELETE", "/alerts/api/clear")).status_code == 404
