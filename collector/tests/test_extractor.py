# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the schema-based metric extractor."""

from src.parser.extractor import MetricExtractor
from src.parser.schema import FieldDefinition, MetricSchema, TagDefinition


class _StubLoader:
    def __init__(self, schemas):
        self._schemas = schemas

    def get_schemas(self):
        return self._schemas


def _extractor(schemas=None):
    return MetricExtractor(_StubLoader(schemas or []))


# ---- _to_numeric (bool-before-int gotcha is the key case) ----


def test_to_numeric_bool_before_int():
    ex = _extractor()
    assert ex._to_numeric(True) == 1.0
    assert ex._to_numeric(False) == 0.0


def test_to_numeric_int_float_str():
    ex = _extractor()
    assert ex._to_numeric(42) == 42.0
    assert ex._to_numeric(3.5) == 3.5
    assert ex._to_numeric("12.5") == 12.5


def test_to_numeric_rejects_nan_inf():
    # Non-finite values must become None: InfluxDB rejects NaN/Inf line protocol
    # at write time, which would fail (and re-queue) the whole batch forever.
    ex = _extractor()
    assert ex._to_numeric(float("nan")) is None
    assert ex._to_numeric(float("inf")) is None
    assert ex._to_numeric(float("-inf")) is None
    assert ex._to_numeric("NaN") is None
    assert ex._to_numeric("Infinity") is None
    assert ex._to_numeric("-inf") is None
    # A 300-digit JSON integer OverflowErrors on float() -> must degrade to None,
    # not propagate and drop the whole poll result.
    assert ex._to_numeric(10**400) is None


def test_to_numeric_word_mappings():
    ex = _extractor()
    assert ex._to_numeric("OK") == 1.0
    assert ex._to_numeric("enabled") == 1.0
    assert ex._to_numeric("disabled") == 0.0


def test_to_numeric_non_numeric_returns_none():
    ex = _extractor()
    assert ex._to_numeric(None) is None
    assert ex._to_numeric("banana") is None
    assert ex._to_numeric({"a": 1}) is None


# ---- extract_from_data ----


def _report():
    return {
        "MetricValues": [
            {"MetricProperty": "GPU_TEMP", "MetricValue": "42.5"},
            {"MetricProperty": "GPU_POWER", "MetricValue": "310"},
            {"MetricProperty": "GPU_HEALTH", "MetricValue": "OK"},
        ]
    }


def test_extract_with_simple_schema():
    schema = MetricSchema(
        name="mv",
        description="",
        path_pattern="$.MetricValues[*]",
        fields=[FieldDefinition(json_key="MetricValue", metric_name="gpu_metric", unit="")],
        tags_from=[TagDefinition(json_key="MetricProperty", tag_name="prop")],
    )
    metrics = _extractor([schema]).extract_from_data(_report(), host="bmc1")
    # 42.5, 310, and "OK"->1.0 are all numeric
    assert len(metrics) == 3
    values = sorted(m.value for m in metrics)
    assert values == [1.0, 42.5, 310.0]
    # base + per-match tags applied
    assert all(m.tags["host"] == "bmc1" for m in metrics)
    assert {m.tags["prop"] for m in metrics} == {"GPU_TEMP", "GPU_POWER", "GPU_HEALTH"}


def test_extract_jsonpath_regex_quoting():
    # Documented rule: jsonpath_ng.ext needs quoted regex, not /.../ syntax.
    schema = MetricSchema(
        name="temp",
        description="",
        path_pattern='$.MetricValues[?(@.MetricProperty =~ "(?i).*GPU_TEMP.*")]',
        fields=[FieldDefinition(json_key="MetricValue", metric_name="gpu_temp")],
    )
    metrics = _extractor([schema]).extract_from_data(_report(), host="bmc1")
    assert len(metrics) == 1
    assert metrics[0].value == 42.5


def test_extract_skips_non_numeric_fields():
    data = {"MetricValues": [{"MetricProperty": "X", "MetricValue": "not-a-number"}]}
    schema = MetricSchema(
        name="mv",
        description="",
        path_pattern="$.MetricValues[*]",
        fields=[FieldDefinition(json_key="MetricValue", metric_name="x")],
    )
    assert _extractor([schema]).extract_from_data(data, host="h") == []


def test_extract_bad_pattern_returns_empty():
    schema = MetricSchema(
        name="bad",
        description="",
        path_pattern="$$$not-valid$$$",
        fields=[FieldDefinition(json_key="v", metric_name="x")],
    )
    # Bad pattern is handled gracefully (no crash, no metrics)
    assert _extractor([schema]).extract_from_data({"a": 1}, host="h") == []
