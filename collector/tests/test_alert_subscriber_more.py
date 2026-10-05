# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for AlertSubscriber error classification, backoff, and SSE parsing."""

import json

import httpx
from src.redfish.alert_subscriber import AlertEvent, AlertSubscriber, ErrorCategory


def _sub(callback=None, severities=None):
    return AlertSubscriber(
        target_id=1,
        target_name="n1",
        target_bmc="10.0.0.1",
        base_url="https://bmc",
        username="u",
        password="p",
        callback=callback,
        severities=severities or ["Critical", "Warning"],
    )


# ---- backoff ----


def test_backoff_schedule():
    # Delays carry ±20% jitter (anti-thundering-herd), so assert on the band
    # around each base value rather than the exact number.
    s = _sub()
    s._consecutive_failures = 1
    assert 24 <= s._calculate_backoff_delay() <= 36  # ~30
    s._consecutive_failures = 2
    assert 48 <= s._calculate_backoff_delay() <= 72  # ~60
    s._consecutive_failures = 100
    assert 5760 <= s._calculate_backoff_delay() <= 8640  # ~7200 capped base


# ---- error classification ----


def test_classify_http_404_permanent():
    s = _sub()
    cat, _reason = s._classify_error(RuntimeError("SSE connection failed: HTTP 404 not found"))
    assert cat == ErrorCategory.PERMANENT


def test_classify_http_401_permanent():
    s = _sub()
    cat, _ = s._classify_error(RuntimeError("SSE connection failed: HTTP 401 unauthorized"))
    assert cat == ErrorCategory.PERMANENT


def test_classify_transient_default():
    s = _sub()
    cat, _ = s._classify_error(ValueError("some transient hiccup"))
    assert cat == ErrorCategory.TRANSIENT


def test_classify_connect_error_transient_when_few_failures():
    s = _sub()
    s._consecutive_failures = 1
    cat, _ = s._classify_error(httpx.ConnectError("boom"))
    assert cat == ErrorCategory.TRANSIENT


# ---- SSE event parsing/filtering ----


def test_process_event_stores_critical():
    seen: list[AlertEvent] = []
    s = _sub(callback=seen.append)
    s._process_event_data(
        json.dumps(
            {
                "Events": [
                    {"MessageSeverity": "Critical", "Message": "over temp", "MessageId": "T.1"}
                ]
            }
        )
    )
    assert len(seen) == 1
    assert seen[0].severity == "Critical"


def test_process_event_filters_ok():
    seen: list[AlertEvent] = []
    s = _sub(callback=seen.append)
    s._process_event_data(
        json.dumps({"Events": [{"MessageSeverity": "OK", "Message": "fine", "MessageId": "T.2"}]})
    )
    assert seen == []


def test_process_event_missing_severity_is_kept():
    # Absent severity must not be silently dropped (stored as OK).
    seen: list[AlertEvent] = []
    s = _sub(callback=seen.append)
    s._process_event_data(
        json.dumps({"Events": [{"Message": "no severity field", "MessageId": "T.3"}]})
    )
    assert len(seen) == 1
    assert seen[0].severity == "OK"


# ---- _connect_and_listen (streamed SSE via httpx_mock) ----

SSE_URL = "https://bmc/redfish/v1/EventService/SSE"


async def test_connect_and_listen_processes_event(httpx_mock):
    captured = []
    sub = _sub(callback=captured.append)
    sub._running = True  # the listen loop checks this per line
    body = (
        b"data: "
        + json.dumps({"Events": [{"Severity": "Critical", "Message": "hot"}]}).encode()
        + b"\n\n"
    )
    httpx_mock.add_response(
        method="GET",
        url=SSE_URL,
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=body,
    )
    await sub._connect_and_listen()
    assert len(captured) == 1 and captured[0].message == "hot"


async def test_connect_and_listen_non_200_raises(httpx_mock):
    import pytest

    sub = _sub()
    httpx_mock.add_response(method="GET", url=SSE_URL, status_code=404, content=b"nope")
    with pytest.raises(RuntimeError):
        await sub._connect_and_listen()
