"""Tests for dashboard password / session cookies."""
from __future__ import annotations

import os

import pytest

from dashboard.auth import (
    check_password,
    make_session_token,
    password_configured,
    verify_session_token,
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-secret-pass")
    monkeypatch.delenv("DASHBOARD_SECRET", raising=False)


def test_password_configured():
    assert password_configured() is True


def test_check_password_ok():
    assert check_password("test-secret-pass") is True


def test_check_password_wrong():
    assert check_password("wrong") is False


def test_session_token_roundtrip():
    token = make_session_token()
    assert verify_session_token(token) is True


def test_session_token_tampered():
    token = make_session_token()
    bad = token[:-4] + "dead"
    assert verify_session_token(bad) is False


def test_session_token_expired(monkeypatch):
    import dashboard.auth as auth

    monkeypatch.setattr(auth, "SESSION_TTL_SEC", -10)
    token = make_session_token()
    assert verify_session_token(token) is False


def test_no_password_disables_auth(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "")
    assert password_configured() is False
    assert check_password("anything") is True
