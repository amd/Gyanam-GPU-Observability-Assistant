# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Route tests for authentication enforcement."""


async def test_api_route_requires_auth_returns_401(noauth_client):
    r = await noauth_client.get("/alerts/api/stats")
    assert r.status_code == 401


async def test_html_route_redirects_when_unauthenticated(noauth_client):
    # A browser request advertises Accept: text/html -> redirected to login
    # (an API/non-HTML client gets 401 instead).
    r = await noauth_client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/login" in r.headers.get("location", "")


async def test_authenticated_index_ok(client):
    # The index redirects authed users to the canonical Systems page.
    r = await client.get("/", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert r.headers.get("location") == "/systems"


async def test_login_page_public(noauth_client):
    r = await noauth_client.get("/login")
    assert r.status_code == 200
