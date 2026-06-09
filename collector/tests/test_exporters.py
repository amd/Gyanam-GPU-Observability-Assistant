# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for exporter base + Prometheus formatting."""

import re
from datetime import UTC, datetime

from src.exporters.base import Metric
from src.exporters.prometheus import PrometheusExporter, sanitize_metric_name

_PROM_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


def test_metric_to_dict():
    m = Metric(
        name="gpu_temp",
        value=42.0,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        tags={"host": "h"},
        unit="C",
        metric_type="gauge",
    )
    d = m.to_dict()
    assert d["name"] == "gpu_temp"
    assert d["value"] == 42.0
    assert d["tags"] == {"host": "h"}
    assert d["type"] == "gauge"
    assert d["unit"] == "C"
    assert d["timestamp"].startswith("2026-01-01")


def test_sanitize_metric_name_produces_valid_names():
    for raw in ["gpu temp (C)", "1starts_with_digit", "a-b.c/d", "GPU:Power"]:
        out = sanitize_metric_name(raw)
        assert _PROM_NAME.match(out), f"{raw!r} -> {out!r} not a valid Prometheus name"


async def test_prometheus_write_and_generate():
    exp = PrometheusExporter()
    await exp.connect()
    ok = await exp.write(
        [Metric(name="gpu_temp", value=42.0, timestamp=datetime.now(UTC), tags={"host": "h"})]
    )
    assert ok is True
    output = exp.generate_metrics()
    assert b"gpu_temp" in output
    await exp.close()


async def test_prometheus_health_check_and_registry():
    exp = PrometheusExporter()
    await exp.connect()
    ok, msg = await exp.health_check()
    assert ok is True
    assert exp.registry is not None
    await exp.close()


async def test_prometheus_counter_metric():
    exp = PrometheusExporter()
    await exp.connect()
    await exp.write(
        [
            Metric(
                name="reqs",
                value=5.0,
                timestamp=datetime.now(UTC),
                tags={"host": "h"},
                metric_type="counter",
            )
        ]
    )
    assert b"reqs" in exp.generate_metrics()
    await exp.close()
