# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Extra AlertSubscriber coverage: lifecycle, subscribe-loop branches, SSE line
parsing, error classification table, timestamp parsing edges, and property
accessors. Pure/in-process only (no real network)."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from src.redfish import alert_subscriber
from src.redfish.alert_subscriber import (
    AlertEvent,
    AlertSubscriber,
    ErrorCategory,
    SubscriptionState,
    parse_redfish_timestamp,
)


def _sub(callback=None, severities=None, **kw):
    return AlertSubscriber(
        target_id=1,
        target_name="gpu-a",
        target_bmc="10.0.0.5",
        base_url="https://bmc",
        username="u",
        password="p",
        callback=callback,
        severities=severities or ["Critical", "Warning"],
        **kw,
    )


# ---- parse_redfish_timestamp edge cases ----


def test_parse_fractional_with_offset_via_regex():
    # Non-zero-padded + fractional + explicit offset exercises the regex path.
    dt = parse_redfish_timestamp("2026-4-16T8:58:6.5+05:30")
    assert dt is not None
    assert dt.utcoffset().total_seconds() == 5 * 3600 + 30 * 60
    assert dt.microsecond == 500000


def test_parse_regex_assumes_utc_when_no_offset():
    dt = parse_redfish_timestamp("2026-4-16 8:58:6")
    assert dt == datetime(2026, 4, 16, 8, 58, 6, tzinfo=UTC)


def test_parse_regex_impossible_date_returns_none():
    # Matches the shape but the component values are invalid -> ValueError -> None.
    assert parse_redfish_timestamp("2026-13-40T25:70:99Z") is None


def test_parse_non_string_returns_none():
    assert parse_redfish_timestamp(12345) is None  # type: ignore[arg-type]


# ---- lifecycle: start / stop / resume ----


async def _park(self=None):
    # A stand-in connection that parks until the task is cancelled.
    import asyncio

    await asyncio.Event().wait()


async def test_start_is_idempotent_when_running():
    s = _sub()
    s._running = True
    s._state = SubscriptionState.CONNECTED
    await s.start()  # already running -> warns and returns, no task spawned
    assert s._task is None
    assert s.state == SubscriptionState.CONNECTED


async def test_start_then_stop():
    import asyncio

    s = _sub()
    s._connect_and_listen = _park  # keep the loop parked, no network
    await s.start()
    assert s.is_running is True
    assert s.state == SubscriptionState.RECONNECTING
    await asyncio.sleep(0)  # let the loop body begin
    await s.stop()
    assert s.is_running is False
    assert s.state == SubscriptionState.STOPPED


async def test_resume_when_stopped_restarts():
    s = _sub()
    s._consecutive_failures = 7
    s._failure_reason = "boom"
    s._connect_and_listen = _park
    await s.resume()
    try:
        assert s.is_running is True
        assert s._consecutive_failures == 0
        assert s._failure_reason is None
    finally:
        await s.stop()


async def test_resume_when_running_resets_state_only():
    s = _sub()
    s._running = True
    s._state = SubscriptionState.DEGRADED
    s._consecutive_failures = 4
    await s.resume()
    assert s.is_running is True
    assert s._task is None  # no new task when already running
    assert s.state == SubscriptionState.RECONNECTING
    assert s._consecutive_failures == 0


# ---- _subscribe_loop branches ----


async def test_subscribe_loop_permanent_error_stops():
    s = _sub()

    async def boom():
        raise RuntimeError("SSE connection failed: HTTP 401 unauthorized")

    s._connect_and_listen = boom
    s._running = True
    await s._subscribe_loop()
    assert s.state == SubscriptionState.FAILED_PERMANENT
    assert s.is_running is False


async def test_subscribe_loop_transient_error_backs_off(monkeypatch):
    s = _sub()

    async def boom():
        s._running = False  # exit after a single transient iteration
        raise ValueError("hiccup")

    async def nosleep(*_a, **_k):
        return None

    monkeypatch.setattr(alert_subscriber.asyncio, "sleep", nosleep)
    s._connect_and_listen = boom
    s._running = True
    await s._subscribe_loop()
    assert s.consecutive_failures == 1
    assert s.failure_reason is not None
    assert s.next_retry_time is not None
    assert s.state in (SubscriptionState.RECONNECTING, SubscriptionState.DEGRADED)


async def test_subscribe_loop_enters_cooldown_on_max_retry(monkeypatch):
    # max_retry_duration_hours=0 makes the elapsed check trip immediately.
    s = _sub(max_retry_duration_hours=0)

    async def boom():
        s._running = False
        raise ValueError("hiccup")

    async def nosleep(*_a, **_k):
        return None

    monkeypatch.setattr(alert_subscriber.asyncio, "sleep", nosleep)
    s._connect_and_listen = boom
    s._running = True
    await s._subscribe_loop()
    assert s._cooldown_start_time is not None
    assert s.state == SubscriptionState.ON_COOLDOWN


