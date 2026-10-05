# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the exporter base Metric model."""

from datetime import UTC, datetime

from src.exporters.base import Metric


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
