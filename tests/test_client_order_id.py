"""clientOrderId must be < 36 chars for Binance USDM."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.bot import TradingBot
from src.config import AppConfig


def _bot() -> TradingBot:
    cfg = AppConfig()
    store = MagicMock()
    store.load_positions.return_value = {}
    store.last_equity.return_value = 0.0
    return TradingBot(cfg, exchange=MagicMock(), store=store)


def test_client_order_id_under_36():
    bot = _bot()
    for symbol in ("SOL/USDT:USDT", "NEAR/USDT:USDT", "LINK/USDT:USDT", "BTC/USDT:USDT"):
        for action in ("enter", "dca", "full_tp", "partial_tp", "grid1", "grid11", "margin_topup"):
            coid = bot._client_order_id(symbol, action)
            assert len(coid) < 36, f"{coid!r} len={len(coid)}"
            assert coid  # non-empty
