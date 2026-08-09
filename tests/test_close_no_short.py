"""Close must never oversell into a short; residual shorts get flattened."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.bot import TradingBot
from src.config import AppConfig, StrategyConfig
from src.filters import MarketSnapshot
from src.position import Position
from src.strategy import ActionType, StrategyAction
import pandas as pd


def _bot() -> TradingBot:
    cfg = AppConfig()
    cfg.mode = "live"
    cfg.strategy = StrategyConfig(dca_mode="grid", leverage=5)
    store = MagicMock()
    store.load_positions.return_value = {}
    store.last_equity.return_value = 100.0
    store.register_order.return_value = True
    ex = MagicMock()
    ex.amount_to_precision.side_effect = lambda _s, a: float(a)
    bot = TradingBot(cfg, exchange=ex, store=store)
    bot.notify = MagicMock()
    return bot


def _snap(price: float = 76.87) -> MarketSnapshot:
    atr = pd.Series([1.0] * 20)
    return MarketSnapshot(
        "SOL/USDT:USDT",
        price=price,
        rsi_1m=55,
        rsi_5m=55,
        rsi_15m=55,
        adx_1m=10,
        atr_1m=1.0,
        atr_series_1m=atr,
    )


def test_close_qty_capped_to_exchange_long():
    bot = _bot()
    bot.exchange.fetch_position_qty.return_value = 0.436319
    pos = Position(symbol="SOL/USDT:USDT", leverage=5)
    pos.add(74.59, 0.44, 10.0)  # inflated local qty
    bot.positions["SOL/USDT:USDT"] = pos
    assert abs(bot._close_qty_for_sell("SOL/USDT:USDT", 0.44) - 0.436319) < 1e-9


def test_close_qty_refuses_when_exchange_already_short():
    bot = _bot()
    bot.exchange.fetch_position_qty.return_value = -0.01
    assert bot._close_qty_for_sell("SOL/USDT:USDT", 0.44) == 0.0


def test_full_tp_uses_reduce_only_and_capped_qty():
    bot = _bot()
    bot.exchange.fetch_position_qty.return_value = 0.436319
    bot.exchange.create_market_order.return_value = {
        "price": 76.87,
        "amount": 0.436319,
        "fee": 0.01,
    }
    pos = Position(symbol="SOL/USDT:USDT", leverage=5)
    pos.add(74.59, 0.44, 10.0)
    bot.positions["SOL/USDT:USDT"] = pos

    decision = StrategyAction(ActionType.FULL_TP, reason="tp", close_qty=0.44)
    # After sell, exchange flat — flatten no-op
    bot.exchange.fetch_position_qty.side_effect = [0.436319, 0.0]

    bot._execute("SOL/USDT:USDT", ActionType.FULL_TP, decision, _snap())

    bot.exchange.create_market_order.assert_called()
    args, kwargs = bot.exchange.create_market_order.call_args
    assert args[1] == "sell"
    assert abs(args[2] - 0.436319) < 1e-9
    assert kwargs["params"]["reduceOnly"] is True
    assert "SOL/USDT:USDT" not in bot.positions


def test_flatten_closes_accidental_short():
    bot = _bot()
    bot.exchange.fetch_position_qty.return_value = -0.01
    bot.exchange.create_market_order.return_value = {
        "price": 76.77,
        "amount": 0.01,
        "fee": 0.0,
    }
    ok = bot._flatten_exchange_residual("SOL/USDT:USDT", reason="local_flat")
    assert ok is True
    args, kwargs = bot.exchange.create_market_order.call_args
    assert args[1] == "buy"
    assert abs(args[2] - 0.01) < 1e-12
    assert kwargs["params"]["reduceOnly"] is True
    bot.store.audit.assert_any_call(
        "exchange_flatten",
        symbol="SOL/USDT:USDT",
        reason="local_flat",
        side="buy",
        qty=0.01,
        price=76.77,
        exchange_qty_before=-0.01,
    )
