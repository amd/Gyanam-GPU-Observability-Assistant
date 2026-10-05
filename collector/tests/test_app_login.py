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


async def test_login_refused_when_default_password_not_allowed(noauth_client, monkeypatch):
    """With the default 'changeme' hash and no allow-flag, login fails closed."""
    import src.api.auth as auth

    monkeypatch.delenv("GYANAM_ALLOW_DEFAULT_PASSWORD", raising=False)
    # Sanity: the test config really is still on the default hash.
    assert auth.default_password_blocks_login(auth._DEFAULT_HASH) is True
    r = await noauth_client.post(
        "/login",
        data={"username": "admin", "password": "changeme", "csrf_token": generate_csrf_token()},
        follow_redirects=False,
    )
    assert r.status_code == 401


async def test_cookie_authenticates_subsequent_request(noauth_client):
    login = await noauth_client.post(
        "/login",
        data={"username": "admin", "password": "changeme", "csrf_token": generate_csrf_token()},
        follow_redirects=False,
    )
    assert login.status_code == 303
    # httpx client retains the Set-Cookie; the protected index must now authorise
    # (authed -> redirect to /systems; unauthed would bounce to /login).
    r = await noauth_client.get("/", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert r.headers.get("location") == "/systems"


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
