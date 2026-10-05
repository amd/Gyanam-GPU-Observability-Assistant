# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for auto-discovery of numeric metrics."""

from src.parser.discovery import MetricDiscovery
from src.parser.schema import AutoDiscoveryConfig


class _StubLoader:
    def __init__(self, config):
        self._config = config

    def get_auto_discovery_config(self):
        return self._config


def _discovery(**cfg):
    # include everything by default unless a test overrides.
    cfg.setdefault("include_patterns", ["*"])
    config = AutoDiscoveryConfig(enabled=True, **cfg)
    return MetricDiscovery(_StubLoader(config))


# ---- pure helpers ----


def test_is_numeric():
    d = _discovery()
    assert d._is_numeric(3) is True
    assert d._is_numeric(2.5) is True
    assert d._is_numeric("10") is True
    # booleans are treated as numeric (0/1) by design
    assert d._is_numeric(True) is True
    # boolean-like words are numeric; arbitrary strings are not
    assert d._is_numeric("healthy") is True
    assert d._is_numeric("hello") is False
    assert d._is_numeric(None) is False


def test_to_numeric():
    d = _discovery()
    assert d._to_numeric(5) == 5.0
    assert d._to_numeric("7.5") == 7.5
    assert d._to_numeric(True) == 1.0
    assert d._to_numeric("x") is None


def test_numeric_rejects_nan_inf():
    # Non-finite values are not numeric and must not be converted (InfluxDB
    # rejects NaN/Inf at write time).
    d = _discovery()
    assert d._is_numeric(float("nan")) is False
    assert d._is_numeric(float("inf")) is False
    assert d._is_numeric("NaN") is False
    assert d._to_numeric(float("nan")) is None
    assert d._to_numeric(float("-inf")) is None
    assert d._to_numeric("inf") is None
    # Oversized int OverflowErrors on float()/isfinite -> must degrade safely.
    assert d._is_numeric(10**400) is False
    assert d._to_numeric(10**400) is None


def test_key_to_metric_name_sanitizes():
    d = _discovery()
    name = d._key_to_metric_name("GPU Temp (C)", "$.a.b")
    assert " " not in name and "(" not in name and ")" not in name
    assert name  # non-empty


def test_should_include_glob_patterns():
    d = _discovery(include_patterns=["temp*"], exclude_patterns=["*secret*"])
    cfg = d.schema_loader.get_auto_discovery_config()
    assert d._should_include("temperature", cfg) is True
    assert d._should_include("secret_key", cfg) is False
    assert d._should_include("unrelated", cfg) is False  # no include match


def test_should_include_empty_include_matches_nothing():
    d = MetricDiscovery(_StubLoader(AutoDiscoveryConfig(enabled=True, include_patterns=[])))
    cfg = d.schema_loader.get_auto_discovery_config()
    assert d._should_include("anything", cfg) is False


# ---- discover ----


def test_discover_finds_numeric_leaves():
    d = _discovery()
    data = {"sensor": {"temp": 42, "name": "gpu0", "power": "310"}}
    metrics = d.discover(data, host="bmc1")
    vals = {m.value for m in metrics}
    assert 42.0 in vals  # int
    assert 310.0 in vals  # numeric string
    assert all(m.tags["host"] == "bmc1" for m in metrics)


def test_discover_disabled_returns_empty():
    d = MetricDiscovery(_StubLoader(AutoDiscoveryConfig(enabled=False, include_patterns=["*"])))
    assert d.discover({"a": 1}, host="h") == []


def test_discover_respects_exclude_keys():
    d = _discovery()
    metrics = d.discover({"temp": 42, "power": 310}, host="h", exclude_keys={"temp"})
    vals = {m.value for m in metrics}
    assert 310.0 in vals
    assert 42.0 not in vals
