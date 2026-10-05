# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for standards-based metric-report discovery + poller resolution."""

import json
from types import SimpleNamespace

from src.redfish.report_discovery import discover_metric_reports, load_metric_definitions


class _FakeClient:
    """Stand-in for RedfishClient.get_metric_report (a JSON GET)."""

    def __init__(self, responses: dict):
        self._responses = responses

    async def get_metric_report(self, uri: str):
        body = self._responses.get(uri)
        if body is None:
            return SimpleNamespace(success=False, content=b"")
        return SimpleNamespace(success=True, content=json.dumps(body).encode())


_ROOT = "/redfish/v1/TelemetryService"


# ---- discover_metric_reports -----------------------------------------------


async def test_discover_enumerates_and_orders_aggregate_last():
    resp = {
        f"{_ROOT}/MetricReports": {
            "Members": [
                {"@odata.id": f"{_ROOT}/MetricReports/All"},
                {"@odata.id": f"{_ROOT}/MetricReports/OAM_ProcessorMetrics_0"},
                {"@odata.id": f"{_ROOT}/MetricReports/PlatformSensorsMetrics_0"},
            ]
        },
    }
    reports = await discover_metric_reports(_FakeClient(resp))
    assert reports is not None
    types = [r["report_type"] for r in reports]
    # Specific reports first, the aggregate "All" ordered last for dedup priority.
    assert types[-1] == "All"
    assert set(types) == {"All", "OAM_ProcessorMetrics_0", "PlatformSensorsMetrics_0"}
    assert reports[0]["uri"].startswith(_ROOT)


async def test_discover_excludes_pre_aggregated_statistical_reports():
    # A real smci355 fleet enumerates 9 Avg/Min/Max PowerConsumption x Hour/Day/Week
    # reports alongside the useful ones. By default those are dropped (GYANAM
    # downsamples itself) while HealthRollup and the superset "All" are kept.
    members = [
        {"@odata.id": f"{_ROOT}/MetricReports/{rt}"}
        for rt in (
            "AvgPowerConsumptionHour",
            "MinPowerConsumptionHour",
            "MaxPowerConsumptionHour",
            "AvgPowerConsumptionDay",
            "MinPowerConsumptionWeek",
            "OAM_ProcessorMetrics_0",
            "PlatformSensorsMetrics_0",
            "HealthRollup",
            "All",
        )
    ]
    resp = {f"{_ROOT}/MetricReports": {"Members": members}}

    reports = await discover_metric_reports(_FakeClient(resp))  # exclude by default
    kept = {r["report_type"] for r in reports}
    assert kept == {"OAM_ProcessorMetrics_0", "PlatformSensorsMetrics_0", "HealthRollup", "All"}
    assert [r["report_type"] for r in reports][-1] == "All"  # aggregate still last


async def test_discover_keeps_aggregates_when_disabled():
    members = [
        {"@odata.id": f"{_ROOT}/MetricReports/{rt}"}
        for rt in (
            "AvgPowerConsumptionHour",
            "OAM_ProcessorMetrics_0",
        )
    ]
    resp = {f"{_ROOT}/MetricReports": {"Members": members}}
    reports = await discover_metric_reports(_FakeClient(resp), exclude_aggregates=False)
    assert {r["report_type"] for r in reports} == {
        "AvgPowerConsumptionHour",
        "OAM_ProcessorMetrics_0",
    }


def test_is_statistical_aggregate_classification():
    from src.redfish.report_discovery import _is_statistical_aggregate

    assert _is_statistical_aggregate("AvgPowerConsumptionHour") is True
    assert _is_statistical_aggregate("MaxPowerConsumptionWeek") is True
    # function word but no time window, or vice-versa -> not a statistical aggregate
    assert _is_statistical_aggregate("HealthRollup") is False
    assert _is_statistical_aggregate("OAM_ProcessorMetrics_0") is False
    assert _is_statistical_aggregate("All") is False
    assert _is_statistical_aggregate("MaxLinkSpeed") is False  # max, but no time window


async def test_discover_absent_service_returns_none():
    assert await discover_metric_reports(_FakeClient({})) is None


async def test_discover_empty_collection_returns_none():
    resp = {f"{_ROOT}/MetricReports": {"Members": []}}
    assert await discover_metric_reports(_FakeClient(resp)) is None


async def test_discover_skips_members_without_odata_id():
    resp = {
        f"{_ROOT}/MetricReports": {
            "Members": [{"nope": 1}, {"@odata.id": f"{_ROOT}/MetricReports/R1"}]
        }
    }
    reports = await discover_metric_reports(_FakeClient(resp))
    assert reports == [{"uri": f"{_ROOT}/MetricReports/R1", "report_type": "R1"}]


