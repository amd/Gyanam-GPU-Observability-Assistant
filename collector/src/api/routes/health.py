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
"""Health check endpoints for API service.

NOTE: This is the API service which handles:
- Web UI and user interactions
- Target management
- On-demand log collection

The collector service (separate process) handles:
- Metric collection and export
- Redfish polling
- SSE subscriptions
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..auth import get_current_user
from ..collector_client import COLLECTOR_BASE_URL, get_json
from ..dependencies import get_log_collector, get_repository

logger = logging.getLogger(__name__)

router = APIRouter()

# Collector service health endpoint (internal docker network).
COLLECTOR_HEALTH_PATH = "/health/detailed"
COLLECTOR_HEALTH_URL = f"{COLLECTOR_BASE_URL}{COLLECTOR_HEALTH_PATH}"


@router.get("/health")
async def health_check():
    """Basic health check endpoint."""
    return {"status": "healthy"}


@router.get("/health/detailed")
async def detailed_health_check(user: str = Depends(get_current_user)):
    """Detailed health check with both API and Collector service status.

    This endpoint queries the collector service's internal health endpoint
    and combines it with API service status for a complete view.

    Auth-gated: the payload includes the per-target subscriber roster, failure
    reasons and the shard topology — fleet recon an unauthenticated caller must
    not get. The bare ``/health`` above stays open for the Docker healthcheck.
    """
    repository = get_repository()
    log_collector = get_log_collector()

    # Check API service database
    db_healthy = False
    target_count = 0
    try:
        targets = await repository.get_all_targets()
        target_count = len(targets)
        db_healthy = True
        db_message = f"OK ({target_count} targets)"
    except Exception as e:
        logger.warning(f"Database check failed: {e}", exc_info=True)
        db_message = f"unavailable ({type(e).__name__})"

    # Check API service log collector
    log_collector_healthy = True
    log_collector_message = "OK"
    active_collections = 0
    try:
        if hasattr(log_collector, "active_tasks"):
            active_collections = len(log_collector.active_tasks)
            log_collector_message = f"OK ({active_collections} active collections)"
    except Exception as e:
        logger.warning(f"Log collector check failed: {e}", exc_info=True)
        log_collector_healthy = False
        # Don't expose raw exception text to the HTTP response; log it instead.
        log_collector_message = f"unavailable ({type(e).__name__})"

    # Query collector service health (separate docker container). The shared
    # helper returns None on any transport error, non-200, or unparseable body
    # (logging the exception type); surface that as an "unavailable" state and
    # don't expose raw exception text to the HTTP response.
    collector_health = await get_json(COLLECTOR_HEALTH_PATH, timeout=5.0)
    collector_error = None if collector_health else "unavailable (cannot reach collector)"

    # Determine overall health
    api_healthy = db_healthy and log_collector_healthy
    collector_healthy = collector_health and collector_health.get("status") == "healthy"
    overall_healthy = api_healthy and collector_healthy

    result: dict[str, Any] = {
        "status": "healthy" if overall_healthy else "degraded",
        "metrics_backend": "influxdb",
        "api_service": {
            "status": "healthy" if api_healthy else "degraded",
            "components": {
                "database": {"healthy": db_healthy, "message": db_message, "targets": target_count},
                "log_collector": {
                    "healthy": log_collector_healthy,
                    "message": log_collector_message,
                    "active_collections": active_collections,
                },
            },
        },
        "collector_service": {},
    }

    # Add collector service status
    if collector_health:
        result["collector_service"] = collector_health
    else:
        result["collector_service"] = {
            "status": "unavailable",
            "error": collector_error or "Unknown error",
            "components": {},
        }

    # Shard roster (empty for a single collector) — one row per live collector,
    # so operators can see the fleet split across shards.
    try:
        shards = await repository.get_collector_stats()
        if len(shards) > 1:
            result["shards"] = [
                {"collector_id": s.get("collector_id"), "owned_targets": s.get("owned_targets", 0)}
                for s in shards
            ]
    except Exception:  # noqa: BLE001 — diagnostics must never 500 on this extra
        pass

    return result


@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request, user: str = Depends(get_current_user)):
    """Render the Diagnostics page: tool health metrics as formatted JSON."""
    data = await detailed_health_check()
    health_json = json.dumps(data, indent=2, default=str)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="status.html",
        context={
            "health_json": health_json,
            "overall_status": data.get("status", "unknown"),
            "user": user,
        },
    )


@router.get("/ready")
async def readiness_check():
    """Kubernetes-style readiness probe for API service."""
    repository = get_repository()

    try:
        # Just check if database is accessible
        await repository.get_all_targets()
        return {"ready": True, "service": "api"}
    except Exception as e:
        logger.warning(f"Readiness check failed: {e}", exc_info=True)
        # Sanitised reason: don't leak stack-trace details to the probe response.
        return JSONResponse(
            status_code=503,
            content={"ready": False, "service": "api", "reason": type(e).__name__},
        )
