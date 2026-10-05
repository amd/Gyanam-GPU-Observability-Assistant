# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Standards-based discovery of a target's Redfish TelemetryService metrics.

Instead of relying on a hardcoded list of (vendor-specific) metric-report URIs,
enumerate whatever a target actually exposes under
``/redfish/v1/TelemetryService/MetricReports`` and consume all of it. This keeps
gyanam generic: any system that conforms to the standard TelemetryService model
works without code changes. The embedded ``metrics_schema.yaml`` remains the
fallback for extraction and the fixture set we test against.

All reads are best-effort authenticated GETs (reusing the client's generic
report fetch); a missing service/collection yields ``None`` and never raises.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_TELEMETRY_ROOT = "/redfish/v1/TelemetryService"
# Bound the enumeration so a pathological service can't balloon onboarding.
_MAX_REPORTS = 64
_MAX_DEFINITIONS = 512
# Report ids that aggregate every metric; order them last so specific reports
# claim their MetricProperties first during the poller's dedup pass.
_AGGREGATE_HINTS = ("all", "comprehensive")

# Pre-aggregated statistical reports (e.g. AvgPowerConsumptionHour) roll a metric
# up over a time window on the BMC. GYANAM does its own downsampling, so polling
# these just multiplies the GETs/parsing per cycle for redundant data — excluded
# from discovery by default. A statistical aggregate = a function token AND a
# time-window token in the report id (so "HealthRollup" and the superset "All"
# report, which carries exclusive metrics, are kept). Operators who do want one
# can still pin it via the per-target report override.
_STAT_FUNC_TOKENS = ("avg", "average", "min", "minimum", "max", "maximum", "mean", "peak", "sum")
_TIME_WINDOW_TOKENS = ("hour", "day", "week", "month", "year", "minute")


async def _get_json(client, uri: str) -> dict | None:
    """Authenticated JSON GET via the client's generic report fetch."""
    try:
        resp = await client.get_metric_report(uri)
        if not resp or not resp.success:
            return None
        data = json.loads(resp.content)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as e:
        logger.debug("Telemetry GET %s failed: %s: %s", uri, type(e).__name__, e)
        return None
    except Exception as e:  # noqa: BLE001 — best-effort; never break the caller
        logger.debug("Telemetry GET %s error: %s: %s", uri, type(e).__name__, e)
        return None


def _report_type(uri: str) -> str:
    """Derive a stable report_type label from a MetricReport URI (its Id)."""
    return uri.rstrip("/").rsplit("/", 1)[-1] or "report"


def _is_aggregate(report_type: str) -> bool:
    rt = report_type.casefold()
    return any(h in rt for h in _AGGREGATE_HINTS)


def _is_statistical_aggregate(report_type: str) -> bool:
    """True if the report id looks like a pre-aggregated statistic over a window."""
    rt = report_type.casefold()
    return any(f in rt for f in _STAT_FUNC_TOKENS) and any(w in rt for w in _TIME_WINDOW_TOKENS)


async def discover_metric_reports(
    client, telemetry_root: str = _TELEMETRY_ROOT, *, exclude_aggregates: bool = True
) -> list[dict] | None:
    """Enumerate a target's MetricReports collection.

    Returns ``[{"uri": ..., "report_type": ...}, ...]`` (aggregate reports like
    ``All`` ordered last so specific reports win dedup), or ``None`` when the
    TelemetryService / collection is absent or empty. When ``exclude_aggregates``
    is set (default), pre-aggregated statistical reports are dropped to keep the
    per-cycle GET/parse cost down.
    """
    collection = await _get_json(client, f"{telemetry_root}/MetricReports")
    if not collection:
        return None
    reports: list[dict] = []
    dropped = 0
    for member in (collection.get("Members") or [])[:_MAX_REPORTS]:
        uri = member.get("@odata.id") if isinstance(member, dict) else None
        if not uri:
            continue
        report_type = _report_type(uri)
        if exclude_aggregates and _is_statistical_aggregate(report_type):
            dropped += 1
            continue
        reports.append({"uri": uri, "report_type": report_type})
    if dropped:
        logger.info(
            "Discovery dropped %d pre-aggregated statistical report(s) (pin via the "
            "per-target override to include them)",
            dropped,
        )
    # Specific reports first, aggregate reports last (stable within each group).
    reports.sort(key=lambda r: _is_aggregate(r["report_type"]))
    return reports or None


async def load_metric_definitions(
    client, telemetry_root: str = _TELEMETRY_ROOT
) -> dict[str, dict] | None:
    """Build a ``MetricId -> {"unit", "data_type", "metric_type"}`` map.

    Resolves units/meaning from the standard ``MetricDefinitions`` collection so
    metric naming can be standards-aware rather than purely schema-driven. Many
    BMCs don't populate MetricDefinitions, so this is best-effort and returns
    ``None`` when absent; callers fall back to the embedded schema.
    """
    collection = await _get_json(client, f"{telemetry_root}/MetricDefinitions")
    if not collection:
        return None
    defs: dict[str, dict] = {}
    for member in (collection.get("Members") or [])[:_MAX_DEFINITIONS]:
        uri = member.get("@odata.id") if isinstance(member, dict) else None
        if not uri:
            continue
        doc = await _get_json(client, uri)
        if not doc:
            continue
        metric_id = doc.get("Id") or _report_type(uri)
        defs[str(metric_id)] = {
            "unit": doc.get("Units"),
            "data_type": doc.get("MetricDataType"),
            "metric_type": doc.get("MetricType"),
        }
    return defs or None