async def test_subscribe_loop_still_in_cooldown(monkeypatch):
    s = _sub(cooldown_duration_hours=6)
    s._cooldown_start_time = datetime.now(UTC)

    async def stop_after(*_a, **_k):
        s._running = False  # break out of the cooldown-wait loop

    monkeypatch.setattr(alert_subscriber.asyncio, "sleep", stop_after)
    s._running = True
    await s._subscribe_loop()
    assert s.state == SubscriptionState.ON_COOLDOWN


async def test_subscribe_loop_cooldown_expired_auto_resume():
    s = _sub(cooldown_duration_hours=6)
    # Cooldown started 7h ago (> duration) -> expired -> auto-resume path.
    s._cooldown_start_time = datetime.now(UTC) - timedelta(hours=7)

    async def conn():
        s._running = False  # normal connection end -> loop exits cleanly

    s._connect_and_listen = conn
    s._running = True
    await s._subscribe_loop()
    assert s._cooldown_start_time is None
    assert s._consecutive_failures == 0


# ---- SSE line parsing in _connect_and_listen ----

SSE_URL = "https://bmc/redfish/v1/EventService/SSE"


async def test_connect_and_listen_multiline_keepalive_and_fields(httpx_mock):
    captured: list[AlertEvent] = []
    s = _sub(callback=captured.append)
    s._running = True
    # Keepalive comment, an event/id field line, a bare line w/o colon, and a
    # multi-line `data:` payload that must be joined before JSON parsing.
    # Split the JSON across two data: lines at a safe token boundary; SSE joins
    # them with "\n" (valid JSON whitespace) before parsing.
    body = (
        b":keepalive\n"
        b"event: alert\n"
        b"id: 42\n"
        b"barefield\n"
        b'data: {"Events": [{"Severity": "Critical",\n'
        b'data:  "Message": "hot"}]}\n'
        b"\n"
    )
    httpx_mock.add_response(
        method="GET",
        url=SSE_URL,
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=body,
    )
    await s._connect_and_listen()
    assert len(captured) == 1
    assert captured[0].message == "hot"
    assert s.state == SubscriptionState.CONNECTED


async def test_quiet_keepalive_stream_not_flagged_closed_too_fast(httpx_mock, monkeypatch):
    # A1 regression: a healthy SSE stream that has been connected a long time and
    # has only sent keep-alives (no events yet) must NOT trip the "closed <30s
    # without events" invalid-endpoint check. Previously each keep-alive reset
    # connect_time, so on any reconnect the check fired and the target was wrongly
    # marked FAILED_PERMANENT. Clock is pinned so the stream appears 40s old.
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    calls = {"n": 0}

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            calls["n"] += 1
            # First now() is connect_time; everything after is +40s (stream age).
            return t0 if calls["n"] == 1 else t0 + timedelta(seconds=40)

    monkeypatch.setattr(alert_subscriber, "datetime", _FakeDT)
    s = _sub()
    s._running = True
    httpx_mock.add_response(
        method="GET",
        url=SSE_URL,
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=b":keepalive\n" * 5,  # only keep-alives, no events, then EOF
    )
    await s._connect_and_listen()  # must NOT raise "without sending events"
    assert s.state != SubscriptionState.FAILED_PERMANENT


async def test_connect_and_listen_blank_payload_is_ignored(httpx_mock):
    captured: list[AlertEvent] = []
    s = _sub(callback=captured.append)
    s._running = True
    # An empty data payload dispatched at the blank line must be skipped, and a
    # trailing (unterminated) event is flushed at stream end.
    good = json.dumps({"Events": [{"Severity": "Warning", "Message": "warm"}]})
    body = b"data: \n\n" + b"data: " + good.encode() + b"\n"
    httpx_mock.add_response(
        method="GET",
        url=SSE_URL,
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=body,
    )
    await s._connect_and_listen()
    assert [e.message for e in captured] == ["warm"]


# ---- _process_event_data branches ----


def test_process_event_type_filtered_out():
    seen: list[AlertEvent] = []
    s = _sub(callback=seen.append)
    s.event_types = ["Alert"]
    s._process_event_data(
        json.dumps(
            {"Events": [{"EventType": "StatusChange", "Severity": "Critical", "Message": "x"}]}
        )
    )
    assert seen == []


