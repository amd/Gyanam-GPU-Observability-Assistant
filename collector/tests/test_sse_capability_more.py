# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for SSE endpoint probing (the streamed _test_sse_endpoint branches)."""

from src.redfish.sse_capability_check import (
    SSESupport,
    batch_check_sse_capability,
    check_sse_capability,
)

BASE = "https://bmc"
EVENT_SERVICE = f"{BASE}/redfish/v1/EventService"
SSE_URL = f"{BASE}/redfish/v1/EventService/SSE"


def _advertise_sse(httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url=EVENT_SERVICE,
        status_code=200,
        json={
            "Id": "EventService",
            "ServiceEnabled": True,
            "ServerSentEventUri": "/redfish/v1/EventService/SSE",
        },
    )


async def test_sse_supported(httpx_mock):
    _advertise_sse(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=SSE_URL,
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=b':keep-alive\ndata: {"x": 1}\n',
    )
    r = await check_sse_capability(BASE, "u", "p", test_duration_seconds=0.3)
    assert r.support == SSESupport.SUPPORTED


async def test_sse_404_not_supported(httpx_mock):
    _advertise_sse(httpx_mock)
    httpx_mock.add_response(method="GET", url=SSE_URL, status_code=404)
    r = await check_sse_capability(BASE, "u", "p", test_duration_seconds=0.3)
    assert r.support == SSESupport.NOT_SUPPORTED


async def test_sse_501_not_supported(httpx_mock):
    _advertise_sse(httpx_mock)
    httpx_mock.add_response(method="GET", url=SSE_URL, status_code=501)
    r = await check_sse_capability(BASE, "u", "p", test_duration_seconds=0.3)
    assert r.support == SSESupport.NOT_SUPPORTED


async def test_sse_non_200_broken(httpx_mock):
    _advertise_sse(httpx_mock)
    httpx_mock.add_response(method="GET", url=SSE_URL, status_code=503)
    r = await check_sse_capability(BASE, "u", "p", test_duration_seconds=0.3)
    assert r.support == SSESupport.BROKEN


async def test_sse_wrong_content_type_broken(httpx_mock):
    _advertise_sse(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=SSE_URL,
        status_code=200,
        headers={"content-type": "application/json"},
        content=b"{}",
    )
    r = await check_sse_capability(BASE, "u", "p", test_duration_seconds=0.3)
    assert r.support == SSESupport.BROKEN


async def test_sse_empty_stream_broken(httpx_mock):
    _advertise_sse(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=SSE_URL,
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=b"",
    )
    r = await check_sse_capability(BASE, "u", "p", test_duration_seconds=0.3)
    assert r.support == SSESupport.BROKEN


async def test_batch_check(httpx_mock):
    _advertise_sse(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=SSE_URL,
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=b":ka\n",
    )
    targets = [{"id": 7, "name": "n", "host": "bmc", "username": "u", "password": "p"}]
    results = await batch_check_sse_capability(targets, concurrency=2)
    assert 7 in results and results[7].support == SSESupport.SUPPORTED
