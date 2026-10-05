# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Live smoke tests against a RUNNING docker-compose stack.

These are NOT run by the default (mock) suite — ``scripts/run-tests.sh``
excludes ``tests/live``. Run them with ``./scripts/smoke-test.sh`` after
``./gyanam.sh start``. They exercise what unit mocks can't: that the images
actually boot and import, that every page/route renders without a 500, that the
two services can talk to each other, and that the real SQLite/Postgres/InfluxDB
connections are up.

Config via env:
  GYANAM_API_URL        default http://api:8080         (from inside the compose net)
  GYANAM_COLLECTOR_URL  default http://collector:8081
  GYANAM_SMOKE_USER     default admin
  GYANAM_SMOKE_PASS     default changeme
  GYANAM_LIVE_MUTATE=1  also run the create/delete round-trip (writes to the DB;
                        off by default so the suite is safe against a live fleet)
"""

import os
import re

import httpx
import pytest

API = os.environ.get("GYANAM_API_URL", "http://api:8080").rstrip("/")
COLLECTOR = os.environ.get("GYANAM_COLLECTOR_URL", "http://collector:8081").rstrip("/")
USER = os.environ.get("GYANAM_SMOKE_USER", "admin")
PASS = os.environ.get("GYANAM_SMOKE_PASS", "changeme")

pytestmark = pytest.mark.live

# Every top-level UI page — a render here catches template/route regressions
# (Jinja errors, broken context) that only surface at request time.
PAGES = ["/", "/systems", "/datahall", "/logs", "/alerts", "/schemas", "/status"]


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert m, "no CSRF token on the login page"
    return m.group(1)


@pytest.fixture(scope="module")
def session() -> httpx.Client:
    """An authenticated client against the live API."""
    c = httpx.Client(base_url=API, timeout=30.0, follow_redirects=False)
    login_page = c.get("/login")
    assert login_page.status_code == 200, "API /login not reachable"
    r = c.post(
        "/login",
        data={"username": USER, "password": PASS, "csrf_token": _csrf(login_page.text)},
    )
    assert r.status_code == 303, (
        f"login failed ({r.status_code}); set GYANAM_SMOKE_USER/PASS if the "
        "deployment uses non-default credentials"
    )
    yield c
    c.close()


# ---- liveness / connectivity ----------------------------------------------


def test_api_health_ok():
    r = httpx.get(f"{API}/health", timeout=15.0)
    assert r.status_code == 200


def test_collector_health_ok():
    r = httpx.get(f"{COLLECTOR}/health", timeout=15.0)
    assert r.status_code == 200


def test_collector_detailed_health_shape():
    # /health/detailed does live InfluxDB + alert-store pings, which can be slow
    # when InfluxDB is under write load — allow a generous timeout.
    r = httpx.get(f"{COLLECTOR}/health/detailed", timeout=60.0)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("healthy", "degraded")
    comps = body.get("components", {})
    # The real pipeline components must be present (catches a wiring regression).
    for key in ("exporter", "influxdb_export", "poller", "alert_manager"):
        assert key in comps, f"missing health component: {key}"
    # Critical integrity: the exporter must be connected to InfluxDB.
    assert comps["influxdb_export"].get("connected") is True


# ---- every page renders (no 500) ------------------------------------------


@pytest.mark.parametrize("path", PAGES)
def test_page_renders(session, path):
    r = session.get(path, follow_redirects=True)
    assert r.status_code == 200, f"{path} returned {r.status_code}"
    assert "<html" in r.text.lower() or "<!doctype" in r.text.lower()


# ---- cross-service + real-DB read paths (safe/read-only) ------------------


def test_systems_api_lists(session):
    r = session.get("/systems/api")
    assert r.status_code == 200
    assert isinstance(r.json(), list)  # real SQLite read


def test_csv_export_has_friendly_headers(session):
    r = session.get("/systems/api/export")
    assert r.status_code == 200
    header = r.text.splitlines()[0]
    assert "host name" in header and "bmc address" in header
    assert "loc_" not in header  # location columns intentionally excluded


def test_datahall_layout_json(session):
    r = session.get("/datahall/api/layout")
    assert r.status_code == 200
    body = r.json()
    for key in ("racks", "unplaced", "halls", "rack_height_u"):
        assert key in body


def test_diagnostics_proxies_collector(session):
    # The API /status (Diagnostics) page proxies the collector's health — a
    # round-trip over the internal docker network.
    r = session.get("/status", follow_redirects=True)
    assert r.status_code == 200


# ---- optional mutating round-trip (gated; writes to the DB) ---------------


@pytest.mark.skipif(
    os.environ.get("GYANAM_LIVE_MUTATE") != "1",
    reason="mutating round-trip disabled (set GYANAM_LIVE_MUTATE=1 on a throwaway stack)",
)
def test_target_create_list_delete_roundtrip(session):
    body = {
        "name": "smoke-test-node",
        "host": "10.255.255.254",  # synthetic, non-routable
        "username": "smoke",
        "password": "smoke",
    }
    created = session.post("/systems/api", json=body)
    assert created.status_code == 200, created.text
    tid = created.json().get("id") or created.json().get("target", {}).get("id")
    try:
        listing = session.get("/systems/api").json()
        assert any(t["host"] == "10.255.255.254" for t in listing)
    finally:
        if tid:
            session.delete(f"/systems/api/{tid}")
    assert not any(t["host"] == "10.255.255.254" for t in session.get("/systems/api").json())
