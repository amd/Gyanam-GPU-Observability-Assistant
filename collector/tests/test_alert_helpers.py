# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for AlertSubscriber pure helpers: timestamp/severity parsing,
the full error-classification table, and state properties."""

import json
from datetime import UTC, datetime

import httpx
from src.redfish.alert_subscriber import (
    AlertSubscriber,
    ErrorCategory,
    SubscriptionState,
    normalize_severity,
    parse_redfish_timestamp,
    severity_allowed,
)


def _sub():
    return AlertSubscriber(
        target_id=1,
        target_name="n1",
        target_bmc="10.0.0.1",
        base_url="https://bmc",
        username="u",
        password="p",
        severities=["Critical", "Warning"],
    )


# ---- parse_redfish_timestamp ----


def test_ts_iso_with_z():
    dt = parse_redfish_timestamp("2026-04-16T08:58:06Z")
    assert dt.year == 2026 and dt.tzinfo is not None and dt.utcoffset().total_seconds() == 0


def test_ts_iso_with_offset():
    dt = parse_redfish_timestamp("2026-04-16T08:58:06+02:00")
    assert dt.utcoffset().total_seconds() == 2 * 3600


def test_ts_iso_without_tz_assumes_utc():
    dt = parse_redfish_timestamp("2026-04-16T08:58:06")
    assert dt.utcoffset().total_seconds() == 0


def test_ts_sloppy_non_padded_with_frac_and_offset():
    dt = parse_redfish_timestamp("2026-3-5T6:7:8.5-02:30")
    assert (dt.month, dt.day, dt.hour, dt.microsecond) == (3, 5, 6, 500000)
    assert dt.utcoffset().total_seconds() == -(2 * 3600 + 30 * 60)


def test_ts_sloppy_with_z():
    dt = parse_redfish_timestamp("2026-4-16T8:58:6Z")
    assert dt.hour == 8 and dt.utcoffset().total_seconds() == 0


def test_ts_invalid_month_returns_none():
    assert parse_redfish_timestamp("2026-13-05T06:07:08Z") is None


def test_ts_none_and_garbage():
    assert parse_redfish_timestamp(None) is None
    assert parse_redfish_timestamp("") is None
    assert parse_redfish_timestamp("not a date") is None
    assert parse_redfish_timestamp(12345) is None  # non-str


# ---- severity ----


def test_normalize_severity_variants():
    assert normalize_severity({"MessageSeverity": "Critical"}) == ("Critical", True)
    assert normalize_severity({"Severity": "Warning"}) == ("Warning", True)
    assert normalize_severity({}) == ("OK", False)


def test_severity_allowed_rules():
    assert severity_allowed("Critical", present=False, allow_list=["OK"]) is True  # absent -> keep
    assert severity_allowed("Critical", present=True, allow_list=None) is True  # no filter
    assert severity_allowed("critical", present=True, allow_list=["Critical"]) is True  # ci
    assert severity_allowed("OK", present=True, allow_list=["Critical", "Warning"]) is False


# ---- _classify_error full table ----


def _http_status_error(code):
    req = httpx.Request("GET", "https://bmc/sse")
    return httpx.HTTPStatusError("err", request=req, response=httpx.Response(code, request=req))


def test_classify_httpstatuserror_401_permanent():
    cat, _ = _sub()._classify_error(_http_status_error(401))
    assert cat == ErrorCategory.PERMANENT


def test_classify_runtime_http_variants_permanent():
    s = _sub()
    for msg in ["HTTP 403", "HTTP 405", "HTTP 501"]:
        cat, _ = s._classify_error(RuntimeError(f"stream failed: {msg}"))
        assert cat == ErrorCategory.PERMANENT


def test_classify_connection_refused_transient_then_permanent():
    s = _sub()
    s._consecutive_failures = 1
    assert s._classify_error(OSError("Connection refused"))[0] == ErrorCategory.TRANSIENT
    s._consecutive_failures = 11
    assert s._classify_error(OSError("connection refused"))[0] == ErrorCategory.PERMANENT


def test_classify_connect_error_permanent_after_many():
    s = _sub()
    s._consecutive_failures = 11
    assert s._classify_error(httpx.ConnectError("down"))[0] == ErrorCategory.PERMANENT


def test_classify_ssl_cert_permanent():
    # Must be a non-ConnectError/OSError (those are caught earlier) to reach the
    # SSL-cert branch; use a RuntimeError whose message names a cert failure.
    s = _sub()
    cat, reason = s._classify_error(RuntimeError("certificate verify failed: self signed"))
    assert cat == ErrorCategory.PERMANENT and "certificate" in reason.lower()


def test_classify_stream_closes_immediately_permanent():
    s = _sub()
    err = RuntimeError("stream closed after 0.1s without sending events")
    assert s._classify_error(err)[0] == ErrorCategory.PERMANENT


def test_classify_timeout_transient_then_permanent():
    s = _sub()
    s._consecutive_failures = 1
    assert s._classify_error(httpx.TimeoutException("t"))[0] == ErrorCategory.TRANSIENT
    s._consecutive_failures = 16
    assert s._classify_error(httpx.TimeoutException("t"))[0] == ErrorCategory.PERMANENT


def test_classify_network_unreachable_and_reset():
    # RuntimeError (not OSError) so these reach the network-unreachable / reset
    # branches instead of the earlier generic OSError handler.
    s = _sub()
    s._consecutive_failures = 1
    assert s._classify_error(RuntimeError("Network unreachable"))[0] == ErrorCategory.TRANSIENT
    s._consecutive_failures = 6
    assert s._classify_error(RuntimeError("network unreachable"))[0] == ErrorCategory.PERMANENT
    assert s._classify_error(RuntimeError("connection reset by peer"))[0] == ErrorCategory.TRANSIENT


# ---- state properties ----


def test_state_properties():
    s = _sub()
    assert s.is_running is False
    assert s.consecutive_failures == 0
    assert s.state == SubscriptionState.CONNECTED or isinstance(s.state, SubscriptionState)
    # time_in_current_state: None when connected / no failure yet.
    s._state = SubscriptionState.CONNECTED
    assert s.time_in_current_state is None
    # When failing, returns elapsed hours as a float.
    s._state = SubscriptionState.DEGRADED
    s._first_failure_time = datetime.now(UTC)
    val = s.time_in_current_state
    assert isinstance(val, float) and val >= 0


# ---- _process_event_data branches ----


def _sub_cb(event_types):
    captured = []
    s = _sub()
    s.callback = captured.append
    s.event_types = event_types
    return s, captured


def test_process_event_drops_disallowed_event_type():
    s, captured = _sub_cb(event_types=["StatusChange"])
    s._process_event_data(
        json.dumps({"Events": [{"EventType": "Alert", "Severity": "Critical", "Message": "m"}]})
    )
    assert captured == []  # Alert not in allow-list


def test_process_event_origin_as_string_and_default_type():
    s, captured = _sub_cb(event_types=[])
    s._process_event_data(
        json.dumps(
            {
                "Events": [
                    {"Severity": "Critical", "Message": "m", "OriginOfCondition": "/redfish/x"}
                ]
            }
        )
    )
    assert len(captured) == 1
    assert captured[0].origin_of_condition == "/redfish/x"
    assert captured[0].event_type == "Alert"  # absent -> default


def test_process_event_origin_as_dict_and_timestamp():
    s, captured = _sub_cb(event_types=[])
    s._process_event_data(
        json.dumps(
            {
                "Events": [
                    {
                        "Severity": "Warning",
                        "Message": "m",
                        "OriginOfCondition": {"@odata.id": "/redfish/y"},
                        "EventTimestamp": "2026-04-22T10:30:00Z",
                    }
                ]
            }
        )
    )
    assert captured[0].origin_of_condition == "/redfish/y"
    assert captured[0].event_timestamp is not None


def test_process_event_callback_error_is_swallowed():
    s = _sub()

    def boom(_alert):
        raise RuntimeError("callback boom")

    s.callback = boom
    s.event_types = []
    # Must not raise despite the callback blowing up.
    s._process_event_data(json.dumps({"Events": [{"Severity": "Critical", "Message": "m"}]}))