# ---- load_metric_definitions -----------------------------------------------


async def test_load_metric_definitions_builds_unit_map():
    resp = {
        f"{_ROOT}/MetricDefinitions": {
            "Members": [
                {"@odata.id": f"{_ROOT}/MetricDefinitions/TempC"},
                {"@odata.id": f"{_ROOT}/MetricDefinitions/PowerW"},
            ]
        },
        f"{_ROOT}/MetricDefinitions/TempC": {
            "Id": "TempC",
            "Units": "Cel",
            "MetricType": "Gauge",
            "MetricDataType": "Decimal",
        },
        f"{_ROOT}/MetricDefinitions/PowerW": {"Id": "PowerW", "Units": "W"},
    }
    defs = await load_metric_definitions(_FakeClient(resp))
    assert defs["TempC"]["unit"] == "Cel" and defs["TempC"]["metric_type"] == "Gauge"
    assert defs["PowerW"]["unit"] == "W"


async def test_load_metric_definitions_absent_returns_none():
    assert await load_metric_definitions(_FakeClient({})) is None


# ---- repository resolution (override > discovered > default) ----------------


_DEFAULTS = [
    {
        "uri": "/redfish/v1/TelemetryService/MetricReports/OAM_ProcessorMetrics_0",
        "report_type": "processor",
    },
    {"uri": "/redfish/v1/TelemetryService/MetricReports/All", "report_type": "comprehensive"},
]


def test_resolve_prefers_override(repo):
    override = [{"uri": "/x", "report_type": "x"}]
    t = SimpleNamespace(
        metric_reports_override=json.dumps(override),
        metric_discovery_mode="auto",
        discovered_reports=json.dumps([{"uri": "/d", "report_type": "d"}]),
    )
    assert repo.resolve_metric_reports(t, _DEFAULTS) == override  # override wins outright


def test_resolve_unions_discovered_with_defaults(repo):
    # A BMC that enumerated only a power report (not the OAM default) must still
    # poll the known-good default — and the aggregate 'All' must come last.
    discovered = [{"uri": "/pwr", "report_type": "AvgPower"}]
    t = SimpleNamespace(
        metric_reports_override=None,
        metric_discovery_mode="auto",
        discovered_reports=json.dumps(discovered),
    )
    merged = repo.resolve_metric_reports(t, _DEFAULTS)
    uris = [r["uri"] for r in merged]
    # Discovered + default OAM report are both present (no metric loss)...
    assert "/pwr" in uris
    assert "/redfish/v1/TelemetryService/MetricReports/OAM_ProcessorMetrics_0" in uris
    # ...and the aggregate 'All' report is ordered last for dedup priority.
    assert uris[-1].endswith("/All")


def test_resolve_union_dedupes_by_uri(repo):
    # A report discovered AND in the defaults appears once.
    dup = {
        "uri": "/redfish/v1/TelemetryService/MetricReports/OAM_ProcessorMetrics_0",
        "report_type": "processor",
    }
    t = SimpleNamespace(
        metric_reports_override=None,
        metric_discovery_mode="auto",
        discovered_reports=json.dumps([dup]),
    )
    merged = repo.resolve_metric_reports(t, _DEFAULTS)
    uris = [r["uri"] for r in merged]
    assert uris.count("/redfish/v1/TelemetryService/MetricReports/OAM_ProcessorMetrics_0") == 1


def test_resolve_manual_mode_ignores_discovered(repo):
    t = SimpleNamespace(
        metric_reports_override=None,
        metric_discovery_mode="manual",
        discovered_reports=json.dumps([{"uri": "/d", "report_type": "d"}]),
    )
    assert repo.resolve_metric_reports(t, _DEFAULTS) == _DEFAULTS


def test_resolve_falls_back_to_global_default(repo):
    t = SimpleNamespace(
        metric_reports_override=None, metric_discovery_mode="auto", discovered_reports=None
    )
    assert repo.resolve_metric_reports(t, _DEFAULTS) == _DEFAULTS


async def test_set_discovered_reports_roundtrip(repo):
    t = await repo.create_target(name="n", host="10.0.0.1", username="u", password="p")
    reports = [{"uri": "/r1", "report_type": "r1"}]
    await repo.set_discovered_reports(t.id, reports)
    fetched = await repo.get_target(t.id)
    assert repo.get_discovered_metric_reports(fetched) == reports
    # Clearing works too.
    await repo.set_discovered_reports(t.id, None)
    fetched = await repo.get_target(t.id)
    assert fetched.discovered_reports is None
