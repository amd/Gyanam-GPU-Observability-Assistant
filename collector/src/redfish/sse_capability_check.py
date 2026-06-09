#!/usr/bin/env python3
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
"""SSE capability detection for Redfish BMCs.

This module provides utilities to detect whether a BMC supports SSE alerts
before attempting to establish long-lived SSE connections.
"""

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum

import httpx

logger = logging.getLogger(__name__)


class SSESupport(Enum):
    """SSE support status."""

    SUPPORTED = "supported"  # SSE endpoint available and working
    NOT_SUPPORTED = "not_supported"  # BMC doesn't support SSE (404, 501)
    BROKEN = "broken"  # SSE advertised but doesn't work properly
    UNKNOWN = "unknown"  # Could not determine (network error, timeout)


@dataclass
class SSECapabilityResult:
    """Result of SSE capability check."""

    support: SSESupport
    reason: str
    event_service_enabled: bool = False
    sse_endpoint: str | None = None
    test_duration_ms: float = 0


async def check_sse_capability(
    base_url: str,
    username: str,
    password: str,
    verify_ssl: bool = False,
    test_duration_seconds: float = 5.0,
) -> SSECapabilityResult:
    """Check if BMC supports SSE alerts with a quick test connection.

    Args:
        base_url: Redfish API base URL
        username: Authentication username
        password: Authentication password
        verify_ssl: Whether to verify SSL certificates
        test_duration_seconds: How long to test SSE connection

    Returns:
        SSECapabilityResult indicating support status
    """
    import time

    start_time = time.time()

    # Step 1: Check if EventService exists and is enabled
    try:
        async with httpx.AsyncClient(
            auth=httpx.BasicAuth(username, password),
            verify=verify_ssl,
            timeout=10.0,
        ) as client:
            # Check EventService
            event_service_url = f"{base_url}/redfish/v1/EventService"
            response = await client.get(event_service_url)

            if response.status_code == 404:
                duration_ms = (time.time() - start_time) * 1000
                return SSECapabilityResult(
                    support=SSESupport.NOT_SUPPORTED,
                    reason="EventService not found (404)",
                    test_duration_ms=duration_ms,
                )

            if response.status_code != 200:
                duration_ms = (time.time() - start_time) * 1000
                return SSECapabilityResult(
                    support=SSESupport.UNKNOWN,
                    reason=f"EventService returned HTTP {response.status_code}",
                    test_duration_ms=duration_ms,
                )

            event_service = response.json()
            service_enabled = event_service.get("ServiceEnabled", False)

            # Check if SSE endpoint is advertised
            sse_uri = event_service.get("ServerSentEventUri")
            if not sse_uri:
                duration_ms = (time.time() - start_time) * 1000
                return SSECapabilityResult(
                    support=SSESupport.NOT_SUPPORTED,
                    reason="ServerSentEventUri not advertised in EventService",
                    event_service_enabled=service_enabled,
                    test_duration_ms=duration_ms,
                )

            # Step 2: Test SSE endpoint with short connection
            sse_endpoint = sse_uri if sse_uri.startswith("http") else f"{base_url}{sse_uri}"
            test_result = await _test_sse_endpoint(
                sse_endpoint, username, password, verify_ssl, test_duration_seconds
            )

            duration_ms = (time.time() - start_time) * 1000
            test_result.event_service_enabled = service_enabled
            test_result.sse_endpoint = sse_uri
            test_result.test_duration_ms = duration_ms

            return test_result

    except httpx.TimeoutException:
        duration_ms = (time.time() - start_time) * 1000
        return SSECapabilityResult(
            support=SSESupport.UNKNOWN,
            reason="Timeout checking EventService",
            test_duration_ms=duration_ms,
        )
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        return SSECapabilityResult(
            support=SSESupport.UNKNOWN,
            reason=f"Error checking EventService: {type(e).__name__}: {e}",
            test_duration_ms=duration_ms,
        )


