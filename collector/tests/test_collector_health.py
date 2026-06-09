# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the collector's internal health app + webhook receiver."""

from fastapi.testclient import TestClient
from src import collector_main


def test_basic_health():
    with TestClient(collector_main.create_health_app()) as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


def test_detailed_health_with_no_components(monkeypatch):
    monkeypatch.setattr(collector_main, "_alert_manager", None)
    monkeypatch.setattr(collector_main, "_exporter", None)
    monkeypatch.setattr(collector_main, "_poller", None)
    monkeypatch.setattr(collector_main, "_sse_manager", None)
    with TestClient(collector_main.create_health_app()) as c:
        r = c.get("/health/detailed")
    assert r.status_code == 200
    assert "components" in r.json()


def test_manager_stats_disabled(monkeypatch):
    monkeypatch.setattr(collector_main, "_alert_manager", None)
    with TestClient(collector_main.create_health_app()) as c:
        r = c.get("/alerts/manager-stats")
    assert r.json()["enabled"] is False


def test_webhook_receiver_forwards_event(monkeypatch):
    class StubMgr:
        def __init__(self):
            self.calls = []

        async def process_webhook_event(self, tid, data):
            self.calls.append((tid, data))

    stub = StubMgr()
    monkeypatch.setattr(collector_main, "_alert_manager", stub)
    with TestClient(collector_main.create_health_app()) as c:
        r = c.post("/redfish-webhook/5", json={"Events": [{"MessageId": "T.1"}]})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert stub.calls and stub.calls[0][0] == 5


def test_webhook_receiver_no_manager(monkeypatch):
    monkeypatch.setattr(collector_main, "_alert_manager", None)
    with TestClient(collector_main.create_health_app()) as c:
        r = c.post("/redfish-webhook/5", json={"Events": []})
    assert r.status_code == 200
    assert r.json()["status"] == "error"
