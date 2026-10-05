# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Additional coverage for RedfishPoller uncovered paths.

Covers staggered-schedule init, new-target scheduling, the client cache
(get/evict/close), _run_poll result enqueue + queue-full drop, the failure /
circuit-breaker refresh path, _schedule_due_polls stale cleanup + eviction,
get_results, the status writer loop, and the GET-first large-report / parse-error
branches.
"""

import asyncio
import contextlib
import time

import pytest
from src.config import MetricReportConfig
from src.redfish.poller import RedfishPoller

BASE = "https://bmc.test"
ALL_URI = "/redfish/v1/TelemetryService/MetricReports/All"
MEM_URI = "/redfish/v1/TelemetryService/MetricReports/Mem"

_ALL = MetricReportConfig(uri=ALL_URI, report_type="comprehensive")
_MEM = MetricReportConfig(uri=MEM_URI, report_type="memory")


async def _target(repo, **kw):
    kw.setdefault("name", "n")
    kw.setdefault("host", "bmc.test")
    kw.setdefault("username", "u")
    kw.setdefault("password", "p")
    kw.setdefault("token", "tok")  # skip session auth
    return await repo.create_target(**kw)


def _poller(repo, reports=None, **kw):
    return RedfishPoller(repository=repo, poll_interval=300, metric_reports=reports or [], **kw)


# --------------------------------------------------------------------------
# start() guard + is_running
# --------------------------------------------------------------------------


async def test_start_already_running_is_noop(repo):
    poller = _poller(repo)
    poller._running = True
    await poller.start()  # early return -> no tasks created
    assert poller._poll_task is None
    assert poller._status_writer_task is None


async def test_is_running_property(repo):
    poller = _poller(repo)
    assert poller.is_running is False
    poller._running = True
    assert poller.is_running is True


# --------------------------------------------------------------------------
# _initialize_staggered_schedule
# --------------------------------------------------------------------------


async def test_initialize_staggered_no_targets(repo):
    poller = _poller(repo)
    await poller._initialize_staggered_schedule()  # repo empty -> early return
    assert poller._next_poll_time == {}


async def test_initialize_staggered_all_sse(repo):
    await _target(repo, name="sse1", connection_mode="sse", sse_endpoint="/x")
    poller = _poller(repo)
    await poller._initialize_staggered_schedule()  # only SSE -> nothing to poll
    assert poller._next_poll_time == {}


async def test_initialize_staggered_schedules_targets(repo):
    ids = []
    for i in range(5):
        t = await _target(repo, name=f"t{i}", host=f"host{i}.test")
        ids.append(t.id)
    poller = _poller(repo)
    # Pre-schedule one so the "already scheduled" skip branch is exercised.
    poller._next_poll_time[ids[0]] = 123.0
    await poller._initialize_staggered_schedule()
    for tid in ids:
        assert tid in poller._next_poll_time
    assert poller._next_poll_time[ids[0]] == 123.0  # untouched


# --------------------------------------------------------------------------
# _schedule_new_targets
# --------------------------------------------------------------------------


async def test_schedule_new_targets_empty_noop(repo):
    poller = _poller(repo)
    await poller._schedule_new_targets([])
    assert poller._next_poll_time == {}


async def test_schedule_new_targets_assigns_times(repo):
    t1 = await _target(repo, name="a", host="a.test")
    t2 = await _target(repo, name="b", host="b.test")
    poller = _poller(repo)
    now = time.monotonic()
    await poller._schedule_new_targets([t1, t2])
    assert t1.id in poller._next_poll_time and t2.id in poller._next_poll_time
    assert poller._next_poll_time[t1.id] >= now


# --------------------------------------------------------------------------
# _get_client cache + _close_client
# --------------------------------------------------------------------------


async def test_get_client_cache_hit(repo):
    target = await _target(repo)
    poller = _poller(repo)
    sentinel = object()
    poller._clients[target.id] = sentinel  # type: ignore[assignment]
    got = await poller._get_client(target, "p", "tok")
    assert got is sentinel
    assert poller._client_cache_hits == 1


async def test_get_client_connect_failure_returns_none(repo, monkeypatch):
    target = await _target(repo)
    poller = _poller(repo)

    class _BadClient:
        def __init__(self, *a, **k):
            pass

        async def connect(self):
            raise RuntimeError("cannot connect")

        async def close(self):
            return None

    monkeypatch.setattr("src.redfish.poller.RedfishClient", _BadClient)
    got = await poller._get_client(target, "p", "tok")
    assert got is None
    assert target.id not in poller._clients
    assert poller._client_cache_misses == 1


async def test_close_client_evicts(repo):
    target = await _target(repo)
    poller = _poller(repo)

    closed = {"v": False}

    class _C:
        async def close(self):
            closed["v"] = True

    poller._clients[target.id] = _C()  # type: ignore[assignment]
    await poller._close_client(target.id)
    assert target.id not in poller._clients
    assert closed["v"] is True
    # Closing an unknown id is a no-op.
    await poller._close_client(999999)


# --------------------------------------------------------------------------
# _run_poll: queue-full drop, exception, callback error, CB refresh
# --------------------------------------------------------------------------


async def test_run_poll_queue_full_drops(repo, httpx_mock):
    target = await _target(repo)
    httpx_mock.add_response(method="GET", url=f"{BASE}{ALL_URI}", json={"MetricValues": []})
    poller = _poller(repo, [_ALL])
    poller._result_queue = asyncio.Queue(maxsize=1)
    poller._result_queue.put_nowait("prefill")  # queue already full
    try:
        await poller._run_poll(target)
        assert poller._polls_dropped_queue_full == 1
        assert poller._polls_completed == 1
    finally:
        await poller.stop()


async def test_run_poll_poll_target_exception(repo, monkeypatch):
    target = await _target(repo)
    poller = _poller(repo, [_ALL])

    async def _boom(_t):
        raise RuntimeError("explode")

    monkeypatch.setattr(poller, "_poll_target", _boom)
    await poller._run_poll(target)  # swallowed, logged
    assert poller._polls_completed == 1
    assert poller._result_queue.qsize() == 0


async def test_run_poll_callback_error_is_caught(repo, httpx_mock):
    target = await _target(repo)
    httpx_mock.add_response(method="GET", url=f"{BASE}{ALL_URI}", json={"MetricValues": []})

    def _cb(_r):
        raise ValueError("callback boom")

    poller = _poller(repo, [_ALL], callback=_cb)
    try:
        await poller._run_poll(target)  # callback raises -> caught
        assert poller._pending_status[target.id][0] == "success"
    finally:
        await poller.stop()


async def test_run_poll_failure_refreshes_schedule(repo, httpx_mock):
    # GET fails and task fallback fails -> result.success False -> CB refresh path.
    target = await _target(repo, telemetry_endpoint="/redfish/v1/X/Actions/Collect")
    httpx_mock.add_response(method="GET", url=f"{BASE}{ALL_URI}", status_code=500, is_reusable=True)
    httpx_mock.add_response(
        method="POST", url=f"{BASE}/redfish/v1/X/Actions/Collect", status_code=500, is_reusable=True
    )
    poller = _poller(repo, [_ALL], task_poll_interval=0)
    try:
        await poller._run_poll(target)
        assert poller._pending_status[target.id][0] == "error"
        assert target.id in poller._next_poll_time  # schedule refreshed
    finally:
        await poller.stop()


# --------------------------------------------------------------------------
# _on_poll_task_done exception logging
# --------------------------------------------------------------------------


async def test_on_poll_task_done_logs_crash(repo):
    poller = _poller(repo)

    async def _crash():
        raise RuntimeError("task crashed")

    task = asyncio.create_task(_crash())
    poller._inflight[42] = task
    with pytest.raises(RuntimeError):
        await task
    poller._on_poll_task_done(task)  # removes from inflight, logs exception
    assert 42 not in poller._inflight


# --------------------------------------------------------------------------
# _flush_pending_status error re-merge
# --------------------------------------------------------------------------


async def test_flush_pending_status_error_remerges(repo, monkeypatch):
    target = await _target(repo)
    poller = _poller(repo)
    poller._pending_status = {target.id: ("error", "boom")}

    async def _fail(_updates):
        raise RuntimeError("db locked")

    monkeypatch.setattr(repo, "update_poll_status_batch", _fail)
    await poller._flush_pending_status()
    # Failed write is re-merged for retry.
    assert poller._pending_status.get(target.id) == ("error", "boom")


# --------------------------------------------------------------------------
# _status_writer_loop
# --------------------------------------------------------------------------


async def test_status_writer_loop_flushes(repo, monkeypatch):
    monkeypatch.setattr("src.redfish.poller._STATUS_FLUSH_INTERVAL_S", 0.01)
    target = await _target(repo)
    poller = _poller(repo)
    poller._running = True
    poller._pending_status = {target.id: ("success", None)}
    task = asyncio.create_task(poller._status_writer_loop())
    await asyncio.sleep(0.05)
    # Loop drained the pending status into the DB.
    assert poller._pending_status == {}
    poller._running = False
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# --------------------------------------------------------------------------
# stop() with in-flight polls + cached clients + eviction tasks
# --------------------------------------------------------------------------


async def test_stop_waits_for_inflight_and_closes_clients(repo):
    poller = _poller(repo)
    done = {"inflight": False, "closed": False, "evict": False}

    async def _slow():
        await asyncio.sleep(0.01)
        done["inflight"] = True

    poller._inflight[1] = asyncio.create_task(_slow())

    class _C:
        async def close(self):
            done["closed"] = True

    poller._clients[2] = _C()  # type: ignore[assignment]

    async def _evict():
        await asyncio.sleep(0.01)
        done["evict"] = True

    poller._eviction_tasks.add(asyncio.create_task(_evict()))

    await poller.stop()
    assert done["inflight"] and done["closed"] and done["evict"]


# --------------------------------------------------------------------------
# _schedule_due_polls: stale cleanup + client eviction
# --------------------------------------------------------------------------


async def test_schedule_due_polls_no_targets(repo):
    poller = _poller(repo)
    poller._running = True
    await poller._schedule_due_polls()  # no enabled targets -> early return
    assert poller._polls_started == 0


async def test_schedule_due_polls_cleans_stale(repo):
    target = await _target(repo)
    poller = _poller(repo, [_ALL])
    poller._running = True
    # Seed schedule + a cached client for an id that no longer exists.
    stale_id = 999999

    class _C:
        async def close(self):
            return None

    poller._next_poll_time[stale_id] = time.monotonic() + 100
    poller._clients[stale_id] = _C()  # type: ignore[assignment]
    poller._client_locks[stale_id] = asyncio.Lock()

    await poller._schedule_due_polls()
    # Stale schedule entry removed; eviction scheduled for the stale client.
    assert stale_id not in poller._next_poll_time
    # The real target is new -> staggered, not dispatched this pass.
    assert target.id in poller._next_poll_time
    # Drain any eviction tasks.
    if poller._eviction_tasks:
        await asyncio.gather(*list(poller._eviction_tasks), return_exceptions=True)
    assert stale_id not in poller._clients


async def test_schedule_due_polls_skips_sse_and_inflight(repo, httpx_mock):
    await _target(repo, name="sse", connection_mode="sse", sse_endpoint="/x")
    direct = await _target(repo, name="d", host="d.test")
    poller = _poller(repo, [_ALL])
    poller._running = True
    # Mark the direct target as already in-flight so it is skipped too.
    poller._inflight[direct.id] = asyncio.create_task(asyncio.sleep(0))
    await poller._schedule_due_polls()
    assert poller._polls_started == 0
    await asyncio.gather(*poller._inflight.values(), return_exceptions=True)


# --------------------------------------------------------------------------
# _poll_target: connect-None failure result
# --------------------------------------------------------------------------


async def test_poll_target_client_none_returns_failure(repo, monkeypatch):
    target = await _target(repo)
    poller = _poller(repo, [_ALL])

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(poller, "_get_client", _none)
    result = await poller._poll_target(target)
    assert result.success is False
    assert "Failed to establish" in result.error_message


# --------------------------------------------------------------------------
# _poll_target: GET-first large-report (thread offload) + JSON parse error
# --------------------------------------------------------------------------


async def test_poll_target_large_report_thread_offload(repo, httpx_mock):
    target = await _target(repo)
    # Build a >256KB JSON payload to trigger the asyncio.to_thread parse branch.
    big_values = [{"MetricProperty": f"/x#M{i}", "MetricValue": str(i)} for i in range(20000)]
    httpx_mock.add_response(method="GET", url=f"{BASE}{ALL_URI}", json={"MetricValues": big_values})
    poller = _poller(repo, [_ALL])
    try:
        result = await poller._poll_target(target)
        assert result.success is True
        assert result.collection_method == "get"
        assert len(result.content) > 262144 or result.content == b""
    finally:
        await poller.stop()


async def test_poll_target_get_json_parse_error_falls_back(repo, httpx_mock):
    target = await _target(repo, telemetry_endpoint="/redfish/v1/X/Actions/Collect")
    # 200 + json content-type but invalid JSON body -> get_metric_report returns
    # success, but _fetch_report's json.loads raises -> report dropped -> task
    # fallback (which also fails here).
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}{ALL_URI}",
        status_code=200,
        content=b"{bad json",
        headers={"Content-Type": "application/json"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="POST", url=f"{BASE}/redfish/v1/X/Actions/Collect", status_code=500, is_reusable=True
    )
    poller = _poller(repo, [_ALL], task_poll_interval=0)
    try:
        result = await poller._poll_target(target)
        assert result.success is False  # parse failed + task fallback failed
    finally:
        await poller.stop()
