# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Unit coverage for the auth helpers and get_current_user branches."""

import base64
import hashlib
import hmac
import time

import pytest
from fastapi import HTTPException
from src.api import auth
from starlette.requests import Request


def _request(path="/", headers=None, cookies=None):
    header_list = []
    if headers:
        for k, v in headers.items():
            header_list.append((k.lower().encode(), v.encode()))
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        header_list.append((b"cookie", cookie_str.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": header_list,
        "query_string": b"",
    }
    return Request(scope)


def _basic_header(username: str, password: str) -> str:
    raw = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {raw}"


# ---- verify_password ----------------------------------------------------


def test_verify_password_empty_hash_false():
    assert auth.verify_password("anything", "") is False


def test_verify_password_invalid_format_false():
    assert auth.verify_password("x", "not-a-bcrypt-hash") is False


def test_verify_password_roundtrip():
    h = auth.hash_password("s3cret")
    assert auth.verify_password("s3cret", h) is True
    assert auth.verify_password("wrong", h) is False


def test_verify_password_checkpw_error_false():
    # Valid-looking format (60 chars, $2b$ prefix) but not a real hash: bcrypt
    # raises internally and the helper fails closed.
    bogus = "$2b$12$" + "x" * 53
    assert len(bogus) == 60
    assert auth.verify_password("pw", bogus) is False


# ---- session cookie -----------------------------------------------------


def test_session_cookie_roundtrip():
    cookie = auth.create_session_cookie("alice")
    assert auth.validate_session_cookie(cookie) == "alice"


def test_session_cookie_empty_none():
    assert auth.validate_session_cookie("") is None


def test_session_cookie_wrong_shape_none():
    assert auth.validate_session_cookie("only:two") is None


def test_session_cookie_tampered_signature_none():
    cookie = auth.create_session_cookie("bob")
    username, expiry, sig = cookie.rsplit(":", 2)
    flipped = "0" if sig[-1] != "0" else "1"
    tampered = f"{username}:{expiry}:{sig[:-1]}{flipped}"
    assert auth.validate_session_cookie(tampered) is None


def test_session_cookie_expired_none():
    expiry = str(int(time.time()) - 10)
    payload = f"carol:{expiry}"
    sig = hmac.new(auth._get_session_secret(), payload.encode(), hashlib.sha256).hexdigest()
    assert auth.validate_session_cookie(f"{payload}:{sig}") is None


def test_session_cookie_non_integer_expiry_none():
    payload = "dave:notanumber"
    sig = hmac.new(auth._get_session_secret(), payload.encode(), hashlib.sha256).hexdigest()
    assert auth.validate_session_cookie(f"{payload}:{sig}") is None


# ---- _is_api_request ----------------------------------------------------


def test_is_api_request_path_segment():
    assert auth._is_api_request(_request(path="/alerts/api/stats")) is True


def test_is_api_request_non_html_accept():
    req = _request(path="/data", headers={"accept": "application/json"})
    assert auth._is_api_request(req) is True


def test_is_api_request_html_accept_false():
    req = _request(path="/page", headers={"accept": "text/html"})
    assert auth._is_api_request(req) is False


def test_is_api_request_no_accept_false():
    assert auth._is_api_request(_request(path="/page")) is False


# ---- _check_basic_auth --------------------------------------------------


def test_check_basic_auth_missing_header_none():
    assert auth._check_basic_auth(_request()) is None


def test_check_basic_auth_bad_base64_none():
    req = _request(headers={"authorization": "Basic !!!notbase64!!!"})
    assert auth._check_basic_auth(req) is None


def test_check_basic_auth_no_colon_none():
    raw = base64.b64encode(b"nocolon").decode()
    req = _request(headers={"authorization": f"Basic {raw}"})
    assert auth._check_basic_auth(req) is None


def test_check_basic_auth_valid_default_allowed():
    # conftest sets GYANAM_ALLOW_DEFAULT_PASSWORD=1, so the shipped 'changeme'
    # default authenticates.
    req = _request(headers={"authorization": _basic_header("admin", "changeme")})
    assert auth._check_basic_auth(req) == "admin"


def test_check_basic_auth_wrong_password_none():
    req = _request(headers={"authorization": _basic_header("admin", "nope")})
    assert auth._check_basic_auth(req) is None


def test_check_basic_auth_default_blocked_none(monkeypatch):
    monkeypatch.delenv("GYANAM_ALLOW_DEFAULT_PASSWORD", raising=False)
    assert auth.default_password_blocks_login(auth._DEFAULT_HASH) is True
    req = _request(headers={"authorization": _basic_header("admin", "changeme")})
    assert auth._check_basic_auth(req) is None


# ---- get_current_user ---------------------------------------------------


async def test_get_current_user_valid_cookie():
    cookie = auth.create_session_cookie("admin")
    req = _request(cookies={auth.SESSION_COOKIE_NAME: cookie})
    assert await auth.get_current_user(req) == "admin"


async def test_get_current_user_api_basic_auth():
    req = _request(
        path="/x/api/y",
        headers={"authorization": _basic_header("admin", "changeme")},
    )
    assert await auth.get_current_user(req) == "admin"


async def test_get_current_user_api_no_creds_401():
    req = _request(path="/x/api/y")
    with pytest.raises(HTTPException) as ei:
        await auth.get_current_user(req)
    assert ei.value.status_code == 401
    assert ei.value.headers.get("WWW-Authenticate") == "Basic"


async def test_get_current_user_api_default_blocked_401(monkeypatch):
    monkeypatch.delenv("GYANAM_ALLOW_DEFAULT_PASSWORD", raising=False)
    req = _request(
        path="/x/api/y",
        headers={"authorization": _basic_header("admin", "changeme")},
    )
    with pytest.raises(HTTPException) as ei:
        await auth.get_current_user(req)
    assert ei.value.status_code == 401


async def test_get_current_user_browser_redirects():
    req = _request(path="/dashboard", headers={"accept": "text/html"})
    with pytest.raises(auth.LoginRequiredError) as ei:
        await auth.get_current_user(req)
    assert ei.value.next_url == "/dashboard"