def test_process_event_origin_as_string_and_dict():
    seen: list[AlertEvent] = []
    s = _sub(callback=seen.append)
    s._process_event_data(
        json.dumps(
            {
                "Events": [
                    {"Severity": "Critical", "Message": "a", "OriginOfCondition": "/redfish/str"},
                    {
                        "Severity": "Warning",
                        "Message": "b",
                        "OriginOfCondition": {"@odata.id": "/redfish/dict"},
                    },
                ]
            }
        )
    )
    assert [e.origin_of_condition for e in seen] == ["/redfish/str", "/redfish/dict"]


def test_process_event_callback_exception_is_swallowed():
    def bad(_ev):
        raise RuntimeError("callback blew up")

    s = _sub(callback=bad)
    # Must not propagate — a bad callback can't kill the subscriber.
    s._process_event_data(json.dumps({"Events": [{"Severity": "Critical", "Message": "x"}]}))


# ---- _classify_error table ----


def test_classify_connection_refused_transient_then_permanent():
    s = _sub()
    s._consecutive_failures = 1
    cat, _ = s._classify_error(OSError("Connection refused by host"))
    assert cat == ErrorCategory.TRANSIENT
    s._consecutive_failures = 11
    cat, _ = s._classify_error(OSError("Connection refused by host"))
    assert cat == ErrorCategory.PERMANENT


def test_classify_connect_error_extended_is_permanent():
    s = _sub()
    s._consecutive_failures = 11
    cat, _ = s._classify_error(httpx.ConnectError("generic connect fail"))
    assert cat == ErrorCategory.PERMANENT


def test_classify_ssl_certificate_permanent():
    # Use a non-ConnectError/OSError so classification reaches the SSL branch
    # (ConnectError/OSError are caught earlier as generic network errors).
    s = _sub()
    cat, reason = s._classify_error(RuntimeError("certificate verify failed: self signed"))
    assert cat == ErrorCategory.PERMANENT
    assert "SSL" in reason


def test_classify_invalid_endpoint_permanent():
    s = _sub()
    err = RuntimeError("SSE stream closed after 2.0s without sending events")
    cat, _ = s._classify_error(err)
    assert cat == ErrorCategory.PERMANENT


def test_classify_timeout_transient_then_permanent():
    s = _sub()
    s._consecutive_failures = 1
    cat, _ = s._classify_error(httpx.ReadTimeout("slow"))
    assert cat == ErrorCategory.TRANSIENT
    s._consecutive_failures = 16
    cat, _ = s._classify_error(httpx.ReadTimeout("slow"))
    assert cat == ErrorCategory.PERMANENT


def test_classify_network_unreachable_escalates():
    # Non-OSError so the "network unreachable" string branch is reachable.
    s = _sub()
    s._consecutive_failures = 2
    cat, _ = s._classify_error(RuntimeError("Network unreachable"))
    assert cat == ErrorCategory.TRANSIENT
    s._consecutive_failures = 6
    cat, _ = s._classify_error(RuntimeError("Network unreachable"))
    assert cat == ErrorCategory.PERMANENT


def test_classify_connection_reset_transient():
    s = _sub()
    cat, reason = s._classify_error(RuntimeError("Connection reset by peer"))
    assert cat == ErrorCategory.TRANSIENT
    assert "reset" in reason.lower()


def test_classify_http_status_error_401_permanent():
    s = _sub()
    req = httpx.Request("GET", SSE_URL)
    resp = httpx.Response(401, request=req)
    err = httpx.HTTPStatusError("unauth", request=req, response=resp)
    cat, _ = s._classify_error(err)
    assert cat == ErrorCategory.PERMANENT


@pytest.mark.parametrize(
    "msg",
    [
        "SSE connection failed: HTTP 403 forbidden",
        "SSE connection failed: HTTP 404 nope",
        "SSE connection failed: HTTP 405 method",
        "SSE connection failed: HTTP 501 not impl",
    ],
)
def test_classify_runtime_http_codes_permanent(msg):
    s = _sub()
    cat, _ = s._classify_error(RuntimeError(msg))
    assert cat == ErrorCategory.PERMANENT


# ---- property accessors ----


def test_property_accessors_defaults_and_setters():
    s = _sub()
    assert s.is_running is False
    assert s.consecutive_failures == 0
    assert s.last_event_time is None
    assert s.state == SubscriptionState.STOPPED
    assert s.failure_reason is None
    assert s.next_retry_time is None
    assert s.time_in_current_state is None

    now = datetime.now(UTC)
    s._last_event_time = now
    s._failure_reason = "degraded"
    s._next_retry_time = now
    assert s.last_event_time == now
    assert s.failure_reason == "degraded"
    assert s.next_retry_time == now


def test_time_in_current_state_when_failing():
    s = _sub()
    s._state = SubscriptionState.DEGRADED
    s._first_failure_time = datetime.now(UTC) - timedelta(hours=2)
    val = s.time_in_current_state
    assert val is not None and val >= 1.9
