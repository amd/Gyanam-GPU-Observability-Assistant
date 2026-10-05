# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the collector's health/webhook FastAPI app (uninitialized state)."""

from datetime import UTC, datetime

import httpx
import src.collector_main as cm
from httpx import ASGITransport
from src.collector_main import create_health_app
from src.redfish.poller import PollResult


def _client():
    app = create_health_app()
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _pollresult(**kw):
    base = {
        "target_id": 1,
        "target_name": "n",
        "target_host": "h",
        "success": True,
        "content": b"",
        "content_type": "",
        "error_message": None,
        "poll_time": datetime.now(UTC),
        "duration_ms": 5.0,
        "data": [("comprehensive", {"MetricValues": []})],
        "collection_method": "get",
    }
    base.update(kw)
    return PollResult(**base)


class _FakePoller:
    def __init__(self, result):
        self._result = result

    @property
    def is_running(self):
        return True

    def get_stats(self):
        return {"inflight": 0}

    async def poll_single(self, target_id):
        return self._result


class _FakeExporter:
    async def health_check(self):
        return True, "ok"

    def get_health_metrics(self):
        return {"connected": True}


class _FakeAlertManager:
    enabled = True

    def get_stats(self):
        return {"subscribers": []}

    async def process_webhook_event(self, tid, data):
        return 3


async def test_basic_health():
    async with _client() as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "healthy", "service": "collector"}


async def test_detailed_health_uninitialized_is_degraded():
    async with _client() as c:
        r = await c.get("/health/detailed")
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "degraded"  # nothing wired up in this bare app
    assert body["components"]["poller"]["status"] == "not_initialized"
    assert body["components"]["sse_manager"]["status"] == "not_initialized"


async def test_alert_manager_stats_disabled():
    async with _client() as c:
        r = await c.get("/alerts/manager-stats")
    assert r.json()["enabled"] is False


async def test_trigger_poll_without_poller_503():
    async with _client() as c:
        r = await c.post("/poll/1")
    assert r.status_code == 503
    assert r.json()["success"] is False


async def test_trigger_poll_requires_internal_token(monkeypatch):
    # When an internal token is configured, /poll must reject callers that don't
    # present it (defense-in-depth on the internal control server).
    monkeypatch.setattr(cm, "internal_service_token", lambda: "s3cret")
    async with _client() as c:
        missing = await c.post("/poll/1")
        wrong = await c.post("/poll/1", headers={cm.INTERNAL_AUTH_HEADER: "nope"})
    assert missing.status_code == 403
    assert wrong.status_code == 403


async def test_trigger_poll_accepts_valid_internal_token(monkeypatch):
    # Correct token passes the gate (then 503 because no poller is wired).
    monkeypatch.setattr(cm, "internal_service_token", lambda: "s3cret")
    async with _client() as c:
        r = await c.post("/poll/1", headers={cm.INTERNAL_AUTH_HEADER: "s3cret"})
    assert r.status_code == 503


# ---- with module globals wired to fakes ----


async def test_detailed_health_with_components(monkeypatch):
    monkeypatch.setattr(cm, "_exporter", _FakeExporter())
    monkeypatch.setattr(cm, "_poller", _FakePoller(None))
    async with _client() as c:
        r = await c.get("/health/detailed")
    body = r.json()
    assert body["components"]["exporter"]["healthy"] is True
    assert body["components"]["poller"]["status"] == "running"


async def test_alert_manager_stats_enabled(monkeypatch):
    monkeypatch.setattr(cm, "_alert_manager", _FakeAlertManager())
    async with _client() as c:
        r = await c.get("/alerts/manager-stats")
    assert r.json()["enabled"] is True


async def test_trigger_poll_target_not_found(monkeypatch):
    monkeypatch.setattr(cm, "_poller", _FakePoller(None))
    async with _client() as c:
        r = await c.post("/poll/5")
    assert r.status_code == 404


async def test_trigger_poll_success(monkeypatch):
    monkeypatch.setattr(cm, "_poller", _FakePoller(_pollresult()))
    async with _client() as c:
        r = await c.post("/poll/5")
    body = r.json()
    assert r.status_code == 200
    assert body["success"] is True and body["collection_method"] == "get"


async def test_webhook_no_alert_manager():
    async with _client() as c:
        r = await c.post("/redfish-webhook/1", json={"Events": []})
    assert r.json()["status"] == "error"


async def test_webhook_processes_events(monkeypatch):
    monkeypatch.setattr(cm, "_alert_manager", _FakeAlertManager())
    async with _client() as c:
        r = await c.post("/redfish-webhook/1", json={"Events": [{}]})
    assert r.json() == {"status": "ok", "events_received": 3}


async def test_webhook_invalid_payload(monkeypatch):
    monkeypatch.setattr(cm, "_alert_manager", _FakeAlertManager())
    async with _client() as c:
        r = await c.post(
            "/redfish-webhook/1",
            content=b'"not-a-dict"',
            headers={"content-type": "application/json"},
        )
    assert r.json()["message"] == "invalid payload"
