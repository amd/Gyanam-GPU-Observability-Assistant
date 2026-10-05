# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Extra coverage for api_main: the _format_time_ago helper, the exception
handlers (HTTP/validation/unhandled, API vs browser branches) and the
application lifespan (startup wiring + retention loop + shutdown)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import src.api_main as api_main
from src.api_main import _format_time_ago


# --------------------------------------------------------------------------- #
# _format_time_ago
# --------------------------------------------------------------------------- #
def test_format_time_ago_none():
    assert _format_time_ago(None) == "never"


def test_format_time_ago_future_is_just_now():
    future = datetime.now(UTC) + timedelta(minutes=5)
    assert _format_time_ago(future) == "just now"


def test_format_time_ago_seconds():
    assert _format_time_ago(datetime.now(UTC) - timedelta(seconds=30)) == "just now"


def test_format_time_ago_minutes_plural():
    assert _format_time_ago(datetime.now(UTC) - timedelta(minutes=5)) == "5 minutes ago"


def test_format_time_ago_minute_singular():
    assert _format_time_ago(datetime.now(UTC) - timedelta(minutes=1)) == "1 minute ago"


def test_format_time_ago_hours():
    assert _format_time_ago(datetime.now(UTC) - timedelta(hours=2)) == "2 hours ago"


def test_format_time_ago_days():
    assert _format_time_ago(datetime.now(UTC) - timedelta(days=3)) == "3 days ago"


def test_format_time_ago_naive_old_shows_date():
    # A naive datetime older than ~a month is treated as UTC and rendered as a
    # bare date string (covers the naive->UTC branch and the absolute fallback).
    old = datetime(2000, 1, 2)  # naive
    assert _format_time_ago(old) == "2000-01-02"


# --------------------------------------------------------------------------- #
# Exception handlers — StarletteHTTPException (404) browser vs API
# --------------------------------------------------------------------------- #
async def test_http_exception_browser_renders_error_page(noauth_client):
    r = await noauth_client.get("/does-not-exist", headers={"accept": "text/html"})
    assert r.status_code == 404
    assert "text/html" in r.headers.get("content-type", "")


async def test_http_exception_api_returns_json(noauth_client):
    r = await noauth_client.get("/does-not-exist", headers={"accept": "application/json"})
    assert r.status_code == 404
    assert "detail" in r.json()


# --------------------------------------------------------------------------- #
# Exception handlers — RequestValidationError (422) browser vs API
# --------------------------------------------------------------------------- #
async def test_validation_error_api_returns_json(noauth_client):
    # POST /login with required form fields missing -> 422.
    r = await noauth_client.post("/login", headers={"accept": "application/json"}, data={})
    assert r.status_code == 422
    assert "detail" in r.json()


async def test_validation_error_browser_renders_page(noauth_client):
    r = await noauth_client.post("/login", headers={"accept": "text/html"}, data={})
    assert r.status_code == 422
    assert "text/html" in r.headers.get("content-type", "")


# --------------------------------------------------------------------------- #
# Exception handlers — unhandled Exception (500) browser vs API
# --------------------------------------------------------------------------- #
def _client_no_raise(app):
    # ServerErrorMiddleware re-raises after the Exception handler builds the 500
    # response, so the transport must be told not to propagate app exceptions.
    import httpx
    from httpx import ASGITransport

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_unhandled_exception_api_returns_json(app):
    async def _boom():
        raise RuntimeError("kaboom")

    app.add_api_route("/_boom", _boom, methods=["GET"])
    async with _client_no_raise(app) as c:
        r = await c.get("/_boom", headers={"accept": "application/json"})
    assert r.status_code == 500
    assert r.json()["detail"] == "Internal server error"


async def test_unhandled_exception_browser_renders_page(app):
    async def _boom2():
        raise RuntimeError("kaboom2")

    app.add_api_route("/_boom2", _boom2, methods=["GET"])
    async with _client_no_raise(app) as c:
        r = await c.get("/_boom2", headers={"accept": "text/html"})
    assert r.status_code == 500
    assert "text/html" in r.headers.get("content-type", "")


# --------------------------------------------------------------------------- #
# lifespan — startup wiring, retention loop, shutdown
# --------------------------------------------------------------------------- #
class _FakeRepo:
    def __init__(self, *a, **k):
        self.closed = False
        self.deleted_calls = 0

    async def init_db(self):
        return None

    async def close(self):
        self.closed = True

    async def delete_expired_logs(self, days):
        self.deleted_calls += 1
        return [SimpleNamespace(file_path="/tmp/expired.log")]


class _FakeSchemaLoader:
    def __init__(self, path, raise_on_load=False):
        self.path = path
        self._raise = raise_on_load

    def load(self):
        if self._raise:
            raise ValueError("bad schema")


class _FakeLogCollector:
    def __init__(self, *a, **k):
        self.deleted = []

    def delete_file(self, path):
        self.deleted.append(path)


def _fake_config(retention_days=0, cleanup_interval_hours=0):
    return SimpleNamespace(
        collected_logs=SimpleNamespace(
            storage_dir="/tmp/logs",
            max_concurrent_collections=2,
            task_timeout=10,
            download_timeout=10,
            retention_days=retention_days,
            cleanup_interval_hours=cleanup_interval_hours,
        ),
        polling=SimpleNamespace(timeout_seconds=10, task_poll_interval=1),
        redfish=SimpleNamespace(collect_endpoint="/redfish", collect_body={}),
    )


