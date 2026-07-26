"""Telegram notifier unit tests (no network)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.notify.telegram import TelegramNotifier


def test_disabled_without_credentials():
    n = TelegramNotifier(token="", chat_id="")
    assert n.enabled is False
    assert n.send("hi") is False


def test_enabled_with_credentials():
    n = TelegramNotifier(token="123:ABC", chat_id="999")
    assert n.enabled is True


def test_send_posts_json():
    n = TelegramNotifier(token="123:ABC", chat_id="42")
    fake_resp = MagicMock()
    fake_resp.read.return_value = b'{"ok":true,"result":{}}'
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False
    with patch("urllib.request.urlopen", return_value=fake_resp) as urlopen:
        assert n.send("test message") is True
        assert urlopen.called
        req = urlopen.call_args[0][0]
        assert "123:ABC" in req.full_url
        assert req.data is not None


def test_notify_enter_builds_message():
    n = TelegramNotifier(token="t", chat_id="1")
    with patch.object(n, "send", return_value=True) as send:
        n.notify_enter(
            symbol="SOL/USDT:USDT",
            price=74.5,
            qty=1.2,
            avg_entry=74.5,
            dca_level=1,
            mode="live",
            margin=10.0,
        )
        text = send.call_args[0][0]
        assert "Відкрито" in text
        assert "SOL/USDT:USDT" in text
        assert "live" in text


def test_notify_dca_and_close():
    n = TelegramNotifier(token="t", chat_id="1")
    with patch.object(n, "send", return_value=True) as send:
        n.notify_dca(
            symbol="NEAR/USDT:USDT",
            price=5.0,
            qty=2.0,
            avg_entry=5.1,
            dca_level=2,
            mode="paper",
        )
        assert "Усереднення" in send.call_args[0][0]
        n.notify_close(
            symbol="NEAR/USDT:USDT",
            action="full_tp",
            price=5.5,
            qty=2.0,
            avg_entry=5.1,
            pnl=0.8,
            mode="paper",
        )
        assert "Закрито" in send.call_args[0][0]
        assert "PnL" in send.call_args[0][0]
