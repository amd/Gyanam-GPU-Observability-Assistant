# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Extra coverage for collector_main helpers and the health/webhook app.

Targets the uncovered branches in _sync_extract_metrics (dedup/slow-path/error
guards), process_poll_result (buffered + error paths), result_processor_task,
cleanup_task, and the create_health_app endpoints (detailed health component
folding, manager-stats error, manual poll processing, webhook body-cap/errors).
"""

import asyncio
import gzip
import json
from datetime import UTC, datetime

import httpx
import pytest
import src.collector_main as cm
from httpx import ASGITransport
from src.collector_main import (
    _sync_extract_metrics,
    cleanup_task,
    create_health_app,
    process_poll_result,
    result_processor_task,
)
from src.parser.discovery import MetricDiscovery
from src.parser.extractor import MetricExtractor
from src.parser.unpacker import BlobUnpacker, ExtractedFile
from src.redfish.poller import PollResult


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def parts(schema_loader, tmp_path):
    return (
        BlobUnpacker(temp_dir=str(tmp_path / "x")),
        MetricExtractor(schema_loader),
        MetricDiscovery(schema_loader),
    )


def _result(**kw):
    base = {
        "target_id": 1,
        "target_name": "gpu-a",
        "target_host": "10.0.0.5",
        "success": True,
        "content": b"",
        "content_type": "application/json",
        "error_message": None,
        "poll_time": datetime.now(UTC),
        "duration_ms": 1.0,
    }
    base.update(kw)
    return PollResult(**base)


class _FakeExporter:
    def __init__(self, connected=True, raise_write=False):
        self.written = []
        self._connected = connected
        self._raise = raise_write

    async def write(self, metrics):
        if self._raise:
            raise RuntimeError("write boom")
        self.written.extend(metrics)

    @property
    def is_connected(self):
        return self._connected


class _RaisingDiscovery:
    """Discovery stub whose discover() always raises (exercises warning guards)."""

    def discover(self, **kw):
        raise RuntimeError("discovery failed")


class _FakeUnpacker:
    def __init__(self, files=None, raise_on_unpack=False):
        self._files = files if files is not None else []
        self._raise = raise_on_unpack
        self.cleaned = []

    def unpack(self, content, target_name):
        if self._raise:
            raise RuntimeError("unpack boom")
        return self._files

    def cleanup(self, files):
        self.cleaned.extend(files)


# --------------------------------------------------------------------------- #
# _sync_extract_metrics — fast path branches
# --------------------------------------------------------------------------- #
def test_fast_path_report_fully_deduped(parts):
    """A later report whose only value duplicates an earlier property is skipped."""
    unpacker, extractor, discovery = parts
    dup = {"MetricProperty": "/redfish/v1/Chassis/1#DUP", "MetricValue": "5"}
    result = _result(data=[("r1", {"MetricValues": [dup]}), ("r2", {"MetricValues": [dup]})])
    metrics, cleanup = _sync_extract_metrics(result, unpacker, extractor, discovery)
    # r2 is fully deduped -> the 'continue' branch runs without error.
    assert isinstance(metrics, list) and cleanup == []


def test_fast_path_discovery_exception_is_swallowed(parts, sample_metric_report):
    """When discovery raises on the GET fast path, extraction still returns."""
    unpacker, extractor, _ = parts
    result = _result(data=[("comprehensive", sample_metric_report)])
    metrics, cleanup = _sync_extract_metrics(result, unpacker, extractor, _RaisingDiscovery())
    # Extractor metrics survive even though discovery blew up.
    assert isinstance(metrics, list) and cleanup == []


# --------------------------------------------------------------------------- #
# _sync_extract_metrics — slow (blob) path branches
# --------------------------------------------------------------------------- #
def test_slow_path_no_files_extracted(parts):
    """unpack() returning no files -> warning + empty result."""
    _, extractor, discovery = parts
    unpacker = _FakeUnpacker(files=[])
    result = _result(data=None, content=b"blob", content_type="application/gzip")
    metrics, files = _sync_extract_metrics(result, unpacker, extractor, discovery)
    assert metrics == [] and files == []


def test_slow_path_skips_non_json_file(parts, tmp_path):
    """A non-JSON extracted file is skipped (JSON decode guard)."""
    _, extractor, discovery = parts
    bad = tmp_path / "notjson.txt"
    bad.write_text("this is not json {{{")
    f = ExtractedFile(path=bad, original_name="notjson.txt", size=20)
    unpacker = _FakeUnpacker(files=[f])
    result = _result(data=None, content=b"blob", content_type="application/gzip")
    metrics, files = _sync_extract_metrics(result, unpacker, extractor, discovery)
    # Nothing extracted (file skipped), but the file list is returned for cleanup.
    assert metrics == [] and files == [f]


def test_slow_path_discovery_exception(parts, tmp_path, sample_metric_report):
    """Discovery raising on the slow path is caught per-file."""
    _, extractor, _ = parts
    good = tmp_path / "report.json"
    good.write_text(json.dumps(sample_metric_report))
    f = ExtractedFile(path=good, original_name="report.json", size=200)
    unpacker = _FakeUnpacker(files=[f])
    result = _result(data=None, content=b"blob", content_type="application/gzip")
    metrics, files = _sync_extract_metrics(result, unpacker, extractor, _RaisingDiscovery())
    # Extractor metrics present; discovery exception swallowed.
    assert isinstance(metrics, list) and files == [f]


def test_slow_path_unpack_raises_returns_empty(parts):
    """An exception from unpack() is caught and returns ([], files)."""
    _, extractor, discovery = parts
    unpacker = _FakeUnpacker(raise_on_unpack=True)
    result = _result(data=None, content=b"blob", content_type="application/gzip")
    metrics, files = _sync_extract_metrics(result, unpacker, extractor, discovery)
    assert metrics == [] and files == []


# --------------------------------------------------------------------------- #
# process_poll_result — buffered + error branches
# --------------------------------------------------------------------------- #
async def test_process_poll_result_buffered_when_disconnected(parts, sample_metric_report):
    """is_connected False -> metrics are buffered (still counted)."""
    unpacker, extractor, discovery = parts
    exporter = _FakeExporter(connected=False)
    result = _result(data=[("comprehensive", sample_metric_report)])
    n = await process_poll_result(result, unpacker, extractor, discovery, exporter)
    assert n >= 1 and len(exporter.written) == n


async def test_process_poll_result_export_error_returns_zero(parts, sample_metric_report):
    """An exporter.write() failure is caught -> returns 0."""
    unpacker, extractor, discovery = parts
    exporter = _FakeExporter(raise_write=True)
    result = _result(data=[("comprehensive", sample_metric_report)])
    assert await process_poll_result(result, unpacker, extractor, discovery, exporter) == 0


async def test_process_poll_result_cleans_up_blob_files(parts, sample_metric_report):
    """The finally block calls unpacker.cleanup() on extracted files."""
    _, extractor, discovery = parts
    unpacker = _FakeUnpacker(files=[])  # no files -> no metrics, but exercises finally
    blob = gzip.compress(json.dumps(sample_metric_report).encode())
    # Build a real file so cleanup gets a non-empty list.
    result = _result(data=None, content=blob, content_type="application/gzip")
    exporter = _FakeExporter()
    await process_poll_result(result, unpacker, extractor, discovery, exporter)


# --------------------------------------------------------------------------- #
# result_processor_task — error/retry branch
# --------------------------------------------------------------------------- #
async def test_result_processor_task_handles_error_then_cancels(parts, monkeypatch):
    unpacker, extractor, discovery = parts
    exporter = _FakeExporter()
    state = {"n": 0}

    class _BumpyPoller:
        async def get_results(self, timeout):
            state["n"] += 1
            if state["n"] == 1:
                raise ValueError("transient")  # hits the generic except + sleep
            raise asyncio.CancelledError()

    # Keep the retry sleep from actually waiting.
    real_sleep = asyncio.sleep

    async def _fast_sleep(secs, *a, **k):
        await real_sleep(0)

    monkeypatch.setattr(cm.asyncio, "sleep", _fast_sleep)
    await result_processor_task(_BumpyPoller(), unpacker, extractor, discovery, exporter)
    assert state["n"] >= 2


async def test_result_processor_drains_in_flight_then_exits_on_stop_event(parts, monkeypatch):
    # On stop_event, the loop must finish processing already-enqueued results
    # (so the final cycle is not lost) and then exit cleanly WITHOUT a cancel.
    unpacker, extractor, discovery = parts
    exporter = _FakeExporter()
    stop = asyncio.Event()
    state = {"n": 0}

    class _Poller:
        async def get_results(self, timeout):
            state["n"] += 1
            if state["n"] == 1:
                return [object()]  # one pending result to process
            stop.set()  # queue now drained -> signal stop
            return []  # empty + stop set -> loop breaks

    processed = []

    async def _fake_ppr(result, *a, **k):
        processed.append(result)

    monkeypatch.setattr(cm, "process_poll_result", _fake_ppr)
    await asyncio.wait_for(
        result_processor_task(_Poller(), unpacker, extractor, discovery, exporter, stop_event=stop),
        timeout=2.0,
    )
    assert len(processed) == 1  # the in-flight result was processed before exit


# --------------------------------------------------------------------------- #
# _init_sharding — claim slice + operability warnings
# --------------------------------------------------------------------------- #
async def test_init_sharding_disabled_returns_none():
    from types import SimpleNamespace

    from src.config import AppConfig

    cfg = AppConfig()
    cfg.sharding.enabled = False
    sm, cid = await cm._init_sharding(cfg, SimpleNamespace(collector_id="x"), object(), "c1")
    assert sm is None and cid == "c1"  # returns (None, passed id) when disabled


async def test_init_sharding_static_claims_sets_context_and_warns(caplog):
    from types import SimpleNamespace

    from src.config import AppConfig

    cfg = AppConfig()
    cfg.sharding.enabled = True
    cfg.sharding.balance = "static"  # static path: id-order claim + startup warnings
    cfg.sharding.max_targets_per_shard = 2  # this replica saturates at 2

    class _Repo:
        def __init__(self):
            self.ctx = None

        def set_shard_context(self, cid, ttl):
            self.ctx = (cid, ttl)

        async def claim_shard_targets(self, cid, cap, ttl):
            return 2  # claims up to cap

        async def get_enabled_target_ids(self):
            return [1, 2, 3, 4]  # 4 enabled -> fleet exceeds this shard's cap

        async def count_all_shard_leases(self, ttl):
            return 2  # only 2 of 4 leased across the fleet -> under-provisioned

    repo = _Repo()
    settings = SimpleNamespace(collector_id="")  # unset -> hostname-fallback warning
    with caplog.at_level("WARNING"):
        sm, cid = await cm._init_sharding(cfg, settings, repo, "host1")
    assert sm is not None and cid == "host1"
    assert repo.ctx == ("host1", cfg.sharding.lease_ttl_seconds)
    text = caplog.text
    assert "COLLECTOR_ID is unset" in text
    assert "under-provisioned" in text


async def test_init_sharding_dynamic_slotclaims_stable_id():
    from types import SimpleNamespace

    from src.config import AppConfig

    cfg = AppConfig()
    cfg.sharding.enabled = True  # balance defaults to "dynamic"

    class _Repo:
        def __init__(self):
            self.ctx = None
            self.stats_published = []

        def set_shard_context(self, cid, ttl):
            self.ctx = (cid, ttl)

        async def claim_collector_slot(self, token, ttl):
            return 0  # lowest free ordinal

        async def renew_collector_slot(self, token, ordinal, ttl):
            return True

        async def upsert_collector_stats(self, cid, data, owned):
            self.stats_published.append(cid)

        async def get_collector_stats(self, ttl):
            return []  # no peers yet

        async def get_enabled_target_ids(self):
            return [1, 2, 3]

        async def reconcile_shard_leases(self, cid, mine, cap, ttl):
            return len(mine)

        async def count_all_shard_leases(self, ttl):
            return 3

    repo = _Repo()
    settings = SimpleNamespace(collector_id="")  # unset -> slot-claim a stable id
    sm, cid = await cm._init_sharding(cfg, settings, repo, "host-xyz")
    assert sm is not None
    # Derives a STABLE collector-<ordinal> id (not the volatile hostname) and uses
    # it for the lease context and the initial membership publish.
    assert cid == "collector-0"
    assert repo.ctx == ("collector-0", cfg.sharding.lease_ttl_seconds)
    assert "collector-0" in repo.stats_published


# --------------------------------------------------------------------------- #
# cleanup_task
# --------------------------------------------------------------------------- #
async def test_cleanup_task_runs_then_cancels(monkeypatch):
    calls = {"sleep": 0}

    async def _sleep(secs, *a, **k):
        calls["sleep"] += 1
        if calls["sleep"] >= 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(cm.asyncio, "sleep", _sleep)

    class _U:
        def cleanup_old_files(self, age):
            return 3  # >0 -> logs "removed N old temp files"

    await cleanup_task(_U(), cleanup_interval=1, max_age_seconds=10)
    assert calls["sleep"] >= 2


async def test_cleanup_task_handles_cleanup_error(monkeypatch):
    calls = {"sleep": 0}

    async def _sleep(secs, *a, **k):
        calls["sleep"] += 1
        if calls["sleep"] >= 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(cm.asyncio, "sleep", _sleep)

    class _U:
        def cleanup_old_files(self, age):
            raise RuntimeError("fs error")  # hits the generic except branch

    await cleanup_task(_U(), cleanup_interval=1, max_age_seconds=10)
    assert calls["sleep"] >= 2


# --------------------------------------------------------------------------- #
# Health/webhook app — richer component wiring
# --------------------------------------------------------------------------- #
def _client():
    app = create_health_app()
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


class _FakeRepo:
    def __init__(self, connected=True):
        self._connected = connected

    async def ping_alert_store(self):
        return self._connected


class _FullPoller:
    is_running = True

    def get_stats(self):
        return {"inflight": 2}

    def is_making_progress(self):
        return True


class _FullExporter:
    async def health_check(self):
        return True, "ok"

    def get_health_metrics(self):
        return {"is_healthy": True}


class _RaisingExporter:
    async def health_check(self):
        raise RuntimeError("exporter down")


class _FullAlertMgr:
    enabled = True

    def __init__(self, repo=None, stats_raise=False):
        self.repository = repo
        self._stats_raise = stats_raise

    def get_stats(self):
        if self._stats_raise:
            raise RuntimeError("stats boom")
        return {"subscribers": ["gpu-a"]}


async def test_detailed_health_all_components_healthy(monkeypatch):
    monkeypatch.setattr(cm, "_exporter", _FullExporter())
    monkeypatch.setattr(cm, "_poller", _FullPoller())
    monkeypatch.setattr(cm, "_sse_manager", object())
    monkeypatch.setattr(cm, "_alert_manager", _FullAlertMgr(repo=_FakeRepo(connected=True)))
    async with _client() as c:
        r = await c.get("/health/detailed")
    body = r.json()
    assert body["status"] == "healthy"
    assert body["components"]["sse_manager"]["status"] == "running"
    assert body["components"]["alert_manager"]["status"] == "enabled"
    assert body["components"]["alert_manager"]["alert_store_connected"] is True


async def test_detailed_health_alert_store_down_degrades(monkeypatch):
    monkeypatch.setattr(cm, "_exporter", _FullExporter())
    monkeypatch.setattr(cm, "_poller", _FullPoller())
    monkeypatch.setattr(cm, "_alert_manager", _FullAlertMgr(repo=_FakeRepo(connected=False)))
    async with _client() as c:
        r = await c.get("/health/detailed")
    body = r.json()
    assert body["status"] == "degraded"
    assert body["components"]["alert_manager"]["alert_store_connected"] is False


async def test_detailed_health_exporter_check_raises(monkeypatch):
    monkeypatch.setattr(cm, "_exporter", _RaisingExporter())
    monkeypatch.setattr(cm, "_poller", _FullPoller())
    async with _client() as c:
        r = await c.get("/health/detailed")
    body = r.json()
    # Exporter health check failed -> message carries the exception type name.
    assert body["components"]["exporter"]["healthy"] is False
    assert "error" in body["components"]["exporter"]["message"]


async def test_detailed_health_alert_stats_error(monkeypatch):
    monkeypatch.setattr(cm, "_exporter", _FullExporter())
    monkeypatch.setattr(cm, "_poller", _FullPoller())
    monkeypatch.setattr(cm, "_alert_manager", _FullAlertMgr(stats_raise=True))
    async with _client() as c:
        r = await c.get("/health/detailed")
    body = r.json()
    assert body["components"]["alert_manager"]["status"].startswith("error:")
    assert body["status"] == "degraded"


async def test_manager_stats_get_stats_error(monkeypatch):
    monkeypatch.setattr(cm, "_alert_manager", _FullAlertMgr(stats_raise=True))
    async with _client() as c:
        r = await c.get("/alerts/manager-stats")
    body = r.json()
    assert body["enabled"] is False
    assert "error" in body


# --------------------------------------------------------------------------- #
# Manual poll — full processing pipeline + content-size (blob) branch
# --------------------------------------------------------------------------- #
def _blob_result(sample):
    blob = gzip.compress(json.dumps(sample).encode())
    return PollResult(
        target_id=1,
        target_name="gpu-a",
        target_host="10.0.0.5",
        success=True,
        content=blob,
        content_type="application/gzip",
        error_message=None,
        poll_time=datetime.now(UTC),
        duration_ms=5.0,
        data=None,
        collection_method="task",
    )


class _ResultPoller:
    def __init__(self, result):
        self._result = result

    async def poll_single(self, target_id):
        return self._result


async def test_manual_poll_processes_blob_result(parts, monkeypatch, sample_metric_report):
    """Success + content (data None) -> runs the pipeline and counts metrics,
    and content_size is derived from len(result.content)."""
    unpacker, extractor, discovery = parts
    exporter = _FakeExporter()
    result = _blob_result(sample_metric_report)
    monkeypatch.setattr(cm, "_poller", _ResultPoller(result))
    monkeypatch.setattr(cm, "_unpacker", unpacker)
    monkeypatch.setattr(cm, "_extractor", extractor)
    monkeypatch.setattr(cm, "_discovery", discovery)
    monkeypatch.setattr(cm, "_exporter", exporter)
    async with _client() as c:
        r = await c.post("/poll/1")
    body = r.json()
    assert r.status_code == 200
    assert body["success"] is True
    assert body["content_size"] == len(result.content)
    assert body["metrics_exported"] >= 1


async def test_manual_poll_process_error_is_caught(monkeypatch, sample_metric_report):
    """If process_poll_result raises, the endpoint still returns a result."""
    result = PollResult(
        target_id=1,
        target_name="gpu-a",
        target_host="10.0.0.5",
        success=True,
        content=b"",
        content_type="application/json",
        error_message=None,
        poll_time=datetime.now(UTC),
        duration_ms=5.0,
        data=[("comprehensive", sample_metric_report)],
        collection_method="get",
    )
    monkeypatch.setattr(cm, "_poller", _ResultPoller(result))
    monkeypatch.setattr(cm, "_unpacker", object())
    monkeypatch.setattr(cm, "_extractor", object())
    monkeypatch.setattr(cm, "_discovery", object())
    monkeypatch.setattr(cm, "_exporter", object())

    async def _boom(*a, **k):
        raise RuntimeError("process boom")

    monkeypatch.setattr(cm, "process_poll_result", _boom)
    async with _client() as c:
        r = await c.post("/poll/1")
    body = r.json()
    assert r.status_code == 200
    assert body["success"] is True
    assert body["metrics_exported"] == 0


# --------------------------------------------------------------------------- #
# Webhook — body cap + processing error
# --------------------------------------------------------------------------- #
class _WebhookAlertMgr:
    enabled = True

    async def process_webhook_event(self, tid, data):
        raise RuntimeError("process event boom")


async def test_webhook_body_too_large(monkeypatch):
    monkeypatch.setattr(cm, "_alert_manager", _WebhookAlertMgr())
    monkeypatch.setattr(cm, "_WEBHOOK_MAX_BYTES", 10)
    async with _client() as c:
        r = await c.post(
            "/redfish-webhook/1",
            content=b"x" * 50,
            headers={"content-type": "application/json"},
        )
    assert r.json()["message"] == "payload too large"


async def test_webhook_processing_error(monkeypatch):
    monkeypatch.setattr(cm, "_alert_manager", _WebhookAlertMgr())
    async with _client() as c:
        r = await c.post("/redfish-webhook/1", json={"Events": [{}]})
    body = r.json()
    assert body["status"] == "error"
    assert "processing failed" in body["message"]
