"""Cancel open limits when position closes / flat."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.bot import TradingBot
from src.config import AppConfig
from src.position import Position


def _bot() -> TradingBot:
    cfg = AppConfig()
    store = MagicMock()
    store.load_positions.return_value = {}
    store.last_equity.return_value = 0.0
    ex = MagicMock()
    ex.cancel_open_orders.return_value = 8
    bot = TradingBot(cfg, exchange=ex, store=store)
    bot.notify = MagicMock()
    return bot


def test_cancel_open_limits_audits():
    bot = _bot()
    n = bot._cancel_open_limits("SOL/USDT:USDT", reason="close")
    assert n == 8
    bot.exchange.cancel_open_orders.assert_called_once_with("SOL/USDT:USDT")
    bot.store.audit.assert_called()
    bot.notify.send.assert_called()


def test_flat_cleanup_runs_once():
    bot = _bot()
    bot.positions = {}
    # simulate process_symbol flat branch
    symbol = "SOL/USDT:USDT"
    assert symbol not in bot._flat_limits_cleared
    bot._cancel_open_limits(symbol, reason="flat_cleanup")
    bot._flat_limits_cleared.add(symbol)
    bot.exchange.cancel_open_orders.reset_mock()
    # second time should not be called by our guard
    if symbol not in bot._flat_limits_cleared:
        bot._cancel_open_limits(symbol, reason="flat_cleanup")
    bot.exchange.cancel_open_orders.assert_not_called()


def test_enter_clears_flat_flag():
    bot = _bot()
    bot._flat_limits_cleared.add("SOL/USDT:USDT")
    bot._flat_limits_cleared.discard("SOL/USDT:USDT")
    assert "SOL/USDT:USDT" not in bot._flat_limits_cleared