async def _test_sse_endpoint(
    sse_url: str,
    username: str,
    password: str,
    verify_ssl: bool,
    test_duration: float,
) -> SSECapabilityResult:
    """Test SSE endpoint with a short connection.

    Args:
        sse_url: Full SSE endpoint URL
        username: Authentication username
        password: Authentication password
        verify_ssl: Whether to verify SSL
        test_duration: How long to listen for events (seconds)

    Returns:
        SSECapabilityResult
    """
    try:
        timeout = httpx.Timeout(10.0, read=test_duration + 2.0)

        async with (
            httpx.AsyncClient(
                auth=httpx.BasicAuth(username, password),
                verify=verify_ssl,
                timeout=timeout,
            ) as client,
            client.stream("GET", sse_url) as response,
        ):
            # Check initial status
            if response.status_code == 404:
                return SSECapabilityResult(
                    support=SSESupport.NOT_SUPPORTED,
                    reason="SSE endpoint not found (404)",
                )

            if response.status_code == 501:
                return SSECapabilityResult(
                    support=SSESupport.NOT_SUPPORTED,
                    reason="SSE not implemented (501)",
                )

            if response.status_code != 200:
                return SSECapabilityResult(
                    support=SSESupport.BROKEN,
                    reason=f"SSE endpoint returned HTTP {response.status_code}",
                )

            # Check content type
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type.lower():
                return SSECapabilityResult(
                    support=SSESupport.BROKEN,
                    reason=f"Wrong content-type: {content_type} (expected text/event-stream)",
                )

            # Listen for test duration to see if we get data
            lines_received = 0
            events_received = 0
            keepalives_received = 0

            try:
                async with asyncio.timeout(test_duration):
                    async for line in response.aiter_lines():
                        lines_received += 1
                        line = line.strip()

                        if not line:
                            continue

                        if line.startswith(":"):
                            # SSE keep-alive comment
                            keepalives_received += 1
                        elif line.startswith("data:"):
                            # Actual event data
                            events_received += 1

            except TimeoutError:
                # Test duration elapsed, check what we received
                pass

            # Evaluate results
            if lines_received == 0:
                # Stream opened but no data at all
                return SSECapabilityResult(
                    support=SSESupport.BROKEN,
                    reason="SSE stream opened but no data received (not even keep-alives)",
                )

            if keepalives_received > 0 or events_received > 0:
                # We got keep-alives or events - SSE is working!
                return SSECapabilityResult(
                    support=SSESupport.SUPPORTED,
                    reason=f"SSE working ({events_received} events, {keepalives_received} keep-alives in {test_duration}s test)",
                )

            # Got some lines but they weren't keep-alives or events
            return SSECapabilityResult(
                support=SSESupport.BROKEN,
                reason=f"SSE stream active but invalid format ({lines_received} lines received)",
            )

    except httpx.ConnectError as e:
        return SSECapabilityResult(
            support=SSESupport.UNKNOWN,
            reason=f"Cannot connect to SSE endpoint: {e}",
        )
    except httpx.TimeoutException:
        return SSECapabilityResult(
            support=SSESupport.UNKNOWN,
            reason="Timeout connecting to SSE endpoint",
        )
    except Exception as e:
        return SSECapabilityResult(
            support=SSESupport.BROKEN,
            reason=f"SSE test failed: {type(e).__name__}: {e}",
        )


async def batch_check_sse_capability(
    targets: list[dict],
    concurrency: int = 10,
) -> dict[int, SSECapabilityResult]:
    """Check SSE capability for multiple targets concurrently.

    Args:
        targets: List of target dicts with keys: id, name, host, username, password
        concurrency: Maximum concurrent checks

    Returns:
        Dict mapping target_id to SSECapabilityResult
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def check_one(target: dict) -> tuple[int, SSECapabilityResult]:
        async with semaphore:
            base_url = f"https://{target['host']}"
            result = await check_sse_capability(
                base_url=base_url,
                username=target["username"],
                password=target["password"],
                verify_ssl=target.get("verify_ssl", False),
            )
            logger.info(
                f"SSE capability check for {target['name']}: {result.support.value} - {result.reason}"
            )
            return target["id"], result

    tasks = [check_one(target) for target in targets]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    capability_map = {}
    for result in results:
        if isinstance(result, BaseException):
            logger.error(f"SSE capability check failed: {result}")
            continue
        if isinstance(result, tuple) and len(result) == 2:
            target_id, cap_result = result
            capability_map[target_id] = cap_result

    return capability_map
