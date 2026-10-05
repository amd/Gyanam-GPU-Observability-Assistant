# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Additional baseline log-pull tests for previously-uncovered branches.

Covers _get_json error/non-200/bad-body paths, discovery traversal guards,
_collect_members pagination/dedup/cycle/truncation, order_members id-parse
fallback, and _pull_collection + pull_baseline_alerts branches (is_known skip,
reference-only entry fetch, no-content skip, event-stage cursor skip, callback
error, per-collection failure, and the all-collections-failed warning).
"""

from datetime import UTC, datetime

import httpx
from src.redfish.log_baseline import (
    _abs,
    _collect_members,
    _discover_entry_collections,
    _extract_origin,
    _get_json,
    order_members_newest_first,
    pull_baseline_alerts,
)

# --------------------------------------------------------------------------
# Pure helpers: _abs and _extract_origin
# --------------------------------------------------------------------------


def test_abs_variants():
    assert _abs("https://bmc/", "/redfish/v1/x") == "https://bmc/redfish/v1/x"
    assert _abs("https://bmc", "https://other/y") == "https://other/y"
    assert _abs("https://bmc", "http://plain/z") == "http://plain/z"
    assert _abs("https://bmc", "") == ""


def test_extract_origin_variants():
    assert _extract_origin({"OriginOfCondition": {"@odata.id": "/x/1"}}) == "/x/1"
    assert _extract_origin({"OriginOfCondition": "/y/2"}) == "/y/2"
    assert _extract_origin({"Links": {"OriginOfCondition": {"@odata.id": "/z/3"}}}) == "/z/3"
    assert _extract_origin({"OriginOfCondition": 123}) is None  # non-dict/str
    assert _extract_origin({}) is None


BASE = "https://bmc"
ENTRIES = "/redfish/v1/Systems/1/LogServices/Log1/Entries"


# --------------------------------------------------------------------------
# _get_json
# --------------------------------------------------------------------------


async def test_get_json_connect_error_returns_none(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("down"), url=f"{BASE}/x")
    async with httpx.AsyncClient() as c:
        assert await _get_json(c, f"{BASE}/x") is None


async def test_get_json_non_200_returns_none(httpx_mock):
    httpx_mock.add_response(url=f"{BASE}/x", status_code=404)
    async with httpx.AsyncClient() as c:
        assert await _get_json(c, f"{BASE}/x") is None


async def test_get_json_bad_body_returns_none(httpx_mock):
    httpx_mock.add_response(url=f"{BASE}/x", status_code=200, text="<<not json>>")
    async with httpx.AsyncClient() as c:
        assert await _get_json(c, f"{BASE}/x") is None


# --------------------------------------------------------------------------
# _discover_entry_collections: each "skip/continue" guard
# --------------------------------------------------------------------------


async def test_discover_both_roots_fail(httpx_mock):
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Systems", status_code=500)
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Managers", status_code=500)
    async with httpx.AsyncClient() as c:
        assert await _discover_entry_collections(c, BASE) == []


async def test_discover_member_without_id(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Systems",
        json={"Members": ["not-a-dict", {"no": "id"}]},
    )
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Managers", json={"Members": []})
    async with httpx.AsyncClient() as c:
        assert await _discover_entry_collections(c, BASE) == []


async def test_discover_member_doc_fails(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Systems",
        json={"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]},
    )
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Systems/1", status_code=404)
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Managers", json={"Members": []})
    async with httpx.AsyncClient() as c:
        assert await _discover_entry_collections(c, BASE) == []


async def test_discover_no_logservices_ref(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Systems",
        json={"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]},
    )
    # LogServices missing / not a dict -> ls_uri None -> continue.
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Systems/1", json={"LogServices": "bad"})
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Managers", json={"Members": []})
    async with httpx.AsyncClient() as c:
        assert await _discover_entry_collections(c, BASE) == []


async def test_discover_ls_doc_fails(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Systems",
        json={"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]},
    )
    httpx_mock.add_response(
        url=f"{BASE}/redfish/v1/Systems/1",
        json={"LogServices": {"@odata.id": "/redfish/v1/Systems/1/LogServices"}},
    )
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Systems/1/LogServices", status_code=404)
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Managers", json={"Members": []})
    async with httpx.AsyncClient() as c:
        assert await _discover_entry_collections(c, BASE) == []


async def test_discover_ls_member_without_id(httpx_mock):
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
        json={"Members": ["not-a-dict"]},
    )
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Managers", json={"Members": []})
    async with httpx.AsyncClient() as c:
        assert await _discover_entry_collections(c, BASE) == []


async def test_discover_ls_detail_fails(httpx_mock):
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
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Systems/1/LogServices/Log1", status_code=404)
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Managers", json={"Members": []})
    async with httpx.AsyncClient() as c:
        assert await _discover_entry_collections(c, BASE) == []


async def test_discover_happy_returns_entries_uri(httpx_mock):
    _mock_discovery(httpx_mock)
    async with httpx.AsyncClient() as c:
        cols = await _discover_entry_collections(c, BASE)
    assert cols == [ENTRIES]


# --------------------------------------------------------------------------
# _collect_members: cycle guard, page-fail, non-dict, dedup, truncation
# --------------------------------------------------------------------------


async def test_collect_members_cyclic_nextlink_breaks(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE}{ENTRIES}",
        json={
            "Members": [{"@odata.id": f"{ENTRIES}/1"}],
            "Members@odata.nextLink": ENTRIES,  # points back to self
        },
    )
    async with httpx.AsyncClient() as c:
        members, truncated = await _collect_members(c, BASE, ENTRIES, max_entries=10)
    assert len(members) == 1
    assert truncated is False


async def test_collect_members_first_page_fails(httpx_mock):
    httpx_mock.add_response(url=f"{BASE}{ENTRIES}", status_code=500)
    async with httpx.AsyncClient() as c:
        members, truncated = await _collect_members(c, BASE, ENTRIES, max_entries=10)
    assert members == []


async def test_collect_members_skips_non_dict(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE}{ENTRIES}",
        json={"Members": ["x", {"@odata.id": f"{ENTRIES}/1"}]},
    )
    async with httpx.AsyncClient() as c:
        members, _ = await _collect_members(c, BASE, ENTRIES, max_entries=10)
    assert [m["@odata.id"] for m in members] == [f"{ENTRIES}/1"]


async def test_collect_members_dedups_across_pages(httpx_mock):
    page2 = f"{ENTRIES}?page=2"
    httpx_mock.add_response(
        url=f"{BASE}{ENTRIES}",
        json={
            "Members": [{"@odata.id": f"{ENTRIES}/1"}],
            "Members@odata.nextLink": page2,
        },
    )
    httpx_mock.add_response(
        url=f"{BASE}{page2}",
        json={"Members": [{"@odata.id": f"{ENTRIES}/1"}, {"@odata.id": f"{ENTRIES}/2"}]},
    )
    async with httpx.AsyncClient() as c:
        members, _ = await _collect_members(c, BASE, ENTRIES, max_entries=10)
    ids = [m["@odata.id"] for m in members]
    assert ids == [f"{ENTRIES}/1", f"{ENTRIES}/2"]  # duplicate /1 dropped


async def test_collect_members_truncates_on_budget(httpx_mock):
    # 3 members on page 1 with a nextLink, max_entries=1 -> 3 >= 1*3 -> stop,
    # truncated because a nextLink remains. Page 2 is never requested.
    httpx_mock.add_response(
        url=f"{BASE}{ENTRIES}",
        json={
            "Members": [
                {"@odata.id": f"{ENTRIES}/1"},
                {"@odata.id": f"{ENTRIES}/2"},
                {"@odata.id": f"{ENTRIES}/3"},
            ],
            "Members@odata.nextLink": f"{ENTRIES}?page=2",
        },
    )
    async with httpx.AsyncClient() as c:
        members, truncated = await _collect_members(c, BASE, ENTRIES, max_entries=1)
    assert len(members) == 3
    assert truncated is True


# --------------------------------------------------------------------------
# order_members_newest_first: numeric-id parse fallback
# --------------------------------------------------------------------------


def test_order_members_non_numeric_id_fallback():
    members = [
        {"@odata.id": "/e/abc"},  # non-numeric -> (0, 0.0)
        {"@odata.id": "/e/5"},  # numeric -> (1, 5.0)
    ]
    out = order_members_newest_first(members, max_entries=5)
    # Numeric id sorts ahead of the un-parseable one.
    assert out[0]["@odata.id"] == "/e/5"


def test_order_members_trims_to_max():
    members = [{"@odata.id": "/e/1"}, {"@odata.id": "/e/2"}, {"@odata.id": "/e/3"}]
    out = order_members_newest_first(members, max_entries=1)
    assert len(out) == 1


# --------------------------------------------------------------------------
# Discovery tree helper + integration pulls
# --------------------------------------------------------------------------


def _mock_discovery(httpx_mock):
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
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Managers", json={"Members": []})


async def _pull(httpx_mock, callback, **kw):
    kw.setdefault("severities", ["Critical", "Warning"])
    return await pull_baseline_alerts(
        target_id=1,
        target_name="gpu-a",
        target_bmc="10.0.0.5",
        base_url=BASE,
        username="u",
        password="p",
        verify_ssl=False,
        callback=callback,
        **kw,
    )


async def test_pull_empty_members_returns_zero(httpx_mock):
    _mock_discovery(httpx_mock)
    httpx_mock.add_response(url=f"{BASE}{ENTRIES}", json={"Members": []})
    got = []
    n = await _pull(httpx_mock, got.append)
    assert n == 0
    assert got == []


async def test_pull_is_known_skips_timestampless(httpx_mock):
    _mock_discovery(httpx_mock)
    # No "Created" -> timestamp-less; is_known True -> skipped before any fetch.
    httpx_mock.add_response(
        url=f"{BASE}{ENTRIES}",
        json={"Members": [{"@odata.id": f"{ENTRIES}/1", "Severity": "Critical", "Message": "m"}]},
    )

    async def is_known(_ref):
        return True

    got = []
    n = await _pull(httpx_mock, got.append, is_known=is_known)
    assert n == 0


async def test_pull_is_known_error_then_fetches_full_entry(httpx_mock):
    _mock_discovery(httpx_mock)
    # Reference-only listing member, no Created -> is_known raises (caught),
    # then the missing-field fetch pulls the full entry.
    httpx_mock.add_response(
        url=f"{BASE}{ENTRIES}",
        json={"Members": [{"@odata.id": f"{ENTRIES}/1"}]},
    )
    httpx_mock.add_response(
        url=f"{BASE}{ENTRIES}/1",
        json={
            "@odata.id": f"{ENTRIES}/1",
            "Severity": "Critical",
            "Message": "fetched event",
            "Created": "2026-06-01T10:00:00Z",
            "MessageId": "T.1",
        },
    )

    async def is_known(_ref):
        raise RuntimeError("db unavailable")

    got = []
    n = await _pull(httpx_mock, got.append, is_known=is_known)
    assert n == 1
    assert got[0].message == "fetched event"


async def test_pull_skips_entry_without_content(httpx_mock):
    _mock_discovery(httpx_mock)
    httpx_mock.add_response(
        url=f"{BASE}{ENTRIES}",
        json={"Members": [{"@odata.id": f"{ENTRIES}/1"}]},
    )
    # Fetched entry has no Message / Severity / MessageSeverity -> skipped.
    httpx_mock.add_response(
        url=f"{BASE}{ENTRIES}/1",
        json={"@odata.id": f"{ENTRIES}/1", "Created": "2026-06-01T10:00:00Z", "Id": "1"},
    )
    got = []
    n = await _pull(httpx_mock, got.append)
    assert n == 0


async def test_pull_event_stage_cursor_skip(httpx_mock):
    _mock_discovery(httpx_mock)
    # Listing member has no Created (passes the pre-filter), but the fetched
    # entry's Created is older than the cursor -> skipped at the event stage.
    httpx_mock.add_response(
        url=f"{BASE}{ENTRIES}",
        json={"Members": [{"@odata.id": f"{ENTRIES}/1"}]},
    )
    httpx_mock.add_response(
        url=f"{BASE}{ENTRIES}/1",
        json={
            "@odata.id": f"{ENTRIES}/1",
            "Severity": "Critical",
            "Message": "old",
            "Created": "2026-01-01T00:00:00Z",
        },
    )

    async def get_cursor(_uri):
        return datetime(2026, 6, 1, tzinfo=UTC)  # newer than the entry

    got = []
    n = await _pull(httpx_mock, got.append, get_cursor=get_cursor)
    assert n == 0


async def test_pull_callback_error_is_caught(httpx_mock):
    _mock_discovery(httpx_mock)
    httpx_mock.add_response(
        url=f"{BASE}{ENTRIES}",
        json={
            "Members": [
                {
                    "@odata.id": f"{ENTRIES}/1",
                    "Severity": "Critical",
                    "Message": "boom",
                    "Created": "2026-06-01T10:00:00Z",
                }
            ]
        },
    )

    def bad_callback(_alert):
        raise RuntimeError("callback failed")

    # Callback raising is swallowed; emitted stays 0.
    n = await _pull(httpx_mock, bad_callback)
    assert n == 0


async def test_pull_all_collections_fail_warns(httpx_mock):
    # get_cursor raises for the single discovered collection -> per-collection
    # failure handler, and failures == len(collections) -> all-failed warning.
    _mock_discovery(httpx_mock)

    async def get_cursor(_uri):
        raise RuntimeError("cursor store down")

    got = []
    n = await _pull(httpx_mock, got.append, get_cursor=get_cursor)
    assert n == 0


async def test_pull_no_collections_discovered(httpx_mock):
    # Both roots empty -> no collections -> debug log + early return 0.
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Systems", json={"Members": []})
    httpx_mock.add_response(url=f"{BASE}/redfish/v1/Managers", json={"Members": []})
    got = []
    n = await _pull(httpx_mock, got.append)
    assert n == 0


async def test_pull_prefilter_cursor_skips_old_listing(httpx_mock):
    _mock_discovery(httpx_mock)
    # Listing member carries its OWN Created older than the cursor -> skipped
    # by the cheap pre-filter before any per-entry fetch.
    httpx_mock.add_response(
        url=f"{BASE}{ENTRIES}",
        json={
            "Members": [
                {
                    "@odata.id": f"{ENTRIES}/1",
                    "Severity": "Critical",
                    "Message": "stale",
                    "Created": "2026-01-01T00:00:00Z",
                }
            ]
        },
    )

    async def get_cursor(_uri):
        return datetime(2026, 6, 1, tzinfo=UTC)

    got = []
    n = await _pull(httpx_mock, got.append, get_cursor=get_cursor)
    assert n == 0


async def test_pull_truncated_page_budget_logs(httpx_mock):
    _mock_discovery(httpx_mock)
    # 3 inline, complete members + a nextLink, max_entries_per_log=1 -> the
    # collector hits its budget (3 >= 1*3) and reports truncated.
    members = [
        {
            "@odata.id": f"{ENTRIES}/{i}",
            "Severity": "Critical",
            "Message": f"event {i}",
            "Created": f"2026-06-0{i}T10:00:00Z",
        }
        for i in (1, 2, 3)
    ]
    httpx_mock.add_response(
        url=f"{BASE}{ENTRIES}",
        json={"Members": members, "Members@odata.nextLink": f"{ENTRIES}?page=2"},
    )
    got = []
    n = await _pull(httpx_mock, got.append, max_entries_per_log=1)
    assert n == 1  # trimmed to the newest single entry


async def test_pull_happy_emits_and_advances_cursor(httpx_mock):
    _mock_discovery(httpx_mock)
    httpx_mock.add_response(
        url=f"{BASE}{ENTRIES}",
        json={
            "Members": [
                {
                    "@odata.id": f"{ENTRIES}/1",
                    "Severity": "Critical",
                    "Message": "event 1",
                    "Created": "2026-06-01T10:00:00Z",
                    "MessageId": "T.1",
                    "EntryType": "Event",
                }
            ]
        },
    )

    saved = {}

    async def get_cursor(_uri):
        return datetime(2026, 1, 1, tzinfo=UTC)

    async def set_cursor(uri, dt):
        saved[uri] = dt

    got = []
    n = await _pull(httpx_mock, got.append, get_cursor=get_cursor, set_cursor=set_cursor)
    assert n == 1
    assert got[0].severity == "Critical"
    assert saved  # cursor advanced to the newest seen
