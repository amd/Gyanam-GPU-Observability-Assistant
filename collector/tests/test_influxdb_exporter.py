# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for InfluxDB exporter buffering/health (no live InfluxDB)."""

import asyncio
import contextlib
from datetime import UTC, datetime

import pytest
from src.exporters.base import Metric
from src.exporters.influxdb import InfluxDBExporter


def _exp(**kw):
    kw.setdefault("batch_size", 1)  # -> max_buffer_size = 200
    return InfluxDBExporter(url="http://x:8086", token="t", org="o", bucket="b", **kw)


def _m(name="gpu_temp", value=1.0):
    return Metric(name=name, value=value, timestamp=datetime.now(UTC), tags={"host": "h"}, unit="C")


def test_metric_to_point_line_protocol():
    lp = _exp()._metric_to_point(_m()).to_line_protocol()
    assert lp.startswith("gpu_temp")
    assert "host=h" in lp
    assert "unit=C" in lp
    assert "value=1" in lp


async def test_write_buffers_without_network():
    exp = _exp()
    wrote = await exp.write([_m(), _m()])
    assert wrote is True
    assert exp.buffer_size == 2
    assert exp.dropped_points == 0


async def test_buffer_cap_drops_oldest():
    exp = _exp(batch_size=1)  # max buffer 200
    await exp.write([_m(value=float(i)) for i in range(250)])
    assert exp.buffer_size == 200
    assert exp.dropped_points == 50


def test_is_connected_false_without_write_api():
    assert _exp().is_connected is False


def test_get_health_metrics_shape():
    hm = _exp().get_health_metrics()
    assert isinstance(hm, dict)
    for key in ("connected", "buffer_size", "total_points_dropped", "is_healthy"):
        assert key in hm
    assert hm["connected"] is False  # not connected in this test


class _FakeWriteApi:
    def __init__(self, fail=False):
        self.written = []
        self.fail = fail

    async def write(self, bucket, org, record, write_precision=None):
        if self.fail:
            raise RuntimeError("influx down")
        # The batch path pre-serializes Points to line-protocol strings.
        self.written.extend(record)


async def test_flush_writes_and_drains():
    exp = _exp(batch_size=1000)
    exp._write_api = _FakeWriteApi()
    await exp.write([_m(), _m(), _m()])
    await exp._flush_buffer()
    assert len(exp._write_api.written) == 3
    assert exp.buffer_size == 0


async def test_flush_without_write_api_readds_points():
    exp = _exp(batch_size=1000)
    await exp.write([_m()])
    exp._write_api = None
    await exp._flush_buffer()
    # Points must be preserved (re-added), not lost.
    assert exp.buffer_size == 1


async def test_write_batch_semaphore_success():
    # Contract: returns (retry_points, written_points, duration_ms).
    exp = _exp()
    api = _FakeWriteApi()
    pts = [exp._metric_to_point(_m())]
    retry, written, _ms = await exp._write_batch_with_semaphore(api, pts, 1, 1)
    assert retry == []
    assert written == 1


async def test_write_batch_semaphore_failure():
    exp = _exp()
    api = _FakeWriteApi(fail=True)
    pts = [exp._metric_to_point(_m())]
    retry, written, _ms = await exp._write_batch_with_semaphore(api, pts, 1, 1)
    # Transient failure: original Points returned for retry (not the serialized
    # form), nothing counted as written.
    assert retry == pts
    assert written == 0


async def test_large_batch_serializes_off_loop_and_writes():
    # >= the offload threshold -> serialized via asyncio.to_thread; the write API
    # receives ready line-protocol strings (one per point).
    exp = _exp()
    api = _FakeWriteApi()
    pts = [exp._metric_to_point(_m(value=float(i))) for i in range(300)]
    retry, written, _ms = await exp._write_batch_with_semaphore(api, pts, 1, 1)
    assert retry == [] and written == 300
    assert len(api.written) == 300
    assert all(isinstance(r, str) and "value=" in r for r in api.written)


def test_serialize_batch_produces_line_protocol():
    from src.exporters.influxdb import _serialize_batch

    exp = _exp()
    lines = _serialize_batch([exp._metric_to_point(_m(value=42.0))])
    assert len(lines) == 1 and isinstance(lines[0], str)
    assert "value=42" in lines[0]


# ---- partial / full flush failure ----


class _ValueFail:
    """Write API that fails only for batches containing a given metric value."""

    def __init__(self, fail_values):
        self.written = []
        self.fail_values = fail_values

    async def write(self, bucket, org, record, write_precision=None):
        # record is already line-protocol strings (pre-serialized by the exporter).
        for lp in record:
            if any(f"value={v}" in lp for v in self.fail_values):
                raise RuntimeError("batch fail")
        self.written.extend(record)


async def test_flush_partial_failure_requeues_only_failed():
    exp = _exp(batch_size=1)  # one point per batch
    exp._write_api = _ValueFail(fail_values=["20"])
    await exp.write([_m(value=10.0), _m(value=20.0), _m(value=30.0)])
    await exp._flush_buffer()
    assert len(exp._write_api.written) == 2  # 10 and 30 got through
    assert exp.buffer_size == 1  # the failed point (20) re-queued
    assert exp._successful_batches == 2 and exp._failed_batches == 1


