# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the collector's ordered graceful-shutdown helper."""

import asyncio

import src.collector_main as cm


class _Rec:
    """Records lifecycle calls in order across the fakes."""

    def __init__(self):
        self.calls = []


class _FakePoller:
    def __init__(self, rec, queue):
        self._rec = rec
        self._result_queue = queue

    async def stop(self):
        self._rec.calls.append("poller.stop")


class _FakeSSE:
    def __init__(self, rec):
        self._rec = rec

    async def stop(self):
        self._rec.calls.append("sse.stop")


class _FakeAlertMgr:
    def __init__(self, rec):
        self._rec = rec

    async def stop(self):
        self._rec.calls.append("alert.stop")


class _FakeExporter:
    def __init__(self, rec):
        self._rec = rec

    async def close(self):
        self._rec.calls.append("exporter.close")


class _FakeRepo:
    def __init__(self, rec):
        self._rec = rec

    async def close(self):
        self._rec.calls.append("repo.close")


class _FakePool:
    def __init__(self, rec):
        self._rec = rec

    def shutdown(self, wait=True):
        self._rec.calls.append(("pool.shutdown", wait))


async def _dummy_task():
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        raise


async def test_shutdown_full_order():
    rec = _Rec()
    q = asyncio.Queue()
    processor = asyncio.create_task(_dummy_task())
    cleanup = asyncio.create_task(_dummy_task())
    health = asyncio.create_task(_dummy_task())
    inventory = asyncio.create_task(_dummy_task())

    await cm._shutdown_collector(
        poller=_FakePoller(rec, q),
        sse_manager=_FakeSSE(rec),
        alert_manager=_FakeAlertMgr(rec),
        exporter=_FakeExporter(rec),
        repository=_FakeRepo(rec),
        extract_pool=_FakePool(rec),
        processor_task=processor,
        cleanup_bg_task=cleanup,
        health_server_task=health,
        inventory_task=inventory,
    )

    # Producers stopped before exporter close; exporter before repo before pool.
    assert rec.calls.index("poller.stop") < rec.calls.index("exporter.close")
    assert rec.calls.index("sse.stop") < rec.calls.index("exporter.close")
    assert rec.calls.index("exporter.close") < rec.calls.index("repo.close")
    assert rec.calls.index("repo.close") < rec.calls.index(("pool.shutdown", True))
    # All background tasks were cancelled/awaited.
    for t in (processor, cleanup, health, inventory):
        assert t.done()


async def test_shutdown_partial_resources_are_safe():
    # Only a repository exists (startup failed early) — must not raise.
    rec = _Rec()
    await cm._shutdown_collector(repository=_FakeRepo(rec))
    assert rec.calls == ["repo.close"]


async def test_shutdown_no_resources_noop():
    # Everything None — pure no-op, no exception.
    await cm._shutdown_collector()


async def test_shutdown_swallows_component_errors():
    rec = _Rec()

    class _BadExporter:
        async def close(self):
            raise RuntimeError("boom")

    # A failing component must not prevent the rest of teardown.
    await cm._shutdown_collector(exporter=_BadExporter(), repository=_FakeRepo(rec))
    assert rec.calls == ["repo.close"]
