# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""In-memory hot cache of the latest heatmap-relevant metric per host.

The Data Hall heatmap needs one current value per node (e.g. hottest GPU) without
querying InfluxDB on every UI refresh. The collector already touches every metric
on the export path, so it records just the handful of heatmap fields here as it
exports them — an O(1) dict write for a small field subset. InfluxDB stays the
historical source of truth; this cache serves only the live heatmap read.

A node reports many values for a metric in one poll cycle (one per GPU/OAM), all
arriving in the same export burst. ``record`` keeps the MAX within a short window
so the per-node value is the hotspot, and resets to the new value on the next
cycle (bursts are seconds apart; cycles are minutes apart).
"""

from __future__ import annotations

import time

# UI metric key -> (raw InfluxDB metric name, unit, critical threshold or None).
# Only these raw names are cached, which keeps the export-path cost negligible.
HEATMAP_METRICS: dict[str, tuple[str, str, float | None]] = {
    "gpu_temp": ("gpu_die_temp_celsius", "°C", 90.0),
    "board_temp": ("board_temp_celsius", "°C", 70.0),
    "power": ("board_power_watts", "W", None),
}

# The raw metric names worth caching (fast membership check on the export path).
CACHED_METRIC_NAMES: frozenset[str] = frozenset(name for name, _u, _c in HEATMAP_METRICS.values())

# Full-scale top of the heatmap colour gradient per metric — the max value the
# component supports/tolerates, so colour reflects absolute headroom (a node near
# its limit is red) rather than merely "hottest in the fleet". Temps use the
# component's critical/throttle point; power uses the board's rated ceiling
# (tune board_power to match the actual hardware).
HEATMAP_SCALE_MAX: dict[str, float] = {
    "gpu_temp": 90.0,  # GPU die throttle/critical
    "board_temp": 70.0,  # board critical
    "power": 10500.0,  # loaded UBB board ceiling (watts)
}

# Values within this window are treated as the same poll cycle -> take the max.
_SAME_CYCLE_WINDOW_S = 90.0
# A cached value older than this is considered stale (node renders "no data").
_FRESH_FOR_S = 600.0


class HeatmapCache:
    """``host -> {metric_name -> [value, monotonic_ts]}`` with per-cycle max."""

    def __init__(self) -> None:
        self._d: dict[str, dict[str, list[float]]] = {}

    def record(self, host: str | None, name: str, value: float, now: float | None = None) -> None:
        """Record a metric value for a host, keeping the max within a cycle."""
        if not host or name not in CACHED_METRIC_NAMES:
            return
        now = time.monotonic() if now is None else now
        per_host = self._d.setdefault(host, {})
        entry = per_host.get(name)
        if entry is None or (now - entry[1]) > _SAME_CYCLE_WINDOW_S:
            per_host[name] = [value, now]  # new cycle
        else:
            if value > entry[0]:
                entry[0] = value  # same cycle -> hotspot
            entry[1] = now

    def latest(self, metric_key: str, now: float | None = None) -> dict[str, float]:
        """Return ``{host: value}`` for the UI metric, dropping stale entries."""
        spec = HEATMAP_METRICS.get(metric_key)
        if spec is None:
            return {}
        name = spec[0]
        now = time.monotonic() if now is None else now
        out: dict[str, float] = {}
        for host, per_host in self._d.items():
            entry = per_host.get(name)
            if entry is not None and (now - entry[1]) <= _FRESH_FOR_S:
                out[host] = entry[0]
        return out


# Process-wide singleton shared by the exporter (writer) and the /heatmap
# endpoint (reader).
HEATMAP = HeatmapCache()
