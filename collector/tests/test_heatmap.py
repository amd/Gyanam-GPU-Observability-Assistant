# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the Data Hall heatmap: hot cache, snapshot publish, API read."""

from src.metrics_cache import HEATMAP_METRICS, HeatmapCache

# ---- HeatmapCache (pure) ----------------------------------------------------


def test_cache_record_and_latest():
    c = HeatmapCache()
    c.record("10.0.0.1", "gpu_die_temp_celsius", 70.0, now=1000.0)
    # Same cycle -> keep the max (hotspot across GPUs).
    c.record("10.0.0.1", "gpu_die_temp_celsius", 85.0, now=1001.0)
    c.record("10.0.0.1", "gpu_die_temp_celsius", 60.0, now=1002.0)
    assert c.latest("gpu_temp", now=1003.0) == {"10.0.0.1": 85.0}


def test_cache_new_cycle_resets():
    c = HeatmapCache()
    c.record("h", "gpu_die_temp_celsius", 90.0, now=0.0)
    # Far later -> a new poll cycle -> value replaced, not maxed with the old one.
    c.record("h", "gpu_die_temp_celsius", 50.0, now=500.0)
    assert c.latest("gpu_temp", now=500.0) == {"h": 50.0}


def test_cache_drops_stale_and_unknown():
    c = HeatmapCache()
    c.record("h", "gpu_die_temp_celsius", 70.0, now=0.0)
    # Older than the freshness window -> omitted (renders no-data).
    assert c.latest("gpu_temp", now=10_000.0) == {}
    # Unknown UI metric -> empty.
    assert c.latest("nonsense", now=1.0) == {}


def test_cache_ignores_uncached_names_and_empty_host():
    c = HeatmapCache()
    c.record("h", "some_unrelated_metric", 5.0, now=0.0)  # not a heatmap field
    c.record(None, "gpu_die_temp_celsius", 5.0, now=0.0)  # no host
    assert c.latest("gpu_temp", now=0.0) == {}


def test_heatmap_metrics_mapping():
    assert HEATMAP_METRICS["gpu_temp"][0] == "gpu_die_temp_celsius"
    assert HEATMAP_METRICS["power"][0] == "board_power_watts"


# ---- API read /datahall/api/heatmap ----------------------------------------


async def test_api_heatmap_maps_hosts_and_domain(client, repo):
    t1 = await repo.create_target(name="a", host="10.0.0.1", username="u", password="p")
    t2 = await repo.create_target(name="b", host="10.0.0.2", username="u", password="p")
    # Collector-published snapshot in shared SQLite; includes an unknown host.
    await repo.upsert_heatmap_snapshot(
        "gpu_temp", {"10.0.0.1": 60.0, "10.0.0.2": 80.0, "10.0.0.99": 50.0}
    )
    r = await client.get("/datahall/api/heatmap?metric=gpu_temp")
    body = r.json()
    assert r.status_code == 200
    # Host -> target_id mapping; the unknown host (10.0.0.99) is dropped.
    assert body["values"] == {str(t1.id): 60.0, str(t2.id): 80.0}
    assert body["unit"] == "°C" and body["critical"] == 90.0
    # Domain top is the STATIC component max (90°C), not the fleet p95.
    assert body["domain"][1] == 90.0
    assert body["domain"][0] <= 60.0


async def test_api_heatmap_no_snapshot_returns_empty(client, repo):
    r = await client.get("/datahall/api/heatmap?metric=gpu_temp")
    body = r.json()
    assert r.status_code == 200
    assert body["values"] == {} and body["domain"] is None
    assert body["unit"] == "°C"  # unit/critical still populated from the metric spec


async def test_api_heatmap_unknown_metric_returns_empty(client):
    r = await client.get("/datahall/api/heatmap?metric=bogus")
    body = r.json()
    assert r.status_code == 200
    assert body["values"] == {} and body["unit"] == ""


# ---- repository snapshot round-trip + staleness ----------------------------


async def test_heatmap_snapshot_roundtrip_and_staleness(repo):
    await repo.upsert_heatmap_snapshot("power", {"h1": 2000.0})
    assert await repo.get_heatmap_snapshot("power") == {"h1": 2000.0}
    # Upsert overwrites.
    await repo.upsert_heatmap_snapshot("power", {"h2": 2100.0})
    assert await repo.get_heatmap_snapshot("power") == {"h2": 2100.0}
    # A tiny max-age makes the just-written snapshot read as stale -> None.
    assert await repo.get_heatmap_snapshot("power", max_age_seconds=-1) is None
    # Missing metric -> None.
    assert await repo.get_heatmap_snapshot("never_written") is None


async def test_api_heatmap_requires_auth(noauth_client):
    r = await noauth_client.get("/datahall/api/heatmap?metric=gpu_temp")
    assert r.status_code in (401, 403)


# ---- collector snapshot publisher task -------------------------------------


async def test_heatmap_snapshot_task_publishes_then_stops(monkeypatch):
    import asyncio

    import pytest
    from src import collector_main as cm
    from src.metrics_cache import HEATMAP

    HEATMAP._d.clear()
    HEATMAP.record("h1", "gpu_die_temp_celsius", 70.0)
    published: dict = {}

    class _FakeRepo:
        async def upsert_heatmap_snapshot(self, metric, values, collector_id=""):
            published[metric] = values

    async def _cancel_sleep(_seconds):
        raise asyncio.CancelledError()

    # One iteration publishes every metric, then the patched sleep (the task's
    # cancellation point) stops it.
    monkeypatch.setattr(cm.asyncio, "sleep", _cancel_sleep)
    try:
        with pytest.raises(asyncio.CancelledError):
            await cm.heatmap_snapshot_task(_FakeRepo(), interval=0)
        assert published["gpu_temp"] == {"h1": 70.0}
        assert "board_temp" in published and "power" in published  # all metrics published
    finally:
        HEATMAP._d.clear()
