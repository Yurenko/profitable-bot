"""Dashboard password auth — password from .env, signed cookie for 1 day."""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Optional

COOKIE_NAME = "mrdca_session"
SESSION_TTL_SEC = 86400  # 1 day


def password_configured() -> bool:
    return bool(os.environ.get("DASHBOARD_PASSWORD", "").strip())


def auth_required() -> bool:
    """If DASHBOARD_PASSWORD is set — protect the dashboard."""
    return password_configured()


def _signing_key() -> bytes:
    secret = (
        os.environ.get("DASHBOARD_SECRET", "").strip()
        or os.environ.get("DASHBOARD_PASSWORD", "").strip()
        or "local-dev-insecure"
    )
    return hashlib.sha256(f"mrdca-dashboard-v1:{secret}".encode()).digest()


def check_password(provided: str) -> bool:
    expected = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if not expected:
        return True
    # Hash both sides so compare_digest always gets equal-length digests
    a = hashlib.sha256(provided.encode("utf-8")).digest()
    b = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(a, b)


def make_session_token() -> str:
    exp = int(time.time()) + SESSION_TTL_SEC
    payload = str(exp)
    sig = hmac.new(_signing_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session_token(token: Optional[str]) -> bool:
    if not token or "." not in token:
        return False
    try:
        payload, sig = token.rsplit(".", 1)
        exp = int(payload)
        if exp < int(time.time()):
            return False
        expected = hmac.new(_signing_key(), payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)
    except (ValueError, TypeError):
        return False


def is_authenticated(cookie_value: Optional[str]) -> bool:
    if not auth_required():
        return True
    return verify_session_token(cookie_value)
