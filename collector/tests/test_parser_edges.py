# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Edge tests: discovery analysis/tags and log-parser file operations."""

from pathlib import Path

from src.parser.discovery import MetricDiscovery
from src.parser.redfish_log_parser import RedfishLogParser
from src.parser.schema import AutoDiscoveryConfig


class _StubLoader:
    def __init__(self, config):
        self._config = config

    def get_auto_discovery_config(self):
        return self._config


def _discovery():
    return MetricDiscovery(_StubLoader(AutoDiscoveryConfig(enabled=True, include_patterns=["*"])))


def test_analyze_structure():
    d = _discovery()
    result = d.analyze_structure({"a": {"b": 1, "c": "text"}, "d": [1, 2, 3]})
    assert isinstance(result, dict)


def test_discover_nested_arrays():
    d = _discovery()
    data = {"sensors": [{"temp": 40}, {"temp": 41}]}
    metrics = d.discover(data, host="h")
    vals = sorted(m.value for m in metrics)
    assert vals == [40.0, 41.0]


def test_log_parser_parse_file(tmp_path):
    p = Path(tmp_path) / "redfish-tree.log"
    p.write_text(
        "GET redfish/v1/TelemetryService/MetricReports/All\n"
        '{"MetricValues": [{"MetricProperty": "T", "MetricValue": "1"}]}\n'
    )
    parser = RedfishLogParser(target_url="redfish/v1/TelemetryService/MetricReports/All")
    data = parser.parse_file(p)
    assert data is not None
    assert data["MetricValues"][0]["MetricValue"] == "1"


def test_log_parser_missing_file(tmp_path):
    parser = RedfishLogParser()
    assert parser.parse_file(Path(tmp_path) / "nope.log") is None
