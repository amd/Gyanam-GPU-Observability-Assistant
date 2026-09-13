# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for InfluxDB exporter buffering/health (no live InfluxDB)."""

from datetime import UTC, datetime

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

    async def write(self, bucket, org, record):
        if self.fail:
            raise RuntimeError("influx down")
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
    exp = _exp()
    api = _FakeWriteApi()
    pts = [exp._metric_to_point(_m())]
    ok, failed, _ms = await exp._write_batch_with_semaphore(api, pts, 1, 1)
    assert ok is True
    assert failed == []


async def test_write_batch_semaphore_failure():
    exp = _exp()
    api = _FakeWriteApi(fail=True)
    pts = [exp._metric_to_point(_m())]
    ok, failed, _ms = await exp._write_batch_with_semaphore(api, pts, 1, 1)
    assert ok is False
    assert failed == pts
