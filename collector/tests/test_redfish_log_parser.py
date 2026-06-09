# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the redfish-tree.log JSON-block parser."""

from src.parser.redfish_log_parser import RedfishLogParser


def test_parse_content_extracts_block_after_target_url():
    parser = RedfishLogParser(target_url="redfish/v1/TelemetryService/MetricReports/All")
    content = (
        "GET redfish/v1/Chassis/1\n"
        '{"other": "data"}\n'
        "GET redfish/v1/TelemetryService/MetricReports/All\n"
        '{"MetricValues": [{"MetricProperty": "T", "MetricValue": "1"}]}\n'
        "GET redfish/v1/Managers/1\n"
        '{"tail": true}\n'
    )
    data = parser.parse_content(content)
    assert data is not None
    assert data["MetricValues"][0]["MetricProperty"] == "T"


def test_parse_handles_braces_in_strings():
    parser = RedfishLogParser(target_url="redfish/v1/X")
    content = 'GET redfish/v1/X\n{"msg": "value with } brace", "n": 2}\n'
    data = parser.parse_content(content)
    assert data == {"msg": "value with } brace", "n": 2}


def test_parse_missing_target_returns_none():
    parser = RedfishLogParser(target_url="redfish/v1/NotThere")
    assert parser.parse_content("GET redfish/v1/Other\n{}\n") is None


def test_parse_malformed_json_returns_none():
    parser = RedfishLogParser(target_url="redfish/v1/X")
    assert parser.parse_content("GET redfish/v1/X\n{not valid json\n") is None
