# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Extra route coverage for health.py: the detailed health proxy endpoint that
queries the collector service (mocked with httpx_mock), the /status HTML page,
and the /ready probe (success + failure).
"""

import httpx
from src.api import dependencies
from src.api.routes.health import COLLECTOR_HEALTH_URL

# ---------------------------------------------------------------------------
# /health/detailed — collector proxy branches
# ---------------------------------------------------------------------------


async def test_detailed_health_requires_auth(noauth_client):
    # The detailed view exposes the subscriber roster + shard topology, so it must
    # NOT be reachable unauthenticated (the bare /health stays open for Docker).
    r = await noauth_client.get("/health/detailed")
    assert r.status_code == 401


async def test_detailed_health_collector_healthy(client, httpx_mock, repo):
    # A log collector with a real active_tasks list exercises the happy path of
    # the log-collector component check.
    lc = dependencies.app_state["log_collector"]
    lc.active_tasks = []

    httpx_mock.add_response(
        method="GET",
        url=COLLECTOR_HEALTH_URL,
        status_code=200,
        json={"status": "healthy", "components": {"poller": {"healthy": True}}},
    )
    r = await client.get("/health/detailed")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert body["collector_service"]["status"] == "healthy"


async def test_detailed_health_collector_unreachable(client, httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("no route"), url=COLLECTOR_HEALTH_URL)
    r = await client.get("/health/detailed")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["collector_service"]["status"] == "unavailable"


async def test_detailed_health_collector_bad_json(client, httpx_mock):
    # 200 but an undecodable body -> generic exception branch.
    httpx_mock.add_response(
        method="GET",
        url=COLLECTOR_HEALTH_URL,
        status_code=200,
        content=b"not-json",
        headers={"Content-Type": "application/json"},
    )
    r = await client.get("/health/detailed")
    assert r.status_code == 200
    assert r.json()["collector_service"]["status"] == "unavailable"


async def test_detailed_health_db_failure(client, httpx_mock, repo, monkeypatch):
    # Force the API-side database check to fail.
    async def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "get_all_targets", _boom)

    httpx_mock.add_exception(httpx.ConnectError("x"), url=COLLECTOR_HEALTH_URL)
    r = await client.get("/health/detailed")
    assert r.status_code == 200
    body = r.json()
    db = body["api_service"]["components"]["database"]
    assert db["healthy"] is False


# ---------------------------------------------------------------------------
# /status HTML page
# ---------------------------------------------------------------------------


async def test_status_page_renders(client, httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url=COLLECTOR_HEALTH_URL,
        status_code=200,
        json={"status": "healthy", "components": {}},
    )
    r = await client.get("/status")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# /ready probe
# ---------------------------------------------------------------------------


async def test_ready_success(client):
    r = await client.get("/ready")
    assert r.status_code == 200
    assert r.json()["ready"] is True


async def test_ready_failure(client, repo, monkeypatch):
    async def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "get_all_targets", _boom)
    r = await client.get("/ready")
    assert r.status_code == 503
    assert r.json()["ready"] is False
