# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for SSE capability probing (early-return paths, no live stream)."""

from src.redfish.sse_capability_check import SSESupport, check_sse_capability

BASE = "https://bmc"
EVENT_SERVICE = f"{BASE}/redfish/v1/EventService"


async def test_event_service_404_not_supported(httpx_mock):
    httpx_mock.add_response(method="GET", url=EVENT_SERVICE, status_code=404)
    result = await check_sse_capability(BASE, "u", "p")
    assert result.support == SSESupport.NOT_SUPPORTED


async def test_event_service_error_unknown(httpx_mock):
    httpx_mock.add_response(method="GET", url=EVENT_SERVICE, status_code=500)
    result = await check_sse_capability(BASE, "u", "p")
    assert result.support in (SSESupport.UNKNOWN, SSESupport.NOT_SUPPORTED)


async def test_event_service_without_sse_uri(httpx_mock):
    # EventService present but advertises no SSE endpoint.
    httpx_mock.add_response(
        method="GET",
        url=EVENT_SERVICE,
        status_code=200,
        json={"Id": "EventService", "ServiceEnabled": True},
    )
    result = await check_sse_capability(BASE, "u", "p")
    assert result.support != SSESupport.SUPPORTED
