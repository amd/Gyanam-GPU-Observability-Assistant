# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Streaming + lifecycle coverage for SSEConnection / SSEManager.

Complements test_sse_subscriber.py (which covers _process_event classification)
by driving the connection loop, the SSE stream parser, the activity-timeout
line iterator, and start/stop lifecycle for both SSEConnection and SSEManager.
"""

import asyncio
from types import SimpleNamespace

import pytest
from src.redfish import sse_subscriber
from src.redfish.sse_subscriber import SSEConnection, SSEManager

BASE = "https://gpu-a"
SSE_URL = f"{BASE}/redfish/v1/EventService/SSE"


class _FakeRepo:
    def __init__(self):
        self.status_updates = 0

    async def update_poll_status(self, *a, **k):
        self.status_updates += 1


def _target():
    t = SimpleNamespace(id=1, name="gpu-a", host="10.0.0.5")
    t.base_url = BASE
    t.verify_ssl = False
    return t


def _conn(token="tok", repo=None):
    return SSEConnection(
        target=_target(),
        repository=repo or _FakeRepo(),
        username="u",
        password="p",
        token=token,
        reconnect_delay=0,
        max_reconnect_delay=0,
    )


# ---- _stream_events: full parse + dispatch via httpx_mock --------------------


async def test_stream_events_parses_and_dispatches(httpx_mock):
    # event:/data: lines, an empty line terminating a MetricReport event, plus a
    # keep-alive comment line and a non-MetricReport event that must be ignored.
    body = (
        b":keep-alive\n"
        b"event: Other\n"
        b"data: ignored\n"
        b"\n"
        b"event: MetricReport\n"
        b'data: {"@odata.id": "/redfish/v1/TelemetryService/MetricReports/OAM_ProcessorMetrics_0"}\n'
        b"\n"
    )
    httpx_mock.add_response(
        method="GET",
        url=SSE_URL,
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=body,
    )
    repo = _FakeRepo()
    conn = _conn(repo=repo)
    conn._running = True  # parse loop checks _running per line
    conn._current_delay = 99  # should be reset once a real event arrives
    q = asyncio.Queue()

    await conn._stream_events(q)

    result = q.get_nowait()
    assert result.data[0][0] == "processor"
    assert result.collection_method == "sse"
    # Backoff reset + counters cleared by the productive MetricReport event.
    assert conn._current_delay == conn.reconnect_delay
    assert conn._consecutive_errors == 0
    assert repo.status_updates == 1


async def test_stream_events_basic_auth_path(httpx_mock):
    # token=None exercises the httpx.BasicAuth branch; empty stream ends cleanly.
    httpx_mock.add_response(
        method="GET",
        url=SSE_URL,
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=b"",
    )
    conn = _conn(token=None)
    q = asyncio.Queue()
    await conn._stream_events(q)
    assert q.empty()


async def test_stream_events_non_200_raises(httpx_mock):
    httpx_mock.add_response(method="GET", url=SSE_URL, status_code=503)
    conn = _conn()
    with pytest.raises(RuntimeError):
        await conn._stream_events(asyncio.Queue())


async def test_stream_events_stops_when_not_running(httpx_mock):
    # _running flipped off mid-stream must break the parse loop immediately.
    body = b'event: MetricReport\ndata: {"@odata.id": "/All"}\n\n'
    httpx_mock.add_response(
        method="GET",
        url=SSE_URL,
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=body,
    )
    conn = _conn()
    conn._running = False
    q = asyncio.Queue()
    await conn._stream_events(q)
    assert q.empty()  # loop broke before the event was dispatched


# ---- _iter_lines_with_timeout -----------------------------------------------


class _FakeResp:
    def __init__(self, lines, delay=0.0):
        self._lines = lines
        self._delay = delay

    def aiter_lines(self):
        async def gen():
            for line in self._lines:
                if self._delay:
                    await asyncio.sleep(self._delay)
                yield line

        return gen()


async def test_iter_lines_stop_async_iteration():
    conn = _conn()
    resp = _FakeResp(["a", "b", "c"])
    got = [line async for line in conn._iter_lines_with_timeout(resp)]
    assert got == ["a", "b", "c"]


async def test_iter_lines_timeout_raises():
    conn = _conn()
    conn.STREAM_ACTIVITY_TIMEOUT = 0.01  # instance override of class default
    resp = _FakeResp(["slow"], delay=1.0)
    with pytest.raises(TimeoutError):
        async for _ in conn._iter_lines_with_timeout(resp):
            pass


# ---- _connection_loop: exception + clean-close backoff ----------------------


async def test_connection_loop_exception_then_clean_close():
    conn = _conn()
    conn._running = True
    calls = []

    async def fake_stream(q):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")  # except branch -> backoff sleep
        conn._running = False  # second iteration returns cleanly then stops

    conn._stream_events = fake_stream
    await conn._connection_loop(asyncio.Queue())
    assert len(calls) == 2
    assert conn._consecutive_errors == 2


async def test_connection_loop_cancelled_breaks():
    conn = _conn()
    conn._running = True

    async def fake_stream(q):
        raise asyncio.CancelledError()

    conn._stream_events = fake_stream
    await conn._connection_loop(asyncio.Queue())  # CancelledError -> break, no raise


# ---- start / stop lifecycle -------------------------------------------------


async def test_start_stop_lifecycle():
    conn = _conn()
    gate = asyncio.Event()

    async def fake_loop(q):
        await gate.wait()

    conn._connection_loop = fake_loop
    assert conn.is_running is False

    await conn.start(asyncio.Queue())
    assert conn.is_running is True
    task = conn._task

    # Starting again is a no-op (already running).
    await conn.start(asyncio.Queue())
    assert conn._task is task

    await conn.stop()
    assert conn.is_running is False
    assert conn._task is None


async def test_stop_without_task_is_safe():
    conn = _conn()
    await conn.stop()  # no task -> no error
    assert conn._task is None


# ---- SSEManager lifecycle + _start_connection -------------------------------


async def test_manager_start_stop(repo):
    mgr = SSEManager(repository=repo, result_queue=asyncio.Queue())
    assert mgr._running is False
    await mgr.start()
    assert mgr._running is True
    # Starting again is a no-op.
    await mgr.start()
    await asyncio.sleep(0)  # let the sync loop run its first (empty) reconcile
    await mgr.stop()
    assert mgr._running is False
    assert mgr.active_connections == 0


async def test_manager_start_connection(repo, monkeypatch):
    # Avoid any real network: SSEConnection.start becomes a no-op flag-setter.
    async def fake_start(self, result_queue):
        self._running = True

    monkeypatch.setattr(sse_subscriber.SSEConnection, "start", fake_start)

    mgr = SSEManager(repository=repo, result_queue=asyncio.Queue())
    target = await repo.create_target(
        name="gpu-a",
        host="10.0.0.5",
        username="u",
        password="p",
        connection_mode="sse",
        sse_endpoint="/sse",
    )
    await mgr._start_connection(target)
    assert target.id in mgr._connections
    assert mgr.active_connections == 1