def _patch_lifespan_deps(monkeypatch, config, schema_raises=False):
    monkeypatch.setattr(api_main, "get_config", lambda: config)
    monkeypatch.setattr(api_main, "TargetRepository", _FakeRepo)
    monkeypatch.setattr(
        api_main, "SchemaLoader", lambda path: _FakeSchemaLoader(path, raise_on_load=schema_raises)
    )
    monkeypatch.setattr(api_main, "LogCollector", _FakeLogCollector)


async def test_lifespan_with_retention_runs_and_shuts_down(monkeypatch):
    from src.api import dependencies

    cfg = _fake_config(retention_days=5, cleanup_interval_hours=0)
    _patch_lifespan_deps(monkeypatch, cfg)

    dependencies.app_state.clear()
    dummy_app = SimpleNamespace(state=SimpleNamespace())
    import asyncio

    try:
        async with api_main.lifespan(dummy_app):
            assert "repository" in dependencies.app_state
            assert "retention_task" in dependencies.app_state
            # Let the (interval=0) retention loop run a few iterations so the
            # delete/cleanup body is exercised.
            await asyncio.sleep(0.05)
        repo = dependencies.app_state.get("repository")
    finally:
        task = dependencies.app_state.get("retention_task")
        dependencies.app_state.clear()
    # Shutdown closed the repo and the retention task is finished.
    assert repo.closed is True
    assert task.done()


async def test_lifespan_without_retention(monkeypatch):
    from src.api import dependencies

    cfg = _fake_config(retention_days=0)
    _patch_lifespan_deps(monkeypatch, cfg)

    dependencies.app_state.clear()
    dummy_app = SimpleNamespace(state=SimpleNamespace())
    try:
        async with api_main.lifespan(dummy_app):
            assert "retention_task" not in dependencies.app_state
            repo = dependencies.app_state["repository"]
    finally:
        dependencies.app_state.clear()
    assert repo.closed is True


class _RetentionErrorRepo(_FakeRepo):
    async def delete_expired_logs(self, days):
        self.deleted_calls += 1
        raise RuntimeError("db gone")


async def test_lifespan_retention_loop_handles_error(monkeypatch):
    """An exception in the retention loop is caught (loop keeps running)."""
    from src.api import dependencies

    cfg = _fake_config(retention_days=5, cleanup_interval_hours=0)
    monkeypatch.setattr(api_main, "get_config", lambda: cfg)
    monkeypatch.setattr(api_main, "TargetRepository", _RetentionErrorRepo)
    monkeypatch.setattr(api_main, "SchemaLoader", lambda path: _FakeSchemaLoader(path))
    monkeypatch.setattr(api_main, "LogCollector", _FakeLogCollector)

    import asyncio

    dependencies.app_state.clear()
    dummy_app = SimpleNamespace(state=SimpleNamespace())
    try:
        async with api_main.lifespan(dummy_app):
            repo = dependencies.app_state["repository"]
            await asyncio.sleep(0.05)  # let the loop hit the error branch
    finally:
        dependencies.app_state.clear()
    assert repo.deleted_calls >= 1


def test_run_entrypoint(monkeypatch):
    """run() configures logging, validates settings and hands off to uvicorn."""
    import logging

    cfg = SimpleNamespace(
        logging=SimpleNamespace(level="INFO", format="%(message)s"),
        ui=SimpleNamespace(host="127.0.0.1", port=8080),
    )
    settings = SimpleNamespace(encryption_key="k")
    sentinel_app = object()
    recorded = {}

    def _fake_uvicorn_run(app, **kw):
        recorded["app"] = app
        recorded["kw"] = kw

    monkeypatch.setattr(api_main, "get_config", lambda: cfg)
    monkeypatch.setattr(api_main, "get_settings", lambda: settings)
    monkeypatch.setattr(api_main, "create_app", lambda: sentinel_app)
    monkeypatch.setattr(api_main.uvicorn, "run", _fake_uvicorn_run)

    # run() reconfigures the root logger; snapshot and restore it afterwards so
    # it can't disturb the rest of the suite.
    root = logging.getLogger()
    saved = (root.level, root.handlers[:])
    try:
        api_main.run()
    finally:
        root.setLevel(saved[0])
        root.handlers[:] = saved[1]

    assert recorded["app"] is sentinel_app
    assert recorded["kw"]["host"] == "127.0.0.1"
    assert recorded["kw"]["port"] == 8080


def test_run_entrypoint_missing_encryption_key_exits(monkeypatch):
    cfg = SimpleNamespace(
        logging=SimpleNamespace(level="INFO", format="%(message)s"),
        ui=SimpleNamespace(host="127.0.0.1", port=8080),
    )
    settings = SimpleNamespace(encryption_key="")
    monkeypatch.setattr(api_main, "get_config", lambda: cfg)
    monkeypatch.setattr(api_main, "get_settings", lambda: settings)

    import logging

    root = logging.getLogger()
    saved = (root.level, root.handlers[:])
    try:
        with pytest.raises(SystemExit):
            api_main.run()
    finally:
        root.setLevel(saved[0])
        root.handlers[:] = saved[1]


async def test_lifespan_schema_load_failure_is_tolerated(monkeypatch):
    from src.api import dependencies

    cfg = _fake_config(retention_days=0)
    _patch_lifespan_deps(monkeypatch, cfg, schema_raises=True)

    dependencies.app_state.clear()
    dummy_app = SimpleNamespace(state=SimpleNamespace())
    try:
        async with api_main.lifespan(dummy_app):
            # Startup still completes despite the schema ValueError.
            assert "schema_loader" in dependencies.app_state
    finally:
        dependencies.app_state.clear()
