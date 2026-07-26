"""Remaining free USDT → isolated margin after enter."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.bot import TradingBot
from src.config import AppConfig, StrategyConfig
from src.position import Position


def _bot(*, free: float = 12.0) -> TradingBot:
    cfg = AppConfig()
    cfg.strategy = StrategyConfig(
        post_entry_add_all_margin=True,
        margin_reserve_usdt=1.0,
        topup_free_while_open=True,
    )
    store = MagicMock()
    store.load_positions.return_value = {}
    store.last_equity.return_value = 0.0
    ex = MagicMock()
    ex.fetch_free_usdt.return_value = free
    ex.add_margin.return_value = {"ok": True}
    bot = TradingBot(cfg, exchange=ex, store=store)
    bot.notify = MagicMock()
    return bot


def test_topup_adds_free_minus_reserve():
    bot = _bot(free=12.34)
    pos = Position(symbol="SOL/USDT:USDT", leverage=5)
    pos.add(74.0, 0.1, 5.0)
    bot.positions["SOL/USDT:USDT"] = pos
    added = bot._topup_remaining_margin("SOL/USDT:USDT", reason="post_enter")
    assert abs(added - 11.34) < 1e-9
    bot.exchange.add_margin.assert_called_once_with("SOL/USDT:USDT", 11.34)
    assert abs(pos.margin - (5.0 + 11.34)) < 1e-9


def test_topup_skips_when_below_min():
    bot = _bot(free=1.2)  # reserve 1 → only 0.2
    assert bot._topup_remaining_margin("SOL/USDT:USDT", reason="post_enter") == 0.0
    bot.exchange.add_margin.assert_not_called()


def test_topup_disabled_post_enter():
    bot = _bot(free=20.0)
    bot.cfg.strategy.post_entry_add_all_margin = False
    assert bot._topup_remaining_margin("SOL/USDT:USDT", reason="post_enter") == 0.0
    bot.exchange.add_margin.assert_not_called()
