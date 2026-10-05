# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Health endpoint must report degraded when the pipeline is dead or the poll
loop has stalled (not just when the client object is alive)."""

import httpx
import src.collector_main as cm
from httpx import ASGITransport
from src.collector_main import create_health_app


def _client():
    app = create_health_app()
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


class _Exporter:
    def __init__(self, ping_ok=True, is_healthy=True, critical_ok=None):
        self._ping_ok = ping_ok
        self._is_healthy = is_healthy
        self._critical_ok = critical_ok

    async def health_check(self):
        return self._ping_ok, "ok" if self._ping_ok else "down"

    def get_health_metrics(self):
        # critical_ok drives the top-line status; is_healthy (critical AND
        # performance) only flips the `performance_degraded` hint.
        crit = self._critical_ok if self._critical_ok is not None else self._is_healthy
        return {
            "is_healthy": self._is_healthy,
            "connected": crit,
            "health_details": {
                "critical_checks": {
                    "connected": crit,
                    "writes_recent": crit,
                    "no_data_loss": True,
                    "buffer_not_full": True,
                }
            },
        }


class _Poller:
    def __init__(self, running=True, progressing=True):
        self._running = running
        self._progressing = progressing

    @property
    def is_running(self):
        return self._running

    def get_stats(self):
        return {"inflight": 0, "making_progress": self._progressing}

    def is_making_progress(self):
        return self._progressing


async def test_healthy_when_pipeline_and_poller_ok(monkeypatch):
    monkeypatch.setattr(cm, "_exporter", _Exporter(is_healthy=True))
    monkeypatch.setattr(cm, "_poller", _Poller(progressing=True))
    monkeypatch.setattr(cm, "_alert_manager", None)
    async with _client() as c:
        body = (await c.get("/health/detailed")).json()
    assert body["status"] == "healthy"


async def test_degraded_when_pipeline_dead_though_client_alive(monkeypatch):
    # Ping OK (client alive) but a CRITICAL check fails (no recent writes — the
    # 7h-stall case): critical_ok False -> top-line degraded.
    monkeypatch.setattr(
        cm, "_exporter", _Exporter(ping_ok=True, is_healthy=False, critical_ok=False)
    )
    monkeypatch.setattr(cm, "_poller", _Poller(progressing=True))
    monkeypatch.setattr(cm, "_alert_manager", None)
    async with _client() as c:
        body = (await c.get("/health/detailed")).json()
    assert body["status"] == "degraded"


async def test_healthy_but_performance_degraded_does_not_flip_status(monkeypatch):
    # All CRITICAL checks pass (data flowing, no loss) but combined is_healthy is
    # False due to performance (high latency / transient batch failures). Top-line
    # stays healthy; the performance_degraded hint is surfaced instead.
    monkeypatch.setattr(
        cm, "_exporter", _Exporter(ping_ok=True, is_healthy=False, critical_ok=True)
    )
    monkeypatch.setattr(cm, "_poller", _Poller(progressing=True))
    monkeypatch.setattr(cm, "_alert_manager", None)
    async with _client() as c:
        body = (await c.get("/health/detailed")).json()
    assert body["status"] == "healthy"
    assert body["performance_degraded"] is True


async def test_degraded_when_poller_stalled(monkeypatch):
    # Exporter healthy, poller "running" but not making progress -> degraded.
    monkeypatch.setattr(cm, "_exporter", _Exporter(is_healthy=True))
    monkeypatch.setattr(cm, "_poller", _Poller(running=True, progressing=False))
    monkeypatch.setattr(cm, "_alert_manager", None)
    async with _client() as c:
        body = (await c.get("/health/detailed")).json()
    assert body["status"] == "degraded"
