# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""Alert management endpoints."""

from datetime import UTC, datetime, timedelta
from math import ceil

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from ...config import get_config
from ..auth import get_current_user
from ..csrf import generate_csrf_token
from ..dependencies import get_repository

router = APIRouter()

# Collector service alert manager stats endpoint (internal docker network)
COLLECTOR_ALERT_STATS_URL = "http://collector:8081/alerts/manager-stats"


def _iso_utc(dt: datetime | None) -> str | None:
    """Serialize a stored (naive UTC) datetime as an explicit-UTC ISO string.

    Alert datetimes are stored as naive UTC; tag them so API clients don't
    misread them as local time.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


# ---- JSON API ----


@router.get("/api", summary="List all alerts")
async def list_alerts_api(
    target_id: int | None = Query(None),
    severity: str | None = Query(None),
    hours: int = Query(168, ge=0, description="Hours to look back (default 7 days; 0 = all)"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: str = Depends(get_current_user),
):
    """Get alerts with optional filtering."""
    repository = get_repository()

    # Calculate since timestamp
    since = datetime.now(UTC) - timedelta(hours=hours) if hours > 0 else None

    alerts = await repository.get_alerts(
        target_id=target_id,
        severity=severity,
        since=since,
        limit=limit,
        offset=offset,
        include_raw=True,
    )

    return [
        {
            "id": alert.id,
            "target_id": alert.target_id,
            "target_name": alert.target_name,
            "target_bmc": alert.target_bmc,
            "severity": alert.severity,
            "message": alert.message,
            "message_id": alert.message_id,
            "event_type": alert.event_type,
            "origin_of_condition": alert.origin_of_condition,
            "event_timestamp": _iso_utc(alert.event_timestamp),
            "received_at": _iso_utc(alert.received_at),
            "raw_data": alert.raw_data,
        }
        for alert in alerts
    ]


@router.get("/api/{alert_id}/raw", summary="Get an alert's raw event data")
async def get_alert_raw_api(alert_id: int, user: str = Depends(get_current_user)):
    """Return the full raw Redfish event/log entry for one alert (lazy-loaded by the UI)."""
    repository = get_repository()
    alert = await repository.get_alert(alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"id": alert.id, "raw_data": alert.raw_data}


@router.get("/api/{alert_id}/cper", summary="Get an alert's decoded CPER")
async def get_alert_cper_api(alert_id: int, user: str = Depends(get_current_user)):
    """Return the decoded CPER JSON for one alert (lazy-loaded by the UI)."""
    repository = get_repository()
    alert = await repository.get_alert(alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {
        "id": alert.id,
        "cper_status": alert.cper_status,
        "refined_message": alert.refined_message,
        "cper_decoded": alert.cper_decoded,
    }


@router.get("/api/stats", summary="Get alert statistics")
async def get_alert_stats_api(user: str = Depends(get_current_user)):
    """Get alert statistics (counts by severity)."""
    repository = get_repository()
    stats = await repository.get_alert_stats()
    return stats


@router.get("/api/manager-stats", summary="Get alert manager stats")
async def get_manager_stats_api(user: str = Depends(get_current_user)):
    """Get alert manager runtime statistics from collector service."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(COLLECTOR_ALERT_STATS_URL)
            if response.status_code == 200:
                return response.json()
    except Exception:
        # Collector unreachable / stats query failed — UI should still load
        # with the "alerts disabled" state rather than throwing 500.
        pass
    return {"enabled": False}


