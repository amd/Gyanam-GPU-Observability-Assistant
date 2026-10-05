# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Additional InfluxDB exporter tests targeting previously-uncovered paths.

No live InfluxDB: the async client and the write_api are faked/monkeypatched.
Covers the flush loop, multi-batch flush, partial/full failures, the
consecutive-failure reconnect threshold, write_immediate, connect() retry +
degrade, reconnect timeout/error branches, buffer-overflow drop counting,
close()'s task cancellation + flush, and get_health_metrics branches.
"""

import asyncio
import contextlib
from datetime import UTC, datetime

import pytest
from src.exporters.base import Metric
from src.exporters.influxdb import InfluxDBExporter


def _exp(**kw):
    kw.setdefault("batch_size", 1000)
    return InfluxDBExporter(url="http://gpu-a:8086", token="t", org="o", bucket="b", **kw)


def _m(name="gpu_temp", value=1.0):
    return Metric(
        name=name, value=value, timestamp=datetime.now(UTC), tags={"host": "gpu-a"}, unit="C"
    )


class _FakeWriteApi:
    def __init__(self, fail=False):
        self.written = []
        self.fail = fail

    async def write(self, bucket, org, record, write_precision=None):
        if self.fail:
            raise RuntimeError("influx down")
        self.written.extend(record)


class _FakeInflux:
    def __init__(self, ping_ok=True, **kw):
        self._ping_ok = ping_ok
        self.closed = False

    async def ping(self):
        return self._ping_ok

    def write_api(self):
        return _FakeWriteApi()

    async def close(self):
        self.closed = True


async def _stop(exp):
    exp._running = False
    if exp._flush_task:
        exp._flush_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await exp._flush_task


# --------------------------------------------------------------------------
# _write_batch_with_semaphore: timeout + auth-hint branches
# --------------------------------------------------------------------------


async def test_write_batch_timeout_requeues():
    exp = _exp(write_timeout_ms=1)  # 1ms -> write that sleeps longer times out

    class _Slow:
        async def write(self, bucket, org, record, write_precision=None):
            await asyncio.sleep(0.05)

    pts = [exp._metric_to_point(_m())]
    # Contract: (retry_points, written_points, duration_ms).
    retry, written, ms = await exp._write_batch_with_semaphore(_Slow(), pts, 1, 2)
    assert retry == pts
    assert written == 0
    assert ms == 0


async def test_write_batch_auth_hint_logged_once():
    exp = _exp()

    class _Auth401:
        async def write(self, bucket, org, record, write_precision=None):
            raise RuntimeError("HTTP 401 Unauthorized: token mismatch")

    pts = [exp._metric_to_point(_m())]
    retry, written, _ = await exp._write_batch_with_semaphore(_Auth401(), pts, 1, 1)
    # A 401 is transient here (token may be rotated back) -> re-queue, not drop.
    assert retry == pts
    assert written == 0
    assert exp._auth_hint_logged is True
    # Second 401 must not reset / re-log (idempotent flag).
    await exp._write_batch_with_semaphore(_Auth401(), pts, 1, 1)
    assert exp._auth_hint_logged is True


def test_is_unprocessable_classification():
    exp = _exp()
    # Text-based detection
    assert exp._is_unprocessable(RuntimeError("x"), "unprocessable entity") is True
    assert exp._is_unprocessable(RuntimeError("x"), "unable to parse points") is True
    assert exp._is_unprocessable(RuntimeError("x"), "server busy") is False
    # Status-based detection (4xx = permanent payload problem, 5xx = retry)
    e422 = type("E", (Exception,), {})()
    e422.status = 422
    assert exp._is_unprocessable(e422, "") is True
    e503 = type("E", (Exception,), {})()
    e503.status = 503
    assert exp._is_unprocessable(e503, "") is False


async def test_write_batch_drops_unprocessable_batch():
    # A permanent 4xx rejection on a single point must be DROPPED (not re-queued,
    # not counted as written) and tallied — not retried forever (which would wedge
    # the flush loop).
    exp = _exp()

    class _Unproc:
        async def write(self, bucket, org, record, write_precision=None):
            raise RuntimeError("422 Unprocessable Entity: unable to parse")

    pts = [exp._metric_to_point(_m())]
    retry, written, _ = await exp._write_batch_with_semaphore(_Unproc(), pts, 1, 1)
    assert retry == []
    assert written == 0
    assert exp._rejected_points == 1


async def test_write_batch_bisects_to_isolate_one_bad_point():
    # A single poisoned point in an otherwise-good batch must NOT take the whole
    # batch down: bisection writes every good point exactly once and drops only
    # the offending one.
    exp = _exp()

    class _BisectApi:
        def __init__(self):
            self.written = []

        async def write(self, bucket, org, record, write_precision=None):
            lines = record if isinstance(record, list) else [record]
            # InfluxDB rejects the whole payload if it contains the bad line.
            if any("host=BAD" in ln for ln in lines):
                raise RuntimeError("422 Unprocessable Entity: unable to parse")
            self.written.extend(lines)

    good = [exp._metric_to_point(_m(value=float(i))) for i in range(8)]
    bad = exp._metric_to_point(
        Metric(
            name="gpu_temp", value=1.0, timestamp=datetime.now(UTC), tags={"host": "BAD"}, unit="C"
        )
    )
    pts = good[:4] + [bad] + good[4:]  # 9 points, poison in the middle
    api = _BisectApi()
    retry, written, _ = await exp._write_batch_with_semaphore(api, pts, 1, 1)
    assert retry == []
    assert written == 8  # all good points written, exactly once each
    assert exp._rejected_points == 1  # only the bad point dropped
    assert len(api.written) == 8
    assert not any("host=BAD" in ln for ln in api.written)


# --------------------------------------------------------------------------
# _flush_buffer: multi-batch success, latency moving average, periodic log
# --------------------------------------------------------------------------


async def test_flush_drops_unprocessable_not_counted_as_written():
    # A dropped-unprocessable batch must NOT inflate total_points_written /
    # successful_batches, and must NOT be re-queued — only _rejected_points.
    exp = _exp(batch_size=1)

    class _Unproc(_FakeWriteApi):
        async def write(self, bucket, org, record, write_precision=None):
            raise RuntimeError("422 Unprocessable Entity: unable to parse")

    exp._write_api = _Unproc()
    await exp.write([_m()])
    await exp._flush_buffer()
    assert exp._rejected_points == 1
    assert exp._total_points_written == 0  # not counted as written
    assert exp._successful_batches == 0  # not counted as a successful batch
    assert exp.buffer_size == 0  # dropped, not re-queued


async def test_flush_multi_batch_success_and_latency_average():
    exp = _exp(batch_size=1)  # one point per batch -> several batches
    exp._write_api = _FakeWriteApi()
    await exp.write([_m(value=float(i)) for i in range(5)])
    await exp._flush_buffer()
    assert len(exp._write_api.written) == 5
    assert exp.buffer_size == 0
    assert exp._successful_batches == 5
    assert exp._last_write_time is not None
    first_latency = exp._write_latency_ms

    # A second flush exercises the moving-average "else" branch (prev != 0).
    await exp.write([_m(value=99.0)])
    await exp._flush_buffer()
    assert exp._write_latency_ms >= 0
    assert exp._total_points_written == 6
    # first_latency was set from the first flush; just assert it was recorded.
    assert first_latency >= 0


async def test_flush_periodic_info_log_at_tenth_write():
    exp = _exp(batch_size=1000)
    exp._write_api = _FakeWriteApi()
    exp._write_count = 9  # next successful flush bumps to 10 -> INFO summary path
    await exp.write([_m()])
    await exp._flush_buffer()
    assert exp._write_count == 10


async def test_flush_empty_buffer_is_noop():
    exp = _exp()
    exp._write_api = _FakeWriteApi()
    await exp._flush_buffer()  # nothing buffered -> early return
    assert exp.buffer_size == 0


# --------------------------------------------------------------------------
# _flush_buffer: consecutive-failure reconnect threshold
# --------------------------------------------------------------------------


async def test_three_consecutive_full_failures_force_reset():
    exp = _exp(batch_size=1)
    exp._client = _FakeInflux()
    exp._write_api = _FakeWriteApi(fail=True)
    # Each full-failure flush bumps the counter; the 3rd trips the threshold.
    for _ in range(3):
        await exp.write([_m()])
        await exp._flush_buffer()
    # Threshold reached -> _force_reset_connection nulls the connection.
    assert exp._write_api is None
    assert exp._client is None
    assert exp._consecutive_batch_failures == 0


# --------------------------------------------------------------------------
# _flush_buffer: gather-exception branch and outer-exception branch
# --------------------------------------------------------------------------


async def test_flush_gather_returns_exception_branch():
    exp = _exp(batch_size=1)
    exp._write_api = _FakeWriteApi()
    await exp.write([_m(), _m()])

    async def _boom(*_a, **_k):
        raise RuntimeError("batch coroutine blew up")

    # Returns an awaitable that raises -> gather(return_exceptions=True) yields
    # the Exception object -> isinstance(result, Exception) branch.
    exp._write_batch_with_semaphore = _boom
    await exp._flush_buffer()
    assert exp._failed_batches == 2
    # Failed points are re-queued.
    assert exp.buffer_size == 2


async def test_flush_outer_exception_requeues_all():
    exp = _exp(batch_size=1)
    exp._write_api = _FakeWriteApi()
    await exp.write([_m(), _m()])

    def _raise_sync(*_a, **_k):
        raise RuntimeError("blew up building tasks")

    # Raising synchronously when building the task list trips the outer except.
    exp._write_batch_with_semaphore = _raise_sync
    await exp._flush_buffer()
    assert exp._consecutive_batch_failures >= 1
    assert exp.buffer_size == 2  # all points re-added


# --------------------------------------------------------------------------
# _re_add_points: overflow drop counting
# --------------------------------------------------------------------------


async def test_re_add_points_overflow_drops_oldest():
    exp = _exp(batch_size=1)  # max buffer 200
    # Pre-fill buffer close to cap.
    await exp.write([_m(value=float(i)) for i in range(150)])
    # Re-add more than remaining capacity -> overflow drop.
    extra = [exp._metric_to_point(_m(value=float(i))) for i in range(100)]
    await exp._re_add_points(extra)
    assert exp.buffer_size == 200
    assert exp.dropped_points == 50


# --------------------------------------------------------------------------
# connect(): retry-then-degrade, and success
# --------------------------------------------------------------------------


async def test_connect_retries_then_degrades(monkeypatch):
    monkeypatch.setattr(
        "src.exporters.influxdb.InfluxDBClientAsync", lambda **kw: _FakeInflux(ping_ok=False)
    )

    # Avoid real sleeping between retries (must NOT call asyncio.sleep itself).
    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("src.exporters.influxdb.asyncio.sleep", _no_sleep)
    exp = _exp()
    with pytest.raises(ConnectionError):
        await exp.connect(max_retries=3, initial_delay=0.01, max_delay=0.02)
    # Flush loop still started so it can reconnect later.
    assert exp._running is True
    await _stop(exp)


async def test_connect_success(monkeypatch):
    monkeypatch.setattr(
        "src.exporters.influxdb.InfluxDBClientAsync", lambda **kw: _FakeInflux(ping_ok=True)
    )
    exp = _exp()
    await exp.connect(max_retries=1)
    try:
        assert exp._client is not None
        assert exp._write_api is not None
    finally:
        await _stop(exp)


# --------------------------------------------------------------------------
# _reconnect(): closes existing client, timeout branch, generic-error branch
# --------------------------------------------------------------------------


async def test_reconnect_closes_existing_client(monkeypatch):
    monkeypatch.setattr(
        "src.exporters.influxdb.InfluxDBClientAsync", lambda **kw: _FakeInflux(ping_ok=True)
    )
    exp = _exp()
    old = _FakeInflux()
    exp._client = old
    assert await exp._reconnect() is True
    assert old.closed is True  # old client was closed first
    assert exp._reconnect_count == 1


async def test_reconnect_ping_timeout_branch(monkeypatch):
    class _TOInflux:
        def __init__(self, **kw):
            pass

        async def ping(self):
            raise TimeoutError()  # asyncio.wait_for re-raises -> except TimeoutError

        async def close(self):
            return None

    monkeypatch.setattr("src.exporters.influxdb.InfluxDBClientAsync", _TOInflux)
    exp = _exp()
    assert await exp._reconnect() is False
    assert exp._client is None
    assert exp._write_api is None


async def test_reconnect_generic_error_branch(monkeypatch):
    class _ErrInflux:
        def __init__(self, **kw):
            pass

        async def ping(self):
            raise RuntimeError("connection refused")

        async def close(self):
            return None

    monkeypatch.setattr("src.exporters.influxdb.InfluxDBClientAsync", _ErrInflux)
    exp = _exp()
    assert await exp._reconnect() is False
    assert exp._client is None


# --------------------------------------------------------------------------
# _flush_loop(): connected, disconnected->reconnect, backoff, error, cancel
# --------------------------------------------------------------------------


async def test_flush_loop_connected_drains_then_cancel():
    exp = _exp(flush_interval=0.01)
    exp._write_api = _FakeWriteApi()
    exp._client = _FakeInflux()
    exp._running = True
    await exp.write([_m(), _m()])
    task = asyncio.create_task(exp._flush_loop())
    # Give the loop time to wake on the flush event and drain the buffer.
    for _ in range(50):
        if exp.buffer_size == 0:
            break
        await asyncio.sleep(0.01)
    assert exp.buffer_size == 0
    # Cancelling drives the CancelledError -> break path.
    exp._running = False
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_flush_loop_reconnect_success_then_flush():
    exp = _exp()
    exp._reconnect_delay = 0.01
    exp._write_api = None
    exp._running = True
    await exp.write([_m()])

    async def _fake_reconnect():
        exp._write_api = _FakeWriteApi()
        exp._running = False  # exit after one successful reconnect+flush
        return True

    exp._reconnect = _fake_reconnect
    await exp._flush_loop()
    assert exp.buffer_size == 0  # drained after reconnect


async def test_flush_loop_reconnect_failure_backoff():
    exp = _exp()
    exp._reconnect_delay = 0.01
    exp._write_api = None
    exp._running = True

    async def _fail_reconnect():
        exp._running = False
        return False

    exp._reconnect = _fail_reconnect
    await exp._flush_loop()
    # Backoff doubled on failed reconnect.
    assert exp._reconnect_delay > 0.01


async def test_flush_loop_generic_error_is_caught():
    exp = _exp(flush_interval=0.01)
    exp._write_api = _FakeWriteApi()
    exp._running = True

    async def _boom():
        exp._running = False
        raise RuntimeError("flush exploded")

    exp._flush_buffer = _boom
    # Should swallow the error, sleep, and exit (running cleared) without raising.
    await exp._flush_loop()


# --------------------------------------------------------------------------
# write_immediate
# --------------------------------------------------------------------------


async def test_write_immediate_not_connected():
    assert await _exp().write_immediate([_m()]) is False


async def test_write_immediate_success_and_failure():
    exp = _exp()
    exp._write_api = _FakeWriteApi()
    assert await exp.write_immediate([_m(), _m()]) is True
    assert len(exp._write_api.written) == 2
    exp._write_api = _FakeWriteApi(fail=True)
    assert await exp.write_immediate([_m()]) is False


# --------------------------------------------------------------------------
# write(): buffer overflow drop counting on the write path
# --------------------------------------------------------------------------


async def test_write_overflow_drops_oldest():
    exp = _exp(batch_size=1)  # max buffer 200
    await exp.write([_m(value=float(i)) for i in range(260)])
    assert exp.buffer_size == 200
    assert exp.dropped_points == 60


# --------------------------------------------------------------------------
# close(): cancels the flush task and flushes remaining buffer
# --------------------------------------------------------------------------


async def test_close_cancels_task_and_flushes():
    exp = _exp()
    exp._client = _FakeInflux()
    api = _FakeWriteApi()
    exp._write_api = api
    exp._running = True
    exp._flush_task = asyncio.create_task(asyncio.sleep(100))
    await exp.write([_m(), _m()])
    await exp.close()
    assert exp._running is False
    assert exp._client is None
    assert exp._write_api is None
    # Buffered points were flushed during close.
    assert len(api.written) == 2


# --------------------------------------------------------------------------
# health_check(): exception branch
# --------------------------------------------------------------------------


async def test_health_check_exception_branch():
    exp = _exp()

    class _BadPing:
        async def ping(self):
            raise RuntimeError("ping blew up")

    exp._client = _BadPing()
    ok, msg = await exp.health_check()
    assert ok is False
    assert "RuntimeError" in msg


# --------------------------------------------------------------------------
# get_health_metrics(): branches around last_write/dropped/failures/consecutive
# --------------------------------------------------------------------------


def test_health_metrics_healthy_with_recent_write():
    exp = _exp()
    exp._write_api = object()
    exp._last_write_time = datetime.now(UTC)
    exp._successful_batches = 100
    hm = exp.get_health_metrics()
    assert hm["connected"] is True
    assert hm["is_healthy"] is True
    assert hm["health_details"]["write_api_alive"] is True


def test_health_metrics_unhealthy_performance_and_consecutive():
    exp = _exp()
    exp._write_api = object()
    exp._last_write_time = datetime.now(UTC)
    # Drive multiple performance checks to fail: latency, failures_24h, failed rate.
    exp._write_latency_ms = 9000
    exp._write_failures_24h = 100
    exp._successful_batches = 10
    exp._failed_batches = 90
    exp._consecutive_batch_failures = 4
    exp._reconnect_count = 2
    hm = exp.get_health_metrics()
    assert hm["is_healthy"] is False
    assert hm["failure_rate_pct"] == 90.0
    assert hm["health_details"]["consecutive_batch_failures"] == 4
    assert hm["health_details"]["reconnects"] == 2
    assert hm["last_write_time"] is not None
    assert hm["time_since_last_write_seconds"] is not None


async def test_reconnect_ping_false_branch(monkeypatch):
    monkeypatch.setattr(
        "src.exporters.influxdb.InfluxDBClientAsync", lambda **kw: _FakeInflux(ping_ok=False)
    )
    exp = _exp()
    assert await exp._reconnect() is False
    assert exp._client is None


async def test_flush_without_write_api_readds_points():
    exp = _exp(batch_size=1000)
    await exp.write([_m()])
    exp._write_api = None
    await exp._flush_buffer()  # not connected -> re-add, points preserved
    assert exp.buffer_size == 1


async def test_health_check_connected_and_ping_false():
    exp = _exp()
    exp._client = _FakeInflux(ping_ok=True)
    ok, msg = await exp.health_check()
    assert ok is True
    assert "Connected" in msg
    exp._client = _FakeInflux(ping_ok=False)
    ok2, _ = await exp.health_check()
    assert ok2 is False


def test_is_connected_property():
    exp = _exp()
    assert exp.is_connected is False
    exp._client = _FakeInflux()
    assert exp.is_connected is True


async def test_query_not_connected_and_results():
    exp = _exp()
    with pytest.raises(RuntimeError):
        await exp.query("from(bucket)")

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

    exp._client = _Client()
    assert await exp.query("from(bucket)") == [{"_time": "t", "_value": 1}]


def test_health_metrics_no_writes_yet():
    exp = _exp()
    exp._write_api = object()  # alive but nothing written yet
    hm = exp.get_health_metrics()
    # pipeline_recent is True when no write has happened yet.
    assert hm["connected"] is True
    assert hm["last_write_time"] is None
    assert hm["time_since_last_write_seconds"] is None
