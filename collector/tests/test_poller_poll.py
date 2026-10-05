# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for RedfishPoller.poll_single / _poll_target (GET-first collection).

A pre-set ``token`` on the target makes RedfishClient.connect() skip the
SessionService POST, so only the metric-report GETs need mocking.
"""

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


def _poller(repo, reports):
    return RedfishPoller(repository=repo, poll_interval=300, metric_reports=reports)


async def test_poll_single_target_not_found(repo):
    assert await _poller(repo, [_ALL]).poll_single(999999) is None


async def test_poll_single_get_first_success(repo, httpx_mock):
    target = await _target(repo)
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}{ALL_URI}",
        json={"MetricValues": [{"MetricProperty": "/x#T", "MetricValue": "1"}]},
    )
    poller = _poller(repo, [_ALL])
    try:
        result = await poller.poll_single(target.id)
        assert result.success is True
        assert result.collection_method == "get"
        assert result.data and result.data[0][0] == "comprehensive"
        assert result.content == b""  # raw bytes dropped once parsed
    finally:
        await poller.stop()


async def test_poll_target_partial_reports(repo, httpx_mock):
    target = await _target(repo)
    httpx_mock.add_response(method="GET", url=f"{BASE}{ALL_URI}", json={"MetricValues": []})
    httpx_mock.add_response(method="GET", url=f"{BASE}{MEM_URI}", status_code=404)
    poller = _poller(repo, [_ALL, _MEM])
    try:
        result = await poller._poll_target(target)
        assert result.success is True  # at least one report came back
        assert len(result.data) == 1  # only the All report (Mem 404'd)
        # Partial collection is now OBSERVABLE, not silently green: 1 of 2 reports.
        assert result.reports_succeeded == 1
        assert result.reports_expected == 2
    finally:
        await poller.stop()


async def test_run_poll_enqueues_and_records_status(repo, httpx_mock):
    captured = []
    target = await _target(repo)
    httpx_mock.add_response(method="GET", url=f"{BASE}{ALL_URI}", json={"MetricValues": []})
    poller = RedfishPoller(
        repository=repo, poll_interval=300, metric_reports=[_ALL], callback=captured.append
    )
    try:
        await poller._run_poll(target)
        assert poller._result_queue.qsize() == 1
        assert poller._pending_status[target.id][0] == "success"
        assert len(captured) == 1  # callback fired
        assert poller._polls_completed == 1
    finally:
        await poller.stop()


async def test_flush_pending_status_commits(repo):
    target = await repo.create_target(name="x", host="hx", username="u", password="p")
    poller = _poller(repo, [])
    poller._pending_status = {target.id: ("error", "boom")}
    await poller._flush_pending_status()
    assert poller._pending_status == {}
    assert (await repo.get_target(target.id)).last_poll_status == "error"


async def test_flush_pending_status_empty_noop(repo):
    poller = _poller(repo, [])
    await poller._flush_pending_status()  # must not raise
    assert poller._pending_status == {}


async def test_poll_single_reuses_cached_client(repo, httpx_mock):
    target = await _target(repo)
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}{ALL_URI}",
        json={"MetricValues": []},
        is_reusable=True,
    )
    poller = _poller(repo, [_ALL])
    try:
        await poller.poll_single(target.id)
        await poller.poll_single(target.id)
        # Second poll hit the cached client (one miss, one hit).
        assert poller._client_cache_hits >= 1
    finally:
        await poller.stop()


async def test_poll_target_task_fallback(repo, httpx_mock):
    target = await _target(repo)  # default telemetry_endpoint = collect action
    endpoint = target.telemetry_endpoint
    task = "/redfish/v1/TaskService/Tasks/7"
    # GET report fails -> falls back to task-based collection.
    httpx_mock.add_response(method="GET", url=f"{BASE}{ALL_URI}", status_code=404, is_reusable=True)
    httpx_mock.add_response(
        method="POST", url=f"{BASE}{endpoint}", status_code=202, headers={"Location": task}
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}{task}",
        status_code=200,
        is_reusable=True,
        json={"Id": "7", "TaskState": "Completed", "PercentComplete": 100, "@odata.id": task},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}{task}/attachment",
        status_code=200,
        content=b"BLOB",
        headers={"Content-Type": "application/octet-stream"},
    )
    httpx_mock.add_response(method="DELETE", url=f"{BASE}{task}", status_code=200, is_optional=True)
    poller = RedfishPoller(
        repository=repo, poll_interval=300, metric_reports=[_ALL], task_poll_interval=0
    )
    try:
        result = await poller._poll_target(target)
        assert result.success is True
        assert result.collection_method == "task" and result.content == b"BLOB"
    finally:
        await poller.stop()


async def test_get_results_drains_queue(repo):
    poller = _poller(repo, [])
    poller._result_queue.put_nowait("r1")
    poller._result_queue.put_nowait("r2")
    results = await poller.get_results(timeout=0.05)
    assert results == ["r1", "r2"]


async def test_get_results_empty_returns_empty(repo):
    results = await _poller(repo, []).get_results(timeout=0.01)
    assert results == []


async def test_schedule_due_polls_new_then_due(repo, httpx_mock):
    import asyncio

    target = await _target(repo)
    httpx_mock.add_response(
        method="GET", url=f"{BASE}{ALL_URI}", json={"MetricValues": []}, is_reusable=True
    )
    poller = RedfishPoller(repository=repo, poll_interval=300, metric_reports=[_ALL])
    poller._running = True
    try:
        # First pass: a brand-new target is scheduled (staggered), not dispatched.
        await poller._schedule_due_polls()
        assert target.id in poller._next_poll_time
        assert poller._polls_started == 0

        # Force it due -> second pass dispatches a poll task. Schedule uses a
        # monotonic clock, so set a time already in the past on that clock.
        import time as _time

        poller._next_poll_time[target.id] = _time.monotonic() - 1
        await poller._schedule_due_polls()
        assert poller._polls_started == 1
        await asyncio.gather(*poller._inflight.values(), return_exceptions=True)
    finally:
        await poller.stop()


async def test_start_and_stop_lifecycle(repo):
    poller = _poller(repo, [])
    await poller.start()
    assert poller._running is True
    assert poller._poll_task is not None
    await poller.stop()
    assert poller._running is False
