# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Route coverage for the alerts API + HTML page.

Alerts are seeded through the `repo` fixture (shared with the app) and exercised
through the authenticated `client`. The collector manager-stats upstream is
simulated with `httpx_mock`; the ASGI client uses an explicit transport and so
bypasses the mock, leaving only the in-route httpx call intercepted.
"""

from datetime import UTC, datetime

import httpx
from src.redfish.alert_subscriber import AlertEvent

STATS_URL = "http://collector:8081/alerts/manager-stats"


def _ev(
    *,
    target_id=1,
    name="gpu-a",
    bmc="10.0.0.5",
    severity="Critical",
    message="over temp",
    msg_id="Thermal.1.0",
    ts=None,
    source_id=None,
    raw=None,
):
    return AlertEvent(
        target_id=target_id,
        target_name=name,
        target_bmc=bmc,
        severity=severity,
        message=message,
        message_id=msg_id,
        event_type="Alert",
        origin_of_condition=None,
        event_timestamp=ts,
        received_at=datetime.now(UTC),
        source_id=source_id,
        raw=raw or {"Message": message, "MessageId": msg_id},
    )


async def _seed(repo, events):
    n = await repo.create_alerts_batch(events)
    return n


async def _first_alert(repo):
    rows = await repo.get_alerts(include_raw=True, limit=1)
    return rows[0]


# ---- JSON list API ----


async def test_list_alerts_api_basic_and_fields(client, repo):
    now = datetime.now(UTC)
    await _seed(
        repo,
        [
            _ev(severity="Critical", message="hot", source_id="/e/1", ts=now),
            # event_timestamp=None exercises the _iso_utc None branch.
            _ev(severity="Warning", message="warm", source_id="/e/2", ts=None),
        ],
    )
    r = await client.get("/alerts/api")
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data) == 2
    row = data[0]
    for key in (
        "id",
        "target_id",
        "target_name",
        "target_bmc",
        "severity",
        "message",
        "message_id",
        "event_type",
        "event_timestamp",
        "received_at",
        "raw_data",
    ):
        assert key in row
    # The None-timestamp row serializes event_timestamp as null.
    assert any(x["event_timestamp"] is None for x in data)
    # received_at carries an explicit UTC offset.
    assert data[0]["received_at"].endswith("+00:00")


async def test_list_alerts_api_filters(client, repo):
    now = datetime.now(UTC)
    await _seed(
        repo,
        [
            _ev(target_id=1, severity="Critical", message="c1", source_id="/e/c1", ts=now),
            _ev(target_id=2, severity="Warning", message="w1", source_id="/e/w1", ts=now),
        ],
    )
    # severity filter
    r = await client.get("/alerts/api", params={"severity": "Critical"})
    assert {x["severity"] for x in r.json()} == {"Critical"}
    # target filter
    r = await client.get("/alerts/api", params={"target_id": 2})
    assert {x["target_id"] for x in r.json()} == {2}
    # hours=0 means "all time" (since=None branch)
    r = await client.get("/alerts/api", params={"hours": 0})
    assert len(r.json()) == 2
    # pagination
    r = await client.get("/alerts/api", params={"limit": 1, "offset": 0})
    assert len(r.json()) == 1


# ---- per-alert raw / cper ----


async def test_alert_raw_endpoint(client, repo):
    await _seed(repo, [_ev(message="raw me", source_id="/e/raw")])
    alert = await _first_alert(repo)
    r = await client.get(f"/alerts/api/{alert.id}/raw")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == alert.id
    assert body["raw_data"]["Message"] == "raw me"


async def test_alert_cper_endpoint(client, repo):
    await _seed(repo, [_ev(message="cper me", source_id="/e/cper")])
    alert = await _first_alert(repo)
    await repo.set_cper_result(
        alert.id,
        status="decoded",
        refined_message="refined!",
        decoded={"section": "mem"},
    )
    r = await client.get(f"/alerts/api/{alert.id}/cper")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == alert.id
    assert body["cper_status"] == "decoded"
    assert body["refined_message"] == "refined!"
    assert body["cper_decoded"] == {"section": "mem"}


async def test_alert_cper_404(client, repo):
    r = await client.get("/alerts/api/999999/cper")
    assert r.status_code == 404


# ---- stats ----


async def test_alert_stats_api(client, repo):
    now = datetime.now(UTC)
    await _seed(
        repo,
        [
            _ev(severity="Critical", message="c", source_id="/e/sc", ts=now),
            _ev(severity="Warning", message="w", source_id="/e/sw", ts=now),
        ],
    )
    r = await client.get("/alerts/api/stats")
    assert r.status_code == 200
    stats = r.json()
    assert stats["total"] == 2
    assert stats["critical"] == 1
    assert stats["warning"] == 1


# ---- manager-stats proxy ----


async def test_manager_stats_success(client, httpx_mock):
    httpx_mock.add_response(url=STATS_URL, json={"enabled": True, "subscribers": []})
    r = await client.get("/alerts/api/manager-stats")
    assert r.status_code == 200
    assert r.json()["enabled"] is True


async def test_manager_stats_unreachable_returns_disabled(client, httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("no route"), url=STATS_URL)
    r = await client.get("/alerts/api/manager-stats")
    assert r.status_code == 200
    assert r.json() == {"enabled": False}


async def test_manager_stats_non_200_returns_disabled(client, httpx_mock):
    httpx_mock.add_response(url=STATS_URL, status_code=503)
    r = await client.get("/alerts/api/manager-stats")
    assert r.json() == {"enabled": False}


# ---- subscription-status ----


async def test_subscription_status_disabled_when_collector_down(client, httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("no route"), url=STATS_URL)
    r = await client.get("/alerts/api/subscription-status")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["subscriptions"] == []
    assert body["summary"]["total_targets"] == 0


async def test_subscription_status_manager_not_enabled(client, httpx_mock):
    httpx_mock.add_response(url=STATS_URL, json={"enabled": False})
    r = await client.get("/alerts/api/subscription-status")
    assert r.json()["enabled"] is False


async def test_subscription_status_enabled_with_states(client, repo, httpx_mock):
    # Three subscribed targets: connected, reconnecting, and one with no
    # subscriber entry (not_subscribed) to hit all three summary buckets.
    ta = await repo.create_target(name="gpu-a", host="10.0.0.5", username="u", password="p")
    tb = await repo.create_target(name="gpu-b", host="10.0.0.6", username="u", password="p")
    tc = await repo.create_target(name="gpu-c", host="10.0.0.7", username="u", password="p")
    td = await repo.create_target(name="gpu-d", host="10.0.0.8", username="u", password="p")

    now = datetime.now(UTC)
    await _seed(
        repo,
        [
            _ev(target_id=ta.id, severity="Critical", message="c", source_id="/e/a1", ts=now),
            _ev(target_id=ta.id, severity="Warning", message="w", source_id="/e/a2", ts=now),
            _ev(target_id=ta.id, severity="OK", message="ok", source_id="/e/a3", ts=now),
        ],
    )

    httpx_mock.add_response(
        url=STATS_URL,
        json={
            "enabled": True,
            "subscribers": [
                {
                    "target_id": ta.id,
                    "state": "connected",
                    "consecutive_failures": 0,
                    "last_event_time": "2026-10-01T00:00:00Z",
                },
                {
                    "target_id": tb.id,
                    "state": "reconnecting",
                    "consecutive_failures": 3,
                    "failure_reason": "network",
                },
                {
                    # A subscribed target in a non-active, non-reconnecting state
                    # (e.g. manually stopped) lands in the "failed" bucket.
                    "target_id": td.id,
                    "state": "stopped",
                    "consecutive_failures": 0,
                },
            ],
            "cper_backlog": {"pending": 1},
            "cper_decoded": 5,
            "cper_failed": 0,
        },
    )

    r = await client.get("/alerts/api/subscription-status")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    summary = body["summary"]
    assert summary["total_targets"] == 4
    assert summary["active"] == 1  # ta connected
    assert summary["disconnected"] == 1  # tb reconnecting
    assert summary["failed"] == 2  # tc not subscribed + td stopped

    by_id = {s["target_id"]: s for s in body["subscriptions"]}
    assert by_id[ta.id]["status"] == "connected"
    assert by_id[ta.id]["alerts_24h"] == 3
    assert by_id[ta.id]["critical_count"] == 1
    assert by_id[tc.id]["status"] == "not_subscribed"
    assert by_id[td.id]["status"] == "stopped"
    assert body["cper"]["decoded"] == 5


# ---- HTML page ----


async def test_alerts_page_renders(client, repo):
    now = datetime.now(UTC)
    await _seed(
        repo,
        [
            _ev(severity="Critical", message="over temp", source_id="/e/p1", ts=now),
            _ev(severity="Warning", message="fan slow", source_id="/e/p2", ts=now),
        ],
    )
    r = await client.get("/alerts")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Critical" in r.text
    assert "Warning" in r.text


async def test_alerts_page_severity_filter_and_search(client, repo):
    now = datetime.now(UTC)
    await _seed(
        repo,
        [
            _ev(severity="Critical", message="over temp", source_id="/e/s1", ts=now),
            _ev(severity="Warning", message="fan slow", source_id="/e/s2", ts=now),
        ],
    )
    # severity=Critical selects the single-severity total branch.
    r = await client.get(
        "/alerts",
        params={"severity": "Critical", "q": "temp", "target_id": "1", "hours": 24, "page": 1},
    )
    assert r.status_code == 200
    assert "over temp" in r.text


async def test_alerts_page_invalid_target_id_coerced(client, repo):
    # A non-numeric target_id must not 422 — it coerces to None.
    r = await client.get("/alerts", params={"target_id": "not-a-number", "hours": 0})
    assert r.status_code == 200


async def test_alerts_page_empty_state(client, repo):
    r = await client.get("/alerts")
    assert r.status_code == 200
    assert "No Critical or Warning alerts" in r.text
