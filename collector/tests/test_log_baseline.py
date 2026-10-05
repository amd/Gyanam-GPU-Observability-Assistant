# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for baseline log pull traversal, filtering, and incremental cursors."""

from datetime import UTC, datetime

from src.redfish.log_baseline import (
    _extract_origin,
    pull_baseline_alerts,
)

BASE = "https://bmc"
ENTRIES = "/redfish/v1/Systems/1/LogServices/Log1/Entries"


def _mock_tree(httpx_mock, entries):
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Systems",
        json={"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]},
    )
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Systems/1",
        json={"LogServices": {"@odata.id": "/redfish/v1/Systems/1/LogServices"}},
    )
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Systems/1/LogServices",
        json={"Members": [{"@odata.id": "/redfish/v1/Systems/1/LogServices/Log1"}]},
    )
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Systems/1/LogServices/Log1",
        json={"Entries": {"@odata.id": ENTRIES}},
    )
    httpx_mock.add_response(url=f"{BASE}{ENTRIES}", json={"Members": entries})
    # Managers root: no managers.
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Managers", json={"Members": []})


def _entry(idx, severity, created="2026-06-01T10:00:00Z"):
    return {
        "@odata.id": f"{ENTRIES}/{idx}",
        "Id": str(idx),
        "Severity": severity,
        "Message": f"event {idx}",
        "MessageId": "T.1",
        "Created": created,
        "EntryType": "Event",
        "MessageArgs": ["GPU0"],
    }


async def _pull(httpx_mock, entries, severities=None, **kw):
    _mock_tree(httpx_mock, entries)
    got = []
    n = await pull_baseline_alerts(
        target_id=1,
        target_name="n1",
        target_bmc="10.0.0.1",
        base_url=BASE,
        username="u",
        password="p",
        verify_ssl=False,
        callback=got.append,
        severities=severities if severities is not None else ["Critical", "Warning"],
        **kw,
    )
    return n, got


async def test_pull_filters_by_severity(httpx_mock):
    n, got = await _pull(httpx_mock, [_entry(1, "Critical"), _entry(2, "OK")])
    assert n == 1
    assert got[0].severity == "Critical"
    assert got[0].message == "event 1"
    # received_at is "now"; event_timestamp is the parsed Created.
    assert got[0].event_timestamp is not None


async def test_pull_incremental_cursor_skips_old(httpx_mock):
    # Cursor newer than the entry's Created -> entry skipped.
    async def get_cursor(_uri):
        return datetime(2026, 12, 1, tzinfo=UTC)

    saved = {}

    async def set_cursor(uri, dt):
        saved[uri] = dt

    n, got = await _pull(
        httpx_mock,
        [_entry(1, "Critical", created="2026-06-01T10:00:00Z")],
        get_cursor=get_cursor,
        set_cursor=set_cursor,
    )
    assert n == 0  # older than cursor


async def test_pull_advances_cursor(httpx_mock):
    async def get_cursor(_uri):
        return datetime(2026, 1, 1, tzinfo=UTC)

    saved = {}

    async def set_cursor(uri, dt):
        saved[uri] = dt

    n, _ = await _pull(
        httpx_mock,
        [_entry(1, "Critical", created="2026-06-01T10:00:00Z")],
        get_cursor=get_cursor,
        set_cursor=set_cursor,
    )
    assert n == 1
    assert any("Entries" in uri for uri in saved)


async def test_pull_no_collections(httpx_mock):
    # Empty Systems and Managers -> nothing discovered.
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Systems", json={"Members": []})
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Managers", json={"Members": []})
    got = []
    n = await pull_baseline_alerts(
        target_id=1,
        target_name="n",
        target_bmc="b",
        base_url=BASE,
        username="u",
        password="p",
        verify_ssl=False,
        callback=got.append,
        severities=["Critical"],
    )
    assert n == 0


def test_extract_origin_variants():
    assert _extract_origin({"OriginOfCondition": {"@odata.id": "/x/1"}}) == "/x/1"
    assert _extract_origin({"OriginOfCondition": "/y/2"}) == "/y/2"
    assert _extract_origin({"Links": {"OriginOfCondition": {"@odata.id": "/z/3"}}}) == "/z/3"
    assert _extract_origin({}) is None


# ---- pure helpers ----

from src.redfish.log_baseline import (  # noqa: E402
    _abs,
    _naive_utc,
    order_members_newest_first,
)


def test_abs_resolves():
    assert _abs("https://bmc/", "/redfish/v1/x") == "https://bmc/redfish/v1/x"
    assert _abs("https://bmc", "https://other/y") == "https://other/y"
    assert _abs("https://bmc", "") == ""


def test_order_members_newest_first_by_created():
    members = [
        {"@odata.id": "/e/1", "Created": "2026-01-01T00:00:00Z"},
        {"@odata.id": "/e/2", "Created": "2026-06-01T00:00:00Z"},
        {"@odata.id": "/e/3", "Created": "2026-03-01T00:00:00Z"},
    ]
    out = order_members_newest_first(members, max_entries=2)
    assert [m["@odata.id"] for m in out] == ["/e/2", "/e/3"]  # newest 2


def test_order_members_newest_first_by_numeric_id():
    members = [{"@odata.id": "/e/10"}, {"@odata.id": "/e/2"}, {"@odata.id": "/e/40"}]
    out = order_members_newest_first(members, max_entries=3)
    assert [m["@odata.id"] for m in out] == ["/e/40", "/e/10", "/e/2"]


def test_naive_utc_coerces():
    aware = datetime.now(UTC)  # tz-aware
    assert _naive_utc(aware).tzinfo is None
    naive = datetime(2026, 4, 1, 12, 0)  # already naive
    assert _naive_utc(naive) == naive
    assert _naive_utc(None) is None
