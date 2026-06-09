# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Unit tests for alert parsing/normalization helpers (no DB required)."""

from datetime import UTC, datetime

from src.redfish.alert_subscriber import (
    normalize_severity,
    parse_redfish_timestamp,
    severity_allowed,
)
from src.redfish.log_baseline import order_members_newest_first

# ---- parse_redfish_timestamp ----


def test_parse_sloppy_non_zero_padded():
    # Some BMCs emit non-zero-padded components; fromisoformat rejects these.
    assert parse_redfish_timestamp("2026-4-16T8:58:6Z") == datetime(
        2026, 4, 16, 8, 58, 6, tzinfo=UTC
    )


def test_parse_strict_iso_with_z():
    assert parse_redfish_timestamp("2026-06-27T12:07:26Z") == datetime(
        2026, 6, 27, 12, 7, 26, tzinfo=UTC
    )


def test_parse_with_offset():
    dt = parse_redfish_timestamp("2026-04-16T08:58:06-07:00")
    assert dt is not None and dt.utcoffset().total_seconds() == -7 * 3600


def test_parse_invalid_returns_none():
    assert parse_redfish_timestamp("") is None
    assert parse_redfish_timestamp(None) is None
    assert parse_redfish_timestamp("not-a-date") is None


# ---- normalize_severity / severity_allowed ----


def test_normalize_prefers_message_severity():
    assert normalize_severity({"MessageSeverity": "Critical"}) == ("Critical", True)
    assert normalize_severity({"Severity": "Warning"}) == ("Warning", True)
    assert normalize_severity({}) == ("OK", False)


def test_severity_allowed_missing_always_passes():
    # An absent severity must not be silently dropped.
    assert severity_allowed("OK", False, ["Critical", "Warning"]) is True


def test_severity_allowed_present_filtered():
    assert severity_allowed("OK", True, ["Critical", "Warning"]) is False
    assert severity_allowed("Critical", True, ["Critical", "Warning"]) is True


def test_severity_allowed_case_insensitive():
    assert severity_allowed("critical", True, ["Critical"]) is True


# ---- order_members_newest_first ----


def test_order_by_created_newest_first_and_cap():
    members = [
        {"@odata.id": "/e/1", "Created": "2026-01-01T00:00:00Z"},
        {"@odata.id": "/e/2", "Created": "2026-03-01T00:00:00Z"},
        {"@odata.id": "/e/3", "Created": "2026-02-01T00:00:00Z"},
    ]
    out = order_members_newest_first(members, max_entries=2)
    assert [m["@odata.id"] for m in out] == ["/e/2", "/e/3"]


def test_order_newest_first_regardless_of_input_order():
    # Newest-first input must still yield newest-first output.
    members = [
        {"@odata.id": "/e/9", "Created": "2026-05-01T00:00:00Z"},
        {"@odata.id": "/e/8", "Created": "2026-04-01T00:00:00Z"},
    ]
    out = order_members_newest_first(members, max_entries=10)
    assert [m["@odata.id"] for m in out] == ["/e/9", "/e/8"]


def test_order_falls_back_to_numeric_id():
    members = [
        {"@odata.id": "/redfish/v1/.../Entries/5"},
        {"@odata.id": "/redfish/v1/.../Entries/42"},
        {"@odata.id": "/redfish/v1/.../Entries/7"},
    ]
    out = order_members_newest_first(members, max_entries=2)
    assert [m["@odata.id"].split("/")[-1] for m in out] == ["42", "7"]
