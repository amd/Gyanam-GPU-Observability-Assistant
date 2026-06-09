# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""CSRF protection for HTML form endpoints."""

import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, status

from ..config import get_settings

# Token validity period (1 hour)
_TOKEN_MAX_AGE = 3600


def _get_secret() -> bytes:
    """Get CSRF secret derived from the encryption key."""
    settings = get_settings()
    return hashlib.sha256(f"csrf-{settings.encryption_key}".encode()).digest()


def generate_csrf_token() -> str:
    """Generate a signed CSRF token.

    Returns:
        A signed token string containing a nonce and timestamp.
    """
    nonce = secrets.token_hex(16)
    timestamp = str(int(time.time()))
    payload = f"{nonce}:{timestamp}"
    signature = hmac.new(_get_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def validate_csrf_token(token: str | None) -> None:
    """Validate a CSRF token.

    Args:
        token: The token to validate.

    Raises:
        HTTPException: If the token is missing, malformed, or invalid.
    """
    if not token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing CSRF token")

    parts = token.split(":")
    if len(parts) != 3:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    nonce, timestamp_str, signature = parts

    # Verify signature
    payload = f"{nonce}:{timestamp_str}"
    expected = hmac.new(_get_secret(), payload.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    # Check expiry
    try:
        token_time = int(timestamp_str)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    if time.time() - token_time > _TOKEN_MAX_AGE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token expired")
