# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for CSRF token generation/validation."""

import time

import pytest
from fastapi import HTTPException
from src.api import csrf


def test_generate_validate_roundtrip():
    token = csrf.generate_csrf_token()
    # valid token does not raise
    csrf.validate_csrf_token(token)


def test_missing_token_rejected():
    with pytest.raises(HTTPException) as e:
        csrf.validate_csrf_token(None)
    assert e.value.status_code == 403


def test_malformed_token_rejected():
    with pytest.raises(HTTPException):
        csrf.validate_csrf_token("not-a-valid-token")


def test_tampered_signature_rejected():
    token = csrf.generate_csrf_token()
    nonce, ts, _sig = token.split(":")
    with pytest.raises(HTTPException):
        csrf.validate_csrf_token(f"{nonce}:{ts}:deadbeef")


def test_expired_token_rejected(monkeypatch):
    # Forge a validly-signed but old token.
    import hashlib
    import hmac

    secret = csrf._get_secret()
    old_ts = str(int(time.time()) - csrf._TOKEN_MAX_AGE - 10)
    payload = f"abc123:{old_ts}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    with pytest.raises(HTTPException) as e:
        csrf.validate_csrf_token(f"{payload}:{sig}")
    assert "expired" in e.value.detail.lower()