@router.get("/api/subscription-status", summary="Get detailed subscription status")
async def get_subscription_status_api(user: str = Depends(get_current_user)):
    """Get detailed alert subscription status per target with alert counts."""
    repository = get_repository()

    # Get manager stats from collector service
    manager_stats = None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(COLLECTOR_ALERT_STATS_URL)
            if response.status_code == 200:
                manager_stats = response.json()
    except Exception:
        # Collector unreachable — fall through and return an empty-state
        # response below; the UI handles missing stats gracefully.
        pass

    if not manager_stats or not manager_stats.get("enabled"):
        return {
            "enabled": False,
            "subscriptions": [],
            "summary": {
                "total_targets": 0,
                "active": 0,
                "disconnected": 0,
                "failed": 0,
            },
        }

    # Get all targets with alert subscription enabled
    all_targets = await repository.get_all_targets(enabled_only=False)
    alert_targets = [t for t in all_targets if t.enable_alert_subscription and t.enabled]

    # Alert counts per (target, severity) for the last 24h — one grouped query
    # instead of loading rows per target.
    since_24h = datetime.now(UTC) - timedelta(hours=24)
    grouped = await repository.count_alerts_by_target_severity(since=since_24h)

    subscriptions = []
    active_count = 0
    disconnected_count = 0
    failed_count = 0

    for target in alert_targets:
        # Find subscriber info from manager stats
        subscriber_info = next(
            (s for s in manager_stats.get("subscribers", []) if s["target_id"] == target.id),
            None,
        )

        critical_count = grouped.get((target.id, "Critical"), 0)
        warning_count = grouped.get((target.id, "Warning"), 0)
        ok_count = grouped.get((target.id, "OK"), 0)
        alerts_24h = critical_count + warning_count + ok_count

        if subscriber_info:
            state = subscriber_info.get("state", "stopped")
            consecutive_failures = subscriber_info.get("consecutive_failures", 0)
            failure_reason = subscriber_info.get("failure_reason")
            time_in_state_hours = subscriber_info.get("time_in_state_hours")
            next_retry_time = subscriber_info.get("next_retry_time")
            last_event = subscriber_info.get("last_event_time")

            # Count by state for summary
            if state == "connected":
                active_count += 1
            elif state in ("reconnecting", "degraded"):
                disconnected_count += 1
            else:
                failed_count += 1

            subscriptions.append(
                {
                    "target_id": target.id,
                    "target_name": target.name,
                    "target_bmc": target.host,
                    "status": state,  # Using enhanced state
                    "consecutive_failures": consecutive_failures,
                    "failure_reason": failure_reason,
                    "time_in_state_hours": time_in_state_hours,
                    "next_retry_time": next_retry_time,
                    "last_event_time": last_event,
                    "alerts_24h": alerts_24h,
                    "critical_count": critical_count,
                    "warning_count": warning_count,
                    "ok_count": ok_count,
                }
            )
        else:
            # Target configured but not subscribed (might be starting up)
            failed_count += 1
            subscriptions.append(
                {
                    "target_id": target.id,
                    "target_name": target.name,
                    "target_bmc": target.host,
                    "status": "not_subscribed",
                    "last_event_time": None,
                    "consecutive_failures": 0,
                    "alerts_24h": 0,
                    "critical_count": 0,
                    "warning_count": 0,
                    "ok_count": 0,
                }
            )

    # Sort by alert count (descending)
    subscriptions.sort(key=lambda x: x["alerts_24h"], reverse=True)

    # Get configured severities to inform UI
    config = get_config()
    configured_severities = config.alerts.severities if config.alerts.enabled else []

    return {
        "enabled": True,
        "subscriptions": subscriptions,
        "summary": {
            "total_targets": len(alert_targets),
            "active": active_count,
            "disconnected": disconnected_count,
            "failed": failed_count,
        },
        "configured_severities": configured_severities,
        # CPER decode backlog/health for operator visibility.
        "cper": {
            "backlog": (manager_stats or {}).get("cper_backlog", {}),
            "decoded": (manager_stats or {}).get("cper_decoded", 0),
            "failed": (manager_stats or {}).get("cper_failed", 0),
        },
    }


# NOTE: alert deletion is intentionally NOT supported. Alerts are managed
# solely by automatic retention (see alerts.retention_days) — manual deletion
# was only "soft" (the next baseline pull could resurface an entry) and is
# removed to avoid confusion.


# ---- HTML UI ----


PAGE_SIZE = 100


@router.get("", response_class=HTMLResponse)
async def alerts_page(
    request: Request,
    hours: int = Query(168, ge=0, description="Hours to look back (default 7 days; 0 = all)"),
    page: int = Query(1, ge=1),
    severity: str | None = Query(None),
    # Accept as a string: the filter form submits an empty value ("") for
    # "All Targets", which would 422 an int query param. Coerce below.
    target_id: str | None = Query(None),
    q: str | None = Query(None, description="Search target/BMC/message"),
    user: str = Depends(get_current_user),
):
    """Render the alerts page with server-side filtering and pagination."""
    repository = get_repository()
    config = get_config()

    since = datetime.now(UTC) - timedelta(hours=hours) if hours > 0 else None

    # Coerce the (possibly empty) target_id form value to an int or None.
    tid: int | None = None
    if target_id and target_id.strip():
        try:
            tid = int(target_id)
        except ValueError:
            tid = None

    # Only Critical/Warning are in scope (OK/informational not collected — see
    # docs/SCALABILITY.md). Severity breakdown is computed within the same
    # window+filters as the list so the header counts match "of N".
    base: dict = {"since": since, "target_id": tid, "search": q or None}
    # One grouped count instead of one COUNT per severity.
    by_sev = await repository.count_alerts_grouped_by_severity(**base)
    window_critical = by_sev.get("Critical", 0)
    window_warning = by_sev.get("Warning", 0)

    if severity in ("Critical", "Warning"):
        filters = {**base, "severity": severity}
        total = window_critical if severity == "Critical" else window_warning
    else:
        filters = {**base, "severity_in": ["Critical", "Warning"]}
        total = window_critical + window_warning

    total_pages = max(1, ceil(total / PAGE_SIZE)) if total else 1
    page = min(page, total_pages)
    offset = (page - 1) * PAGE_SIZE
    alerts = await repository.get_alerts(**filters, limit=PAGE_SIZE, offset=offset)

    targets = await repository.get_all_targets(enabled_only=False)
    configured_severities = config.alerts.severities if config.alerts.enabled else []

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="alerts.html",
        context={
            "alerts": alerts,
            "window_critical": window_critical,
            "window_warning": window_warning,
            "targets": targets,
            "user": user,
            "csrf_token": generate_csrf_token(),
            "configured_severities": configured_severities,
            "selected_hours": hours,
            # Pagination + current filter state (for controls + preserving filters)
            "total": total,
            "page": page,
            "page_size": PAGE_SIZE,
            "total_pages": total_pages,
            "page_start": (offset + 1) if total else 0,
            "page_end": offset + len(alerts),
            "f_severity": severity or "",
            "f_target_id": str(tid) if tid is not None else "",
            "f_q": q or "",
        },
    )
