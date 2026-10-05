# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Integration-style coverage for the collector service lifecycle:

* ``run_collector()`` — full startup -> shutdown with every external component
  faked (no sockets, no DB, no uvicorn), plus the DB-not-ready retry branch, the
  InfluxDB connect-error / schema-load-error degraded branches, the alerts- and
  inventory-disabled branches, the signal.signal fallback path, and the
  "shutdown requested during startup" early return.
* ``run()`` — the process entry point: missing ENCRYPTION_KEY and missing
  ALERTS_DATABASE_URL both SystemExit; the happy path (asyncio.run stubbed);
  and the KeyboardInterrupt / crash handling.
* ``run_health_server`` / ``_NoSignalServer`` / ``_shutdown_collector`` queue
  drain — the remaining small branches.

Everything uses synthetic hosts/URLs only. No real network or disk I/O.
"""

import asyncio
import logging
import signal
from contextlib import contextmanager

import pytest
import src.collector_main as cm
from src.config import AppConfig, Settings


# --------------------------------------------------------------------------- #
# Shared fakes
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _fast_processor_drain(monkeypatch):
    """These lifecycle tests mock the result processor as a never-ending task,
    so shrink the shutdown drain budget to keep the backstop-cancel instant."""
    monkeypatch.setattr(cm, "_PROCESSOR_DRAIN_TIMEOUT_S", 0.05)


async def _forever(*args, **kwargs):
    """A coroutine that blocks until cancelled (immune to sleep/Event patching)."""
    await asyncio.get_running_loop().create_future()


def _make_fakes(
    rec, *, connect_error=False, schema_error=False, db_fail_once=False, db_always_fail=False
):
    """Build the set of fake component classes/callables that run_collector uses.

    ``rec`` is a list that records lifecycle calls so tests can assert ordering.
    """

    class _Repo:
        def __init__(self, **kw):
            self._calls = 0

        async def init_db(self):
            self._calls += 1
            if db_always_fail:
                raise RuntimeError("database never ready")
            if db_fail_once and self._calls == 1:
                raise RuntimeError("database not ready yet")
            rec.append("repo.init_db")

        async def close(self):
            rec.append("repo.close")

    class _Schema:
        def __init__(self, path):
            self.path = path

        def load(self):
            if schema_error:
                raise ValueError("unparseable schema")
            rec.append("schema.load")

        def get_schemas(self):
            return []

    started = asyncio.Event()

    class _Exporter:
        def __init__(self, **kw):
            pass

        async def connect(self):
            rec.append("exporter.connect")
            started.set()
            if connect_error:
                raise ConnectionError("influxdb unreachable")

        async def close(self):
            rec.append("exporter.close")

        def get_health_metrics(self):
            return {"is_healthy": True}

        async def health_check(self):
            return True, "ok"

        @property
        def is_connected(self):
            return True

    class _Poller:
        def __init__(self, **kw):
            self._result_queue = asyncio.Queue()
            self.is_running = True

        async def start(self):
            rec.append("poller.start")

        async def stop(self):
            rec.append("poller.stop")

        def get_stats(self):
            return {}

        def is_making_progress(self):
            return True

    class _SSE:
        def __init__(self, **kw):
            pass

        async def start(self):
            rec.append("sse.start")

        async def stop(self):
            rec.append("sse.stop")

    class _Alert:
        def __init__(self, **kw):
            pass

        async def start(self):
            rec.append("alert.start")

        async def stop(self):
            rec.append("alert.stop")

    class _Enricher:
        def __init__(self, repository, inv_config, location):
            pass

        async def run(self):
            await _forever()

    class _Unpacker:
        def __init__(self, **kw):
            pass

    class _Extractor:
        def __init__(self, schema_loader):
            pass

    class _Discovery:
        def __init__(self, schema_loader, **kw):
            pass

    return {
        "repo": _Repo,
        "schema": _Schema,
        "exporter": _Exporter,
        "poller": _Poller,
        "sse": _SSE,
        "alert": _Alert,
        "enricher": _Enricher,
        "unpacker": _Unpacker,
        "extractor": _Extractor,
        "discovery": _Discovery,
        "started": started,
    }


def _install(monkeypatch, fakes, config, settings):
    """Wire all the fakes + config/settings into the collector module."""
    monkeypatch.setattr(cm, "get_config", lambda: config)
    monkeypatch.setattr(cm, "get_settings", lambda: settings)
    monkeypatch.setattr(cm, "TargetRepository", fakes["repo"])
    monkeypatch.setattr(cm, "SchemaLoader", fakes["schema"])
    monkeypatch.setattr(cm, "RedfishPoller", fakes["poller"])
    monkeypatch.setattr(cm, "SSEManager", fakes["sse"])
    monkeypatch.setattr(cm, "BlobUnpacker", fakes["unpacker"])
    monkeypatch.setattr(cm, "MetricExtractor", fakes["extractor"])
    monkeypatch.setattr(cm, "MetricDiscovery", fakes["discovery"])
    monkeypatch.setattr(cm, "create_health_app", lambda: object())
    monkeypatch.setattr(cm, "run_health_server", _forever)
    monkeypatch.setattr(cm, "result_processor_task", _forever)
    monkeypatch.setattr(cm, "cleanup_task", _forever)
    # Imported lazily inside run_collector -> patch at their source modules.
    import src.alert_manager as am
    import src.exporters.influxdb as influx
    import src.inventory.enricher as enr

    monkeypatch.setattr(influx, "InfluxDBExporter", fakes["exporter"])
    monkeypatch.setattr(am, "AlertManager", fakes["alert"])
    monkeypatch.setattr(enr, "InventoryEnricher", fakes["enricher"])


def _settings():
    return Settings(
        encryption_key="unit-test-key",
        alerts_database_url="postgresql://alerts/db",
        influxdb_token="unit-test-token",
        database_url="sqlite:///:memory:",
        schema_path="/app/config/metrics_schema.yaml",
    )


@pytest.fixture(autouse=True)
def _reset_module_globals():
    """Snapshot + restore the collector module globals the service mutates."""
    names = [
        "_exporter",
        "_poller",
        "_sse_manager",
        "_alert_manager",
        "_unpacker",
        "_extractor",
        "_discovery",
    ]
    saved = {n: getattr(cm, n) for n in names}
    try:
        yield
    finally:
        for n, v in saved.items():
            setattr(cm, n, v)


async def _drive(monkeypatch, fakes, config, use_fallback_signal=False):
    """Start run_collector, wait for startup, trigger a graceful shutdown."""
    _install(monkeypatch, fakes, config, _settings())
    loop = asyncio.get_running_loop()
    captured = {}

    if use_fallback_signal:
        # Force the add_signal_handler path to fail so the signal.signal
        # fallback branch is exercised; capture the handler it installs.
        def _raise(*a, **k):
            raise NotImplementedError

        monkeypatch.setattr(loop, "add_signal_handler", _raise)
        monkeypatch.setattr(
            cm.signal, "signal", lambda num, handler: captured.__setitem__(num, handler)
        )
    else:
        monkeypatch.setattr(
            loop, "add_signal_handler", lambda num, cb, *a: captured.__setitem__(num, cb)
        )

    task = asyncio.create_task(cm.run_collector())
    await asyncio.wait_for(fakes["started"].wait(), timeout=5)

    assert signal.SIGTERM in captured
    handler = captured[signal.SIGTERM]
    if use_fallback_signal:
        handler(signal.SIGTERM, None)  # signal.signal-style (signum, frame)
    else:
        handler()  # add_signal_handler-style (bound shutdown_event.set)

    await asyncio.wait_for(task, timeout=5)


# --------------------------------------------------------------------------- #
# run_collector — happy path (alerts + inventory enabled)
# --------------------------------------------------------------------------- #
async def test_run_collector_full_lifecycle(monkeypatch):
    rec: list = []
    fakes = _make_fakes(rec)
    config = AppConfig()  # defaults: alerts.enabled = inventory.enabled = True
    await _drive(monkeypatch, fakes, config)

    # Startup ran through every subsystem...
    for step in (
        "repo.init_db",
        "schema.load",
        "exporter.connect",
        "poller.start",
        "sse.start",
        "alert.start",
    ):
        assert step in rec, f"missing startup step {step}"
    # ...and shutdown tore them back down (producers before exporter close).
    assert rec.index("poller.stop") < rec.index("exporter.close")
    assert rec.index("sse.stop") < rec.index("exporter.close")
    assert "alert.stop" in rec and "repo.close" in rec
    # Global references were published for the health endpoint.
    assert cm._poller is not None and cm._exporter is not None


# --------------------------------------------------------------------------- #
# run_collector — degraded / disabled branches
# --------------------------------------------------------------------------- #
async def test_run_collector_degraded_and_disabled_branches(monkeypatch):
    """InfluxDB connect fails (degraded), schema load fails (default discovery),
    and alerts + inventory are disabled -> their start blocks are skipped."""
    rec: list = []
    fakes = _make_fakes(rec, connect_error=True, schema_error=True)
    config = AppConfig()
    config.alerts.enabled = False
    config.inventory.enabled = False
    await _drive(monkeypatch, fakes, config)

    # Exporter connect was attempted (and raised ConnectionError, swallowed).
    assert "exporter.connect" in rec
    # schema.load raised -> never recorded success.
    assert "schema.load" not in rec
    # Alerts disabled -> alert manager never started/stopped.
    assert "alert.start" not in rec and "alert.stop" not in rec
    # Poller still came up and was torn down.
    assert "poller.start" in rec and "poller.stop" in rec


# --------------------------------------------------------------------------- #
# run_collector — DB-not-ready retry branch
# --------------------------------------------------------------------------- #
async def test_run_collector_db_retry_then_succeeds(monkeypatch):
    rec: list = []
    fakes = _make_fakes(rec, db_fail_once=True)
    config = AppConfig()

    real_sleep = asyncio.sleep

    async def _fast_sleep(secs, *a, **k):
        await real_sleep(0)

    # Keep the 2s inter-retry wait from actually blocking the test.
    monkeypatch.setattr(cm.asyncio, "sleep", _fast_sleep)
    await _drive(monkeypatch, fakes, config)

    # init_db failed once then succeeded -> exactly one recorded success.
    assert rec.count("repo.init_db") == 1
    assert "poller.start" in rec  # startup continued past the retry


# --------------------------------------------------------------------------- #
# run_collector — DB never ready -> retries exhausted -> raise (still tears down)
# --------------------------------------------------------------------------- #
async def test_run_collector_db_never_ready_raises(monkeypatch):
    rec: list = []
    fakes = _make_fakes(rec, db_always_fail=True)
    config = AppConfig()
    _install(monkeypatch, fakes, config, _settings())

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *a, **k: None)

    real_sleep = asyncio.sleep

    async def _fast_sleep(secs, *a, **k):
        await real_sleep(0)

    monkeypatch.setattr(cm.asyncio, "sleep", _fast_sleep)

    with pytest.raises(RuntimeError, match="database never ready"):
        await asyncio.wait_for(cm.run_collector(), timeout=5)

    # Never got past the DB loop, but the finally block still closed the repo.
    assert "repo.init_db" not in rec
    assert "poller.start" not in rec
    assert "repo.close" in rec


# --------------------------------------------------------------------------- #
# run_collector — signal.signal fallback path
# --------------------------------------------------------------------------- #
async def test_run_collector_signal_signal_fallback(monkeypatch):
    rec: list = []
    fakes = _make_fakes(rec)
    config = AppConfig()
    await _drive(monkeypatch, fakes, config, use_fallback_signal=True)
    assert "poller.stop" in rec and "repo.close" in rec


# --------------------------------------------------------------------------- #
# run_collector — shutdown requested during startup (early return)
# --------------------------------------------------------------------------- #
async def test_run_collector_shutdown_during_startup(monkeypatch):
    rec: list = []
    fakes = _make_fakes(rec)
    config = AppConfig()
    _install(monkeypatch, fakes, config, _settings())

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *a, **k: None)

    # Pre-set the shutdown event so the DB-readiness loop aborts immediately,
    # before init_db is ever called.
    preset = asyncio.Event()
    preset.set()
    monkeypatch.setattr(cm.asyncio, "Event", lambda: preset)

    await asyncio.wait_for(cm.run_collector(), timeout=5)

    # Aborted before the DB came up; teardown still closed the repository.
    assert "repo.init_db" not in rec
    assert "poller.start" not in rec
    assert "repo.close" in rec


# --------------------------------------------------------------------------- #
# _shutdown_collector — queue-drain-before-cancel branch
# --------------------------------------------------------------------------- #
async def test_shutdown_drains_queue_before_cancel(monkeypatch):
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait("pending-result")

    class _P:
        def __init__(self, q):
            self._result_queue = q

        async def stop(self):
            pass

    processor = asyncio.create_task(_forever())
    real_sleep = asyncio.sleep

    async def _fast_sleep(secs, *a, **k):
        # Drain the queue during the first wait so the loop then breaks.
        if not queue.empty():
            queue.get_nowait()
        await real_sleep(0)

    monkeypatch.setattr(cm.asyncio, "sleep", _fast_sleep)
    await cm._shutdown_collector(poller=_P(queue), processor_task=processor)
    assert processor.done()


# --------------------------------------------------------------------------- #
# run_health_server + _NoSignalServer
# --------------------------------------------------------------------------- #
def test_no_signal_server_capture_signals_is_noop():
    server = cm._NoSignalServer(cm.uvicorn.Config(cm.create_health_app()))
    with server.capture_signals():
        pass  # the no-op yield body is what we cover


async def test_run_health_server_serves(monkeypatch):
    served = {}

    class _FakeServer:
        def __init__(self, config):
            served["config"] = config

        async def serve(self):
            served["served"] = True

    monkeypatch.setattr(cm, "_NoSignalServer", _FakeServer)
    await cm.run_health_server(cm.create_health_app(), port=18081)
    assert served["served"] is True
    assert served["config"].port == 18081


# --------------------------------------------------------------------------- #
# run() entry point
# --------------------------------------------------------------------------- #
@contextmanager
def _preserve_root_logging():
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    try:
        yield
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
        for h in handlers:
            root.addHandler(h)
        root.setLevel(level)


def test_run_missing_encryption_key_exits(monkeypatch):
    with _preserve_root_logging():
        monkeypatch.setattr(cm, "get_config", lambda: AppConfig())
        monkeypatch.setattr(
            cm,
            "get_settings",
            lambda: Settings(encryption_key="", alerts_database_url="pg://x", influxdb_token="t"),
        )
        with pytest.raises(SystemExit) as ei:
            cm.run()
    assert ei.value.code == 1


def test_run_missing_alerts_db_exits(monkeypatch):
    with _preserve_root_logging():
        config = AppConfig()  # alerts.enabled default True
        monkeypatch.setattr(cm, "get_config", lambda: config)
        monkeypatch.setattr(
            cm,
            "get_settings",
            lambda: Settings(encryption_key="k", alerts_database_url="", influxdb_token="t"),
        )
        with pytest.raises(SystemExit) as ei:
            cm.run()
    assert ei.value.code == 1


def test_run_happy_path_invokes_asyncio_run(monkeypatch):
    calls = {}

    def _fake_run(coro):
        coro.close()  # avoid "coroutine was never awaited"
        calls["ran"] = True

    with _preserve_root_logging():
        config = AppConfig()  # alerts enabled + loopback webhook fallback -> warning
        monkeypatch.setattr(cm, "get_config", lambda: config)
        monkeypatch.setattr(
            cm,
            "get_settings",
            lambda: Settings(
                encryption_key="k", alerts_database_url="pg://x", influxdb_token=""
            ),  # empty token -> warning branch
        )
        monkeypatch.setattr(cm.asyncio, "run", _fake_run)
        cm.run()
    assert calls.get("ran") is True


def test_run_keyboard_interrupt_is_swallowed(monkeypatch):
    def _fake_run(coro):
        coro.close()
        raise KeyboardInterrupt

    with _preserve_root_logging():
        monkeypatch.setattr(cm, "get_config", lambda: AppConfig())
        monkeypatch.setattr(
            cm,
            "get_settings",
            lambda: Settings(encryption_key="k", alerts_database_url="pg://x", influxdb_token="t"),
        )
        monkeypatch.setattr(cm.asyncio, "run", _fake_run)
        cm.run()  # must return normally (no SystemExit)


def test_run_crash_exits(monkeypatch):
    def _fake_run(coro):
        coro.close()
        raise RuntimeError("collector blew up")

    with _preserve_root_logging():
        monkeypatch.setattr(cm, "get_config", lambda: AppConfig())
        monkeypatch.setattr(
            cm,
            "get_settings",
            lambda: Settings(encryption_key="k", alerts_database_url="pg://x", influxdb_token="t"),
        )
        monkeypatch.setattr(cm.asyncio, "run", _fake_run)
        with pytest.raises(SystemExit) as ei:
            cm.run()
    assert ei.value.code == 1
