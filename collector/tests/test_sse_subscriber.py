# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for SSEConnection._process_event (report-type classification + queueing)."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from src.redfish.sse_subscriber import SSEConnection, SSEManager


class _FakeRepo:
    async def update_poll_status(self, *a, **k):
        return None


def _conn():
    target = SimpleNamespace(id=1, name="n", host="h")
    return SSEConnection(target=target, repository=_FakeRepo(), username="u", password="p")


@pytest.mark.parametrize(
    "odata,expected",
    [
        ("/redfish/v1/TelemetryService/MetricReports/OAM_ProcessorMetrics_0", "processor"),
        ("/redfish/v1/TelemetryService/MetricReports/OAM_MemoryMetrics_0", "memory"),
        ("/redfish/v1/TelemetryService/MetricReports/OAM_ProcessorPortMetrics_0", "interconnect"),
        ("/redfish/v1/TelemetryService/MetricReports/PlatformSensorsMetrics_0", "platform"),
        ("/redfish/v1/TelemetryService/MetricReports/HealthRollup", "health"),
        ("/redfish/v1/TelemetryService/MetricReports/All", "comprehensive"),
        ("/redfish/v1/TelemetryService/MetricReports/Unknown", "sse"),
    ],
)
async def test_process_event_report_type(odata, expected):
    conn = _conn()
    q = asyncio.Queue()
    await conn._process_event([json.dumps({"@odata.id": odata, "MetricValues": []})], q)
    result = q.get_nowait()
    assert result.data[0][0] == expected
    assert result.collection_method == "sse" and result.success is True


async def test_process_event_bad_json_drops():
    conn = _conn()
    q = asyncio.Queue()
    await conn._process_event(["{ not valid json"], q)
    assert q.empty()


async def test_process_event_queue_full_is_swallowed():
    conn = _conn()
    q = asyncio.Queue(maxsize=1)
    q.put_nowait("occupied")
    # Should not raise even though the queue is full.
    await conn._process_event([json.dumps({"@odata.id": "/All"})], q)
    assert q.qsize() == 1


# ---- SSEManager._sync_connections reconciliation ----


class _FakeConn:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


async def test_sync_connections_starts_new_and_stops_removed(repo):
    mgr = SSEManager(repository=repo, result_queue=asyncio.Queue())
    started = []

    async def fake_start(target):
        started.append(target.id)

    mgr._start_connection = fake_start

    # A live SSE target should get a connection started.
    t = await repo.create_target(
        name="s",
        host="h",
        username="u",
        password="p",
        connection_mode="sse",
        sse_endpoint="/sse",
    )
    # A stale connection for a target no longer present must be stopped + removed.
    stale = _FakeConn()
    mgr._connections[999999] = stale

    await mgr._sync_connections()
    assert started == [t.id]
    assert stale.stopped is True and 999999 not in mgr._connections
