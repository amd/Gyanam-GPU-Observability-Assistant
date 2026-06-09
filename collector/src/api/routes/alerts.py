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

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...config import get_config
from ..auth import get_current_user
from ..csrf import generate_csrf_token, validate_csrf_token
from ..dependencies import get_repository

router = APIRouter()

# Collector service alert manager stats endpoint (internal docker network)
COLLECTOR_ALERT_STATS_URL = "http://collector:8081/alerts/manager-stats"


# ---- JSON API ----


@router.get("/api", summary="List all alerts")
async def list_alerts_api(
    target_id: int | None = Query(None),
    severity: str | None = Query(None),
    hours: int = Query(24, description="Hours to look back"),
    limit: int = Query(100, le=1000),
    offset: int = Query(0),
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
            "event_timestamp": alert.event_timestamp.isoformat() if alert.event_timestamp else None,
            "received_at": alert.received_at.isoformat(),
        }
        for alert in alerts
    ]


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

    # Get alert counts per target (last 24 hours)
    since_24h = datetime.now(UTC) - timedelta(hours=24)

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

        # Get alert counts for this target
        alerts = await repository.get_alerts(target_id=target.id, since=since_24h, limit=1000)
        critical_count = sum(1 for a in alerts if a.severity == "Critical")
        warning_count = sum(1 for a in alerts if a.severity == "Warning")
        ok_count = sum(1 for a in alerts if a.severity == "OK")

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
                    "alerts_24h": len(alerts),
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
    }


@router.delete("/api/{alert_id}", summary="Delete an alert")
async def delete_alert_api(
    alert_id: int,
    user: str = Depends(get_current_user),
):
    """Delete a specific alert."""
    repository = get_repository()
    alert = await repository.delete_alert(alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"message": "Alert deleted successfully"}


@router.delete("/api/clear", summary="Clear alerts")
async def clear_alerts_api(
    target_id: int | None = Query(None),
    severity: str | None = Query(None),
    user: str = Depends(get_current_user),
):
    """Clear multiple alerts based on filters."""
    repository = get_repository()

    if target_id:
        count = await repository.delete_alerts_by_target(target_id)
        return {"message": f"Deleted {count} alerts for target {target_id}"}

    # Clear alerts older than retention (30 days)
    cutoff = datetime.now(UTC) - timedelta(days=30)
    count = await repository.delete_alerts_before(cutoff)
    return {"message": f"Deleted {count} old alerts"}


@router.post("/api/subscription/{target_id}/retry", summary="Retry failed subscription")
async def retry_subscription_api(
    target_id: int,
    user: str = Depends(get_current_user),
):
    """Manually retry a failed or circuit-open subscription.

    Note: This endpoint is not supported in the API service as the alert manager
    runs in the collector service. Retry functionality should be implemented
    via collector service API if needed.
    """
    # Alert manager runs in collector service, not API service
    raise HTTPException(
        status_code=501,
        detail="Manual retry not supported in API service. Alert manager runs in collector service.",
    )


# ---- HTML UI ----


@router.get("", response_class=HTMLResponse)
async def alerts_page(request: Request, user: str = Depends(get_current_user)):
    """Render the alerts page."""
    repository = get_repository()
    config = get_config()

    # Get alerts from last 24 hours by default
    since = datetime.now(UTC) - timedelta(hours=24)
    alerts = await repository.get_alerts(since=since, limit=500)

    # Get statistics
    stats = await repository.get_alert_stats()

    # Get all targets for filter dropdown
    targets = await repository.get_all_targets(enabled_only=False)

    # Get configured severities for dynamic UI rendering
    configured_severities = config.alerts.severities if config.alerts.enabled else []

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="alerts.html",
        context={
            "alerts": alerts,
            "stats": stats,
            "targets": targets,
            "user": user,
            "csrf_token": generate_csrf_token(),
            "configured_severities": configured_severities,
        },
    )


@router.post("/{alert_id}/delete")
async def delete_alert_form(
    alert_id: int,
    csrf_token: str = Form(...),
    user: str = Depends(get_current_user),
):
    """Handle delete alert form submission."""
    validate_csrf_token(csrf_token)
    repository = get_repository()

    alert = await repository.delete_alert(alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    return RedirectResponse(url="/alerts", status_code=303)


@router.post("/clear")
async def clear_alerts_form(
    csrf_token: str = Form(...),
    user: str = Depends(get_current_user),
):
    """Handle clear all alerts form submission."""
    validate_csrf_token(csrf_token)
    repository = get_repository()

    # Clear alerts older than 30 days
    cutoff = datetime.now(UTC) - timedelta(days=30)
    await repository.delete_alerts_before(cutoff)

    return RedirectResponse(url="/alerts", status_code=303)