async def test_flush_full_failure_counts_consecutive():
    exp = _exp(batch_size=1)
    exp._write_api = _FakeWriteApi(fail=True)
    await exp.write([_m(value=1.0), _m(value=2.0)])
    await exp._flush_buffer()
    assert exp._consecutive_batch_failures >= 1
    assert exp.buffer_size == 2  # all points re-queued


# ---- write_immediate ----


async def test_write_immediate_not_connected():
    assert await _exp().write_immediate([_m()]) is False


async def test_write_immediate_success_and_failure():
    exp = _exp()
    exp._write_api = _FakeWriteApi()
    assert await exp.write_immediate([_m(), _m()]) is True
    assert len(exp._write_api.written) == 2
    exp._write_api = _FakeWriteApi(fail=True)
    assert await exp.write_immediate([_m()]) is False


# ---- health_check + get_health_metrics branches ----


async def test_health_check_not_connected():
    ok, msg = await _exp().health_check()
    assert ok is False and msg == "Not connected"


def test_health_metrics_healthy():
    exp = _exp()
    exp._write_api = object()  # alive
    exp._last_write_time = datetime.now(UTC)  # recent
    hm = exp.get_health_metrics()
    assert hm["connected"] is True and hm["is_healthy"] is True


def test_health_metrics_data_loss_unhealthy():
    exp = _exp()
    exp._write_api = object()
    exp._last_write_time = datetime.now(UTC)
    exp._dropped_points = 5
    hm = exp.get_health_metrics()
    assert hm["is_healthy"] is False  # zero-tolerance on data loss
    assert hm["total_points_dropped"] == 5


def test_health_metrics_pipeline_dead_disconnected():
    exp = _exp()
    exp._write_api = object()
    exp._last_write_time = datetime(2020, 1, 1, tzinfo=UTC)  # ancient -> pipeline dead
    hm = exp.get_health_metrics()
    assert hm["connected"] is False


def test_health_metrics_failure_rate():
    exp = _exp()
    exp._successful_batches = 90
    exp._failed_batches = 10
    assert exp.get_health_metrics()["failure_rate_pct"] == 10.0


# ---- connect() via a fake InfluxDBClientAsync ----


class _FakeInflux:
    def __init__(self, ping_ok=True, **kw):
        self._ping_ok = ping_ok

    async def ping(self):
        return self._ping_ok

    def write_api(self):
        return _FakeWriteApi()

    async def close(self):
        return None


async def _stop(exp):
    exp._running = False
    if exp._flush_task:
        exp._flush_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await exp._flush_task


async def test_connect_success(monkeypatch):
    monkeypatch.setattr(
        "src.exporters.influxdb.InfluxDBClientAsync", lambda **kw: _FakeInflux(ping_ok=True)
    )
    exp = _exp()
    await exp.connect(max_retries=1)
    try:
        assert exp._client is not None and exp._write_api is not None
    finally:
        await _stop(exp)


async def test_connect_failure_raises(monkeypatch):
    monkeypatch.setattr(
        "src.exporters.influxdb.InfluxDBClientAsync", lambda **kw: _FakeInflux(ping_ok=False)
    )
    exp = _exp()
    with pytest.raises(ConnectionError):
        await exp.connect(max_retries=1)
    await _stop(exp)


async def test_health_check_connected():
    exp = _exp()
    exp._client = _FakeInflux(ping_ok=True)
    ok, msg = await exp.health_check()
    assert ok is True and "Connected" in msg


async def test_health_check_ping_false():
    exp = _exp()
    exp._client = _FakeInflux(ping_ok=False)
    ok, _ = await exp.health_check()
    assert ok is False


async def test_reconnect_success(monkeypatch):
    monkeypatch.setattr(
        "src.exporters.influxdb.InfluxDBClientAsync", lambda **kw: _FakeInflux(ping_ok=True)
    )
    exp = _exp()
    assert await exp._reconnect() is True
    assert exp._write_api is not None and exp._reconnect_count == 1


async def test_reconnect_ping_false(monkeypatch):
    monkeypatch.setattr(
        "src.exporters.influxdb.InfluxDBClientAsync", lambda **kw: _FakeInflux(ping_ok=False)
    )
    exp = _exp()
    assert await exp._reconnect() is False
    assert exp._client is None and exp._write_api is None


async def test_force_reset_connection():
    exp = _exp()
    exp._client = _FakeInflux()
    exp._write_api = _FakeWriteApi()
    exp._consecutive_batch_failures = 9
    await exp._force_reset_connection()
    assert exp._client is None and exp._write_api is None
    assert exp._consecutive_batch_failures == 0


async def test_close_flushes_and_clears():
    exp = _exp()
    exp._client = _FakeInflux()
    exp._write_api = _FakeWriteApi()
    await exp.close()
    assert exp._running is False and exp._client is None


async def test_query_not_connected_raises():
    with pytest.raises(RuntimeError):
        await _exp().query("from(bucket)")


async def test_query_returns_record_values():
    class _Rec:
        values = {"_time": "t", "_value": 1}

    class _Tbl:
        records = [_Rec()]

    class _QApi:
        async def query(self, q):
            return [_Tbl()]

    class _Client:
        def query_api(self):
            return _QApi()

    exp = _exp()
    exp._client = _Client()
    assert await exp.query("from(bucket)") == [{"_time": "t", "_value": 1}]
