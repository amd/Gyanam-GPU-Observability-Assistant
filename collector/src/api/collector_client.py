# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Thin async client for the collector service's internal HTTP surface.

The API container talks to the collector over the internal docker network
(``http://collector:8081``) for stats/health proxies. These calls must never
turn a transient collector outage into a 500 for the UI, so the shared helper
degrades to ``None`` on any error (logging the exception type) rather than
raising or silently swallowing — a bare ``except: pass`` would also hide real
programming errors.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

# Internal docker-network base URL for the collector's control/stats surface.
COLLECTOR_BASE_URL = "http://collector:8081"


async def get_json(path: str, timeout: float = 10.0) -> dict | None:
    """GET JSON from the collector service.

    ``path`` is appended to :data:`COLLECTOR_BASE_URL`. Returns the parsed JSON
    object on a 200 response, or ``None`` on any httpx transport error, a non-200
    status, or an unparseable body. Never raises; a warning is logged with the
    exception type so failures stay diagnosable without leaking detail.
    """
    url = f"{COLLECTOR_BASE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
        if response.status_code != 200:
            logger.warning("Collector GET %s returned HTTP %d", path, response.status_code)
            return None
        return response.json()  # type: ignore[no-any-return]
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("Collector GET %s failed: %s", path, type(e).__name__)
        return None
