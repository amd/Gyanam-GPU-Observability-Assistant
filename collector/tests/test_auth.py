# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for auth helpers: password hashing, session cookies, request typing."""

import time
from types import SimpleNamespace

from src.api import auth

# ---- password hashing ----


def test_hash_and_verify_password():
    h = auth.hash_password("s3cret")
    assert auth.is_valid_bcrypt_hash(h)
    assert auth.verify_password("s3cret", h) is True
    assert auth.verify_password("wrong", h) is False


def test_verify_rejects_bad_hash():
    assert auth.verify_password("x", "") is False
    assert auth.verify_password("x", "not-a-bcrypt-hash") is False


def test_is_valid_bcrypt_hash():
    assert auth.is_valid_bcrypt_hash("$2b$12$" + "a" * 53) is True
    assert auth.is_valid_bcrypt_hash("$1$short") is False
    assert auth.is_valid_bcrypt_hash("") is False


# ---- session cookies ----


def test_session_cookie_roundtrip():
    cookie = auth.create_session_cookie("admin")
    assert auth.validate_session_cookie(cookie) == "admin"


def test_session_cookie_tampered():
    cookie = auth.create_session_cookie("admin")
    user, expiry, _sig = cookie.rsplit(":", 2)
    assert auth.validate_session_cookie(f"{user}:{expiry}:bad") is None


def test_session_cookie_expired():
    import hashlib
    import hmac

    secret = auth._get_session_secret()
    payload = f"admin:{int(time.time()) - 10}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    assert auth.validate_session_cookie(f"{payload}:{sig}") is None


def test_session_cookie_username_with_colon():
    # rsplit-based parsing must handle a colon in the username.
    cookie = auth.create_session_cookie("dom\\user:x")
    assert auth.validate_session_cookie(cookie) == "dom\\user:x"


def test_empty_cookie():
    assert auth.validate_session_cookie("") is None


# ---- request typing ----


def _req(path, accept="text/html"):
    return SimpleNamespace(url=SimpleNamespace(path=path), headers={"accept": accept})


def test_is_api_request_by_path():
    assert auth._is_api_request(_req("/alerts/api/stats")) is True
    assert auth._is_api_request(_req("/targets")) is False


def test_is_api_request_by_accept():
    assert auth._is_api_request(_req("/targets", accept="application/json")) is True
    assert auth._is_api_request(_req("/targets", accept="text/html")) is False
