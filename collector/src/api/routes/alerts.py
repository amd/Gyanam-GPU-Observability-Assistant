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

import logging
from datetime import UTC, datetime, timedelta
from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from ...config import get_config
from ..auth import get_current_user
from ..collector_client import get_json
from ..csrf import generate_csrf_token
from ..dependencies import get_repository

router = APIRouter()
logger = logging.getLogger(__name__)

# Collector service alert manager stats path (internal docker network).
COLLECTOR_ALERT_STATS_PATH = "/alerts/manager-stats"


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


def _permanent_failure_retries(manager_stats: dict) -> dict[int, str | None]:
    """Return retry deadlines keyed by target ID.

    ``datetime.max`` means auto-retry is disabled. Older collectors return only
    target IDs, so ignore entries without a retry deadline during rolling upgrades.
    """
    retries: dict[int, str | None] = {}
    entries = manager_stats.get("permanent_failure_retries")
    if entries is None:
        entries = manager_stats.get("permanently_failed_targets") or []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("target_id") is None:
            continue
        next_retry = entry.get("next_retry_at")
        try:
            parsed = datetime.fromisoformat(next_retry) if next_retry else None
        except (TypeError, ValueError):
            parsed = None
        retries[entry["target_id"]] = next_retry if parsed and parsed.year < 9999 else None
    return retries


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
    """Get alert manager runtime statistics, aggregated across all shards.

    Prefers the shared-DB aggregate (correct under ``--scale collector=N``);
    falls back to the single-collector HTTP proxy before any collector has
    published. Collector unreachable / non-200 -> the UI still loads with the
    "alerts disabled" state rather than throwing 500.
    """
    repository = get_repository()
    stats = await _aggregate_collector_stats(repository)
    if stats is None:
        stats = await get_json(COLLECTOR_ALERT_STATS_PATH, timeout=5.0)
    if stats is not None:
        return stats
    return {"enabled": False}


async def _aggregate_collector_stats(repository) -> dict | None:
    """Merge every live collector's stats into one fleet view.

    With sharding each collector owns a disjoint slice, so subscriber/retry lists
    concatenate and counters sum. Returns None when no collector has published yet,
    or when the control DB is unreachable — in both cases the caller falls back to
    the single-collector HTTP proxy / empty state rather than 500ing the alerts UI.
    """
    try:
        rows = await repository.get_collector_stats()
    except Exception as e:  # noqa: BLE001 — a DB blip must degrade, not 500 the UI
        logger.warning("Collector-stats aggregation failed (%s); falling back", e)
        return None
    if not rows:
        return None

    # Process rows oldest-first so that, during a lease handoff where two
    # collectors transiently report the same target, the newest owner's entry
    # wins the per-target dedup.
    rows = sorted(rows, key=lambda r: r.get("updated_at") or "")

    # Per-target dedup: one subscriber / retry / failed entry per target_id,
    # regardless of how many shards momentarily claim it. Subscription counts
    # are then DERIVED from the deduped sets rather than summed across shards,
    # so the handoff window can't inflate them.
    subscribers_by_tid: dict = {}
    retries_by_tid: dict = {}
    failed_targets: dict = {}

    # True cumulative counters: each is produced once per collector (an alert is
    # received/written/dropped by whichever collector owns the target; CPER and
    # baseline counters are per-collector work), so summing across shards does not
    # double-count.
    cumulative = (
        "alerts_received",
        "alerts_written",
        "alerts_dropped",
        "queue_size",
        "cper_decoded",
        "cper_failed",
        "alerts_baseline_pulled",
        "baseline_jobs_active",
    )
    sums = dict.fromkeys(cumulative, 0)
    # cper_backlog is a per-status dict; merge by summing each status across shards.
    backlog: dict = {}

    enabled = False
    for r in rows:
        if "alerts_received" in r:
            enabled = True
        for sub in r.get("subscribers", []):
            tid = sub.get("target_id")
            subscribers_by_tid[tid] = sub  # newest wins (rows sorted ascending)
        for ret in r.get("permanent_failure_retries", []):
            retries_by_tid[ret.get("target_id") if isinstance(ret, dict) else ret] = ret
        for tgt in r.get("permanently_failed_targets", []):
            failed_targets[tgt] = tgt
        for k in cumulative:
            sums[k] += r.get(k) or 0
        for status, count in (r.get("cper_backlog") or {}).items():
            backlog[status] = backlog.get(status, 0) + (count or 0)

    subscribers = list(subscribers_by_tid.values())
    merged: dict = {
        "subscribers": subscribers,
        "permanent_failure_retries": list(retries_by_tid.values()),
        "permanently_failed_targets": list(failed_targets.values()),
        "active_subscriptions": len(subscribers),
        "sse_subscriptions": sum(1 for s in subscribers if s.get("subscription_type") == "sse"),
        "webhook_subscriptions": sum(
            1 for s in subscribers if s.get("subscription_type") == "webhook"
        ),
        "permanently_failed": len(failed_targets),
        "cper_backlog": backlog,
        **sums,
    }
    if not enabled:
        # No collector has published real alert data yet — e.g. only the initial
        # membership row (published before the alert stats task's first run), or
        # alerts genuinely disabled. Return None so the caller falls back to the
        # single-collector HTTP proxy, which reports the true state, instead of
        # showing "alerts disabled" during the startup window.
        return None
    merged["enabled"] = enabled
    merged["collector_count"] = len(rows)
    return merged


@router.get("/api/subscription-status", summary="Get detailed subscription status")
async def get_subscription_status_api(user: str = Depends(get_current_user)):
    """Get detailed alert subscription status per target with alert counts."""
    repository = get_repository()

    # Prefer the shared-DB aggregate (works across N sharded collectors); fall
    # back to the single-collector HTTP proxy before any collector has published.
    # On any error the helper returns None and the empty-state response is used.
    manager_stats = await _aggregate_collector_stats(repository)
    if manager_stats is None:
        manager_stats = await get_json(COLLECTOR_ALERT_STATS_PATH, timeout=5.0)

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

    perm_failed = _permanent_failure_retries(manager_stats)

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
            # Permanent webhook failures are absent from the subscriber list.
            failed_count += 1
            perm_failed_retry = perm_failed.get(target.id)
            is_perm_failed = target.id in perm_failed
            subscriptions.append(
                {
                    "target_id": target.id,
                    "target_name": target.name,
                    "target_bmc": target.host,
                    "status": "failed_permanent" if is_perm_failed else "not_subscribed",
                    "next_retry_time": perm_failed_retry,
                    "auto_retry_disabled": is_perm_failed and perm_failed_retry is None,
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
    # Guard the alert-store (Postgres) reads so a DB outage renders an empty page
    # with a banner rather than 500ing the whole alerts UI.
    alert_store_error = False
    try:
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
    except Exception as e:  # noqa: BLE001 — degrade the page, don't 500 on a DB blip
        logger.warning("Alert store unavailable; rendering empty alerts page: %s", e)
        window_critical = window_warning = total = 0
        total_pages = 1
        page = 1
        offset = 0
        alerts = []
        alert_store_error = True

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
            "alert_store_error": alert_store_error,
        },
    )
