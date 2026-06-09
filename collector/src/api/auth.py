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
"""Authentication middleware for the Config UI."""

import base64
import hashlib
import hmac
import logging
import secrets
import time

import bcrypt
from fastapi import HTTPException, Request, status

from ..config import get_config, get_settings

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "session"
SESSION_MAX_AGE = 28800  # 8 hours

# The hash of the default password 'changeme' — used only to detect
# whether the operator has changed the default and log a warning.
_DEFAULT_HASH = "$2b$12$DDVvJVK1RdIj//rkWa7g8Op8Sc00hu64FJ9lwMZ/.8hvlXkF7jLaW"


class LoginRequiredError(Exception):
    """Raised when a browser request needs authentication.

    Handled by a custom exception handler in app.py that returns
    a RedirectResponse to the login page.
    """

    def __init__(self, next_url: str = "/"):
        self.next_url = next_url


def _get_session_secret() -> bytes:
    """Get session signing secret derived from the encryption key."""
    settings = get_settings()
    return hashlib.sha256(f"session-{settings.encryption_key}".encode()).digest()


def is_valid_bcrypt_hash(hash_string: str) -> bool:
    """Check if a string is a valid bcrypt hash.

    Args:
        hash_string: String to validate

    Returns:
        True if it's a valid bcrypt hash format
    """
    if not hash_string:
        return False
    # Bcrypt hashes start with $2a$, $2b$, or $2y$ and are 60 characters
    if not hash_string.startswith(("$2a$", "$2b$", "$2y$")):
        return False
    return len(hash_string) == 60


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash.

    Args:
        plain_password: The plaintext password to verify
        hashed_password: The bcrypt hash to verify against

    Returns:
        True if the password matches
    """
    if not hashed_password:
        return False

    if not is_valid_bcrypt_hash(hashed_password):
        logger.warning("Invalid bcrypt hash format in configuration")
        return False

    try:
        return bool(bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8")))
    except Exception as e:
        logger.error(f"Password verification error: {e}")
        return False


def hash_password(password: str) -> str:
    """Hash a password using bcrypt.

    Args:
        password: The plaintext password to hash

    Returns:
        The bcrypt hash
    """
    return str(bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8"))


def create_session_cookie(username: str) -> str:
    """Create a signed session cookie value.

    Format: {username}:{expiry_timestamp}:{hmac_signature}

    Args:
        username: The authenticated username

    Returns:
        Signed cookie string
    """
    expiry = str(int(time.time()) + SESSION_MAX_AGE)
    payload = f"{username}:{expiry}"
    signature = hmac.new(_get_session_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def validate_session_cookie(cookie: str) -> str | None:
    """Validate a session cookie and return the username.

    Args:
        cookie: The cookie value to validate

    Returns:
        The username if valid, None otherwise
    """
    if not cookie:
        return None

    # rsplit from right: HMAC (hex) and expiry (digits) never contain colons,
    # so this correctly handles usernames that might contain colons.
    parts = cookie.rsplit(":", 2)
    if len(parts) != 3:
        return None

    username, expiry_str, signature = parts

    # Verify signature
    payload = f"{username}:{expiry_str}"
    expected = hmac.new(_get_session_secret(), payload.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(signature, expected):
        return None

    # Check expiry
    try:
        expiry = int(expiry_str)
    except ValueError:
        return None

    if time.time() > expiry:
        return None

    return username


def _check_basic_auth(request: Request) -> str | None:
    """Check for HTTP Basic Auth credentials in the request.

    Args:
        request: The incoming request

    Returns:
        The username if valid Basic Auth credentials are present, None otherwise
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Basic "):
        return None

    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
    except Exception:
        return None

    if ":" not in decoded:
        return None

    username, password = decoded.split(":", 1)
    config = get_config()

    correct_username = secrets.compare_digest(username, config.ui.auth.username)

    password_hash = config.ui.auth.password_hash
    if password_hash == _DEFAULT_HASH:
        logger.warning(
            "SECURITY WARNING: Using default password 'changeme'. "
            "Please set a secure password_hash in config.yaml!"
        )

    correct_password = verify_password(password, password_hash)

    if correct_username and correct_password:
        return username
    return None


def _is_api_request(request: Request) -> bool:
    """Determine if the request is an API request (not browser HTML).

    API requests get 401 responses; browser requests get redirected to login.
    """
    # Path-based: any route segment containing /api
    if "/api" in request.url.path:
        return True
    # Accept header: if client doesn't accept HTML
    accept = request.headers.get("accept", "")
    return bool(accept and "text/html" not in accept)


async def get_current_user(request: Request) -> str:
    """Validate session cookie or Basic Auth and return the username.

    For browser requests: only session cookies are accepted;
      redirects to /login if missing/invalid.
    For API requests: accepts both session cookies and Basic Auth;
      returns 401 with Basic Auth challenge if neither is valid.

    Args:
        request: The incoming request

    Returns:
        The authenticated username

    Raises:
        HTTPException: If API request with invalid/missing credentials
        LoginRequiredError: If browser request without valid session
    """
    # Check session cookie first (works for both browser and API)
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    username = validate_session_cookie(cookie)
    if username:
        return username

    is_api = _is_api_request(request)

    # Basic Auth only for API/programmatic requests — browsers cache
    # Basic Auth credentials indefinitely, which would bypass the login form.
    if is_api:
        username = _check_basic_auth(request)
        if username:
            return username

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    # Browser request without valid session → redirect to login
    raise LoginRequiredError(next_url=request.url.path)
