"""Grid DCA planner unit tests (Bybit-style % step + growing size)."""
from __future__ import annotations

from src.grid_dca import build_grid, reverse_engineer_grid
from src.config import RiskConfig, StrategyConfig
from src.filters import MarketSnapshot
from src.position import Position
from src.risk_manager import RiskManager
from src.strategy import ActionType, MeanReversionDCAStrategy
import pandas as pd


def test_reverse_engineer_from_screenshot_ladder():
    # Observed from screenshot: qtys 0.07→0.09→0.11→0.13→0.17; prices 2046→1966→1859
    stats = reverse_engineer_grid(
        prices=[2046.06, 1966.95, 1859.81],
        qtys=[0.07, 0.09, 0.11, 0.13, 0.17],
    )
    assert 1.2 <= stats["size_multiplier"] <= 1.32
    assert 0.03 <= stats["avg_step_pct"] <= 0.06


def test_build_grid_4pct_125x():
    plan = build_grid(2000.0, 0.07, step_pct=0.04, size_multiplier=1.25, num_adds=4)
    assert len(plan.levels) == 4
    assert abs(plan.levels[0].price - 2000 * 0.96) < 1e-9
    assert abs(plan.levels[0].qty - 0.07 * 1.25) < 1e-12
    assert abs(plan.levels[1].qty - 0.07 * 1.25**2) < 1e-12
    # Each level deeper
    for i in range(1, 4):
        assert plan.levels[i].price < plan.levels[i - 1].price
        assert plan.levels[i].qty > plan.levels[i - 1].qty


def test_strategy_grid_waits_then_fills():
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
    pos = Position(symbol="ETH/USDT:USDT", leverage=5)
    pos.add(2000.0, 0.07, 28.0)
    plan = strat._attach_grid(pos, 2000.0, 0.07)
    pos.meta["grid"]["entry_price"] = 2000.0
    pos.meta["grid"]["base_qty"] = 0.07
    pos.meta["base_qty"] = 0.07
    pos.meta["grid_entry"] = 2000.0

    atr = pd.Series([1.0] * 50)
    # Above first grid — wait
    snap = MarketSnapshot(
        "ETH/USDT:USDT",
        price=1950.0,
        rsi_1m=20,
        rsi_5m=20,
        rsi_15m=20,
        adx_1m=10,
        atr_1m=1.0,
        atr_series_1m=atr,
    )
    d1 = strat.decide(snap, pos, equity=1000.0)
    assert d1.action == ActionType.HOLD
    assert "wait_grid" in d1.reason

    # At/below first level — DCA
    snap2 = MarketSnapshot(
        "ETH/USDT:USDT",
        price=plan.levels[0].price,
        rsi_1m=20,
        rsi_5m=20,
        rsi_15m=20,
        adx_1m=10,
        atr_1m=1.0,
        atr_series_1m=atr,
    )
    d2 = strat.decide(snap2, pos, equity=1000.0)
    assert d2.action == ActionType.DCA
    assert d2.size is not None
    assert abs(d2.size.qty - plan.levels[0].qty) < 1e-12
