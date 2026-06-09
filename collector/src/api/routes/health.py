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

import logging

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ...config import get_settings
from ..dependencies import get_log_collector, get_repository

logger = logging.getLogger(__name__)

router = APIRouter()

# Collector service health endpoint (internal docker network)
COLLECTOR_HEALTH_URL = "http://collector:8081/health/detailed"


@router.get("/health")
async def health_check():
    """Basic health check endpoint."""
    return {"status": "healthy"}


@router.get("/health/detailed")
async def detailed_health_check():
    """Detailed health check with both API and Collector service status.

    This endpoint queries the collector service's internal health endpoint
    and combines it with API service status for a complete view.
    """
    repository = get_repository()
    log_collector = get_log_collector()
    settings = get_settings()

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

    # Query collector service health (separate docker container)
    collector_health = None
    collector_error = None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(COLLECTOR_HEALTH_URL)
            if response.status_code == 200:
                collector_health = response.json()
    except httpx.RequestError as e:
        collector_error = f"Cannot reach collector service: {str(e)}"
    except httpx.TimeoutException:
        collector_error = "Collector service timeout"
    except Exception as e:
        collector_error = f"Error querying collector: {str(e)}"

    # Determine overall health
    api_healthy = db_healthy and log_collector_healthy
    collector_healthy = collector_health and collector_health.get("status") == "healthy"
    overall_healthy = api_healthy and collector_healthy

    result = {
        "status": "healthy" if overall_healthy else "degraded",
        "metrics_backend": settings.metrics_backend,
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

    return result


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
