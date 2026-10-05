# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the small util/http/api helper modules.

Covers:
  * src/util/timeutil.py       -- naive-UTC normalization helpers
  * src/redfish/http_client.py -- make_bmc_client factory
  * src/api/collector_client.py-- get_json degrade-to-None behaviour
"""

from datetime import UTC, datetime, timedelta, timezone

import httpx

# ---------------------------------------------------------------------------
# src/util/timeutil.py
# ---------------------------------------------------------------------------
from src.util.timeutil import naive_utc_now, to_naive_utc


def test_to_naive_utc_none():
    assert to_naive_utc(None) is None


def test_to_naive_utc_naive_unchanged():
    naive = datetime(2026, 4, 1, 12, 0, 0)
    assert to_naive_utc(naive) is naive
    assert to_naive_utc(naive).tzinfo is None


def test_to_naive_utc_aware_is_converted_and_stripped():
    # 09:00 at +05:00 == 04:00 UTC, tz stripped.
    aware = datetime(2026, 4, 1, 9, 0, 0, tzinfo=timezone(timedelta(hours=5)))
    out = to_naive_utc(aware)
    assert out.tzinfo is None
    assert out == datetime(2026, 4, 1, 4, 0, 0)


def test_naive_utc_now_is_naive_and_recent():
    now = naive_utc_now()
    assert now.tzinfo is None
    # Within a minute of real "now" (compared naive-to-naive).
    delta = abs((datetime.now(UTC).replace(tzinfo=None) - now).total_seconds())
    assert delta < 60


# ---------------------------------------------------------------------------
# src/redfish/http_client.py
# ---------------------------------------------------------------------------
from src.redfish.http_client import _BMC_LIMITS, make_bmc_client  # noqa: E402


def test_bmc_limits_are_bounded():
    assert _BMC_LIMITS.max_connections == 10
    assert _BMC_LIMITS.max_keepalive_connections == 4


async def test_make_bmc_client_defaults():
    client = make_bmc_client(verify_ssl=False, timeout=30.0)
    try:
        assert isinstance(client, httpx.AsyncClient)
        # All phases default to `timeout` when read_timeout is unset.
        assert client.timeout.connect == 30.0
        assert client.timeout.read == 30.0
        assert client.timeout.write == 30.0
        assert client.timeout.pool == 30.0
        assert client.follow_redirects is True
    finally:
        await client.aclose()


async def test_make_bmc_client_unbounded_read():
    client = make_bmc_client(verify_ssl=True, timeout=15.0, read_timeout=None)
    try:
        # read_timeout=None leaves the read phase unbounded, others bounded.
        assert client.timeout.read is None
        assert client.timeout.connect == 15.0
    finally:
        await client.aclose()


async def test_make_bmc_client_explicit_read_timeout():
    client = make_bmc_client(verify_ssl=False, timeout=20.0, read_timeout=5.0)
    try:
        assert client.timeout.read == 5.0
        assert client.timeout.connect == 20.0
    finally:
        await client.aclose()


async def test_make_bmc_client_follow_redirects_false():
    client = make_bmc_client(verify_ssl=False, timeout=10.0, follow_redirects=False)
    try:
        assert client.follow_redirects is False
    finally:
        await client.aclose()


async def test_make_bmc_client_with_auth():
    auth = httpx.BasicAuth("u", "p")
    client = make_bmc_client(verify_ssl=False, timeout=10.0, auth=auth)
    try:
        assert client.auth is auth
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# src/api/collector_client.py
# ---------------------------------------------------------------------------
from src.api.collector_client import COLLECTOR_BASE_URL, get_json  # noqa: E402


async def test_get_json_200_returns_dict(httpx_mock):
    httpx_mock.add_response(url=f"{COLLECTOR_BASE_URL}/stats", json={"ok": True, "count": 3})
    out = await get_json("/stats")
    assert out == {"ok": True, "count": 3}


async def test_get_json_non_200_returns_none(httpx_mock):
    httpx_mock.add_response(url=f"{COLLECTOR_BASE_URL}/stats", status_code=503)
    assert await get_json("/stats") is None


async def test_get_json_connect_error_returns_none(httpx_mock):
    httpx_mock.add_exception(
        httpx.ConnectError("collector down"), url=f"{COLLECTOR_BASE_URL}/health"
    )
    assert await get_json("/health") is None


async def test_get_json_bad_body_returns_none(httpx_mock):
    # 200 but unparseable body -> response.json() raises ValueError -> None.
    httpx_mock.add_response(
        url=f"{COLLECTOR_BASE_URL}/stats", status_code=200, text="not json at all"
    )
    assert await get_json("/stats") is None


def test_collector_base_url_is_internal():
    assert COLLECTOR_BASE_URL == "http://collector:8081"
