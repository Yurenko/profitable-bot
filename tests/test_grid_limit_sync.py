"""Live grid limit fills must sync — never market-rebuy the same level."""
from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd

from src.bot import TradingBot
from src.config import AppConfig, RiskConfig, StrategyConfig
from src.filters import MarketSnapshot
from src.position import Position
from src.risk_manager import RiskManager
from src.strategy import ActionType, MeanReversionDCAStrategy


def _snap(price: float) -> MarketSnapshot:
    atr = pd.Series([1.0] * 50)
    return MarketSnapshot(
        "SOL/USDT:USDT",
        price=price,
        rsi_1m=20,
        rsi_5m=20,
        rsi_15m=20,
        adx_1m=10,
        atr_1m=1.0,
        atr_series_1m=atr,
    )


def test_strategy_limits_live_never_market_dca():
    cfg = StrategyConfig(
        dca_mode="grid",
        grid_step_pct=0.04,
        grid_size_multiplier=1.25,
        max_dca_levels=5,
        long_term_mode=True,
        auto_take_profit=False,
        trailing_tp_enabled=False,
    )
    strat = MeanReversionDCAStrategy(cfg, RiskManager(cfg, RiskConfig()))
    pos = Position(symbol="SOL/USDT:USDT", leverage=5)
    pos.add(76.30, 0.19, 2.9)
    plan = strat._attach_grid(pos, 76.30, 0.19)
    pos.meta["grid"]["limits_live"] = True
    pos.meta["base_qty"] = 0.19
    pos.meta["grid_entry"] = 76.30

    # Price well below L1 — still HOLD (sync handles fills)
    d = strat.decide(_snap(plan.levels[0].price - 1.0), pos, equity=100.0)
    assert d.action == ActionType.HOLD
    assert "wait_grid" in d.reason


def test_sync_applies_filled_limit_and_records_trade():
    cfg = AppConfig()
    cfg.mode = "live"
    cfg.strategy = StrategyConfig(
        dca_mode="grid",
        grid_step_pct=0.04,
        grid_size_multiplier=1.25,
        max_dca_levels=10,
        leverage=5,
    )
    store = MagicMock()
    store.load_positions.return_value = {}
    store.last_equity.return_value = 0.0
    store.register_order.return_value = True

    ex = MagicMock()
    # L1 filled on exchange; L2 still open
    l1_price = 76.30 * 0.96
    l1_qty = 0.19 * 1.25
    l2_price = 76.30 * (0.96**2)
    l2_qty = 0.19 * (1.25**2)
    ex.fetch_open_orders.return_value = [
        {
            "id": "ord2",
            "side": "buy",
            "type": "limit",
            "status": "open",
            "price": l2_price,
            "amount": l2_qty,
        }
    ]

    bot = TradingBot(cfg, exchange=ex, store=store)
    bot.notify = MagicMock()
    pos = Position(symbol="SOL/USDT:USDT", leverage=5)
    pos.add(76.30, 0.19, 2.9)
    plan = MeanReversionDCAStrategy(cfg.strategy, RiskManager(cfg.strategy, RiskConfig()))._attach_grid(
        pos, 76.30, 0.19
    )
    # Ensure levels meta prices match
    assert abs(plan.levels[0].price - l1_price) < 1e-9
    pos.meta["grid"]["limits_live"] = True
    bot.positions["SOL/USDT:USDT"] = pos

    n = bot._sync_grid_limit_fills("SOL/USDT:USDT")
    assert n == 1
    assert pos.dca_level == 2
    assert abs(pos.qty - (0.19 + l1_qty)) < 1e-9
    assert pos.meta["grid"]["levels"][0]["filled"] is True
    store.record_trade.assert_called()
    kwargs = store.record_trade.call_args.kwargs
    assert kwargs["action"] == "dca"
    assert kwargs["side"] == "buy"
    bot.notify.notify_dca.assert_called_once()


def test_sync_waits_while_limit_still_open():
    cfg = AppConfig()
    cfg.mode = "live"
    cfg.strategy = StrategyConfig(dca_mode="grid", grid_step_pct=0.04, max_dca_levels=10)
    store = MagicMock()
    store.load_positions.return_value = {}
    store.last_equity.return_value = 0.0
    ex = MagicMock()
    bot = TradingBot(cfg, exchange=ex, store=store)
    bot.notify = MagicMock()

    pos = Position(symbol="SOL/USDT:USDT", leverage=5)
    pos.add(76.30, 0.19, 2.9)
    MeanReversionDCAStrategy(cfg.strategy, RiskManager(cfg.strategy, RiskConfig()))._attach_grid(
        pos, 76.30, 0.19
    )
    pos.meta["grid"]["limits_live"] = True
    lv = pos.meta["grid"]["levels"][0]
    ex.fetch_open_orders.return_value = [
        {
            "id": "ord1",
            "side": "buy",
            "type": "limit",
            "status": "open",
            "price": lv["price"],
            "amount": lv["qty"],
        }
    ]
    bot.positions["SOL/USDT:USDT"] = pos

    assert bot._sync_grid_limit_fills("SOL/USDT:USDT") == 0
    assert pos.dca_level == 1
    store.record_trade.assert_not_called()


def test_sync_recovers_without_limits_live_flag():
    """Existing open position: resting grid orders imply limits_live + apply missing fill."""
    cfg = AppConfig()
    cfg.mode = "live"
    cfg.strategy = StrategyConfig(
        dca_mode="grid",
        grid_step_pct=0.04,
        grid_size_multiplier=1.25,
        max_dca_levels=10,
        leverage=5,
    )
    store = MagicMock()
    store.load_positions.return_value = {}
    store.last_equity.return_value = 0.0
    store.register_order.return_value = True
    ex = MagicMock()
    bot = TradingBot(cfg, exchange=ex, store=store)
    bot.notify = MagicMock()

    pos = Position(symbol="SOL/USDT:USDT", leverage=5)
    pos.add(76.30, 0.19, 2.9)
    MeanReversionDCAStrategy(cfg.strategy, RiskManager(cfg.strategy, RiskConfig()))._attach_grid(
        pos, 76.30, 0.19
    )
    # Flag missing (old entry) — but L2+ still open on exchange
    assert not pos.meta["grid"].get("limits_live")
    lv2 = pos.meta["grid"]["levels"][1]
    ex.fetch_open_orders.return_value = [
        {
            "id": "ord2",
            "side": "buy",
            "type": "limit",
            "status": "open",
            "price": lv2["price"],
            "amount": lv2["qty"],
        }
    ]
    bot.positions["SOL/USDT:USDT"] = pos

    n = bot._sync_grid_limit_fills("SOL/USDT:USDT")
    assert n == 1
    assert pos.meta["grid"]["limits_live"] is True
    assert pos.dca_level == 2
    store.record_trade.assert_called()
