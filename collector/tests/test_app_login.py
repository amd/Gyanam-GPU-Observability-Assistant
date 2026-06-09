# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the login/logout flow and cookie-based auth (real config)."""

from src.api.auth import SESSION_COOKIE_NAME
from src.api.csrf import generate_csrf_token


async def test_login_success_sets_cookie(noauth_client):
    r = await noauth_client.post(
        "/login",
        data={"username": "admin", "password": "changeme", "csrf_token": generate_csrf_token()},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert SESSION_COOKIE_NAME in r.headers.get("set-cookie", "")


async def test_login_bad_password(noauth_client):
    r = await noauth_client.post(
        "/login",
        data={"username": "admin", "password": "wrong", "csrf_token": generate_csrf_token()},
    )
    assert r.status_code == 401


async def test_login_missing_csrf(noauth_client):
    r = await noauth_client.post("/login", data={"username": "admin", "password": "changeme"})
    assert r.status_code in (403, 422)


async def test_cookie_authenticates_subsequent_request(noauth_client):
    login = await noauth_client.post(
        "/login",
        data={"username": "admin", "password": "changeme", "csrf_token": generate_csrf_token()},
        follow_redirects=False,
    )
    assert login.status_code == 303
    # httpx client retains the Set-Cookie; the protected index must now load.
    r = await noauth_client.get("/")
    assert r.status_code == 200


async def test_logout(noauth_client):
    await noauth_client.post(
        "/login",
        data={"username": "admin", "password": "changeme", "csrf_token": generate_csrf_token()},
        follow_redirects=False,
    )
    r = await noauth_client.post(
        "/logout", data={"csrf_token": generate_csrf_token()}, follow_redirects=False
    )
    assert r.status_code == 303
