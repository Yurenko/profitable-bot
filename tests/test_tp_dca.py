"""TP, DCA, average entry price."""
from __future__ import annotations

from src.config import RiskConfig, StrategyConfig
from src.filters import MarketSnapshot
from src.position import Position
from src.risk_manager import RiskManager
from src.strategy import ActionType, MeanReversionDCAStrategy
import pandas as pd


def _snap(**kwargs):
    defaults = dict(
        symbol="BTC/USDT:USDT",
        price=100.0,
        rsi_1m=25.0,
        rsi_5m=30.0,
        rsi_15m=28.0,
        adx_1m=20.0,
        atr_1m=0.5,
        atr_series_1m=pd.Series([0.5] * 100),
        funding_rate=0.0,
    )
    defaults.update(kwargs)
    return MarketSnapshot(**defaults)


def test_average_entry_after_dca():
    pos = Position(symbol="BTC/USDT:USDT", leverage=5)
    pos.add(100.0, qty=1.0, margin_added=20.0)
    pos.add(90.0, qty=1.0, margin_added=18.0, is_dca=True)
    assert pos.dca_level == 2
    assert abs(pos.avg_entry - 95.0) < 1e-9
    assert abs(pos.qty - 2.0) < 1e-12


def test_tp_min_and_strong():
    rm = RiskManager(StrategyConfig(), RiskConfig())
    strat = MeanReversionDCAStrategy(StrategyConfig(tp_min_pct=0.01, tp_strong_pct=0.02, rsi_5m_strong_oversold=25), rm)
    assert strat.tp_target_pct(30) == 0.01
    assert strat.tp_target_pct(20) == 0.02

    pos = Position(symbol="BTC/USDT:USDT", leverage=5)
    pos.add(100.0, 1.0, 20.0)
    # +1.2% → full TP (partial disabled for clarity)
    strat.cfg.partial_close_enabled = False
    strat.cfg.trailing_tp_enabled = False
    decision = strat.decide(_snap(price=101.2, rsi_5m=30), pos, equity=10_000)
    assert decision.action == ActionType.FULL_TP


def test_dca_on_15m_oversold_when_underwater():
    cfg = StrategyConfig(
        max_dca_levels=4,
        partial_close_enabled=False,
        trailing_tp_enabled=False,
        dynamic_sizing=False,
        dca_mode="rsi",
    )
    rm = RiskManager(cfg, RiskConfig(min_adverse_move_pct=0.12))
    strat = MeanReversionDCAStrategy(cfg, rm)
    pos = Position(symbol="BTC/USDT:USDT", leverage=5)
    pos.add(100.0, 0.01, 20.0)
    decision = strat.decide(_snap(price=95.0, rsi_15m=25.0), pos, equity=10_000)
    assert decision.action == ActionType.DCA
    assert decision.size is not None

    # RSI not oversold → no DCA
    decision2 = strat.decide(_snap(price=95.0, rsi_15m=40.0), pos, equity=10_000)
    assert decision2.action == ActionType.HOLD


def test_max_dca_levels():
    cfg = StrategyConfig(
        max_dca_levels=2,
        trailing_tp_enabled=False,
        partial_close_enabled=False,
        dca_mode="rsi",
    )
    rm = RiskManager(cfg, RiskConfig())
    strat = MeanReversionDCAStrategy(cfg, rm)
    pos = Position(symbol="BTC/USDT:USDT", leverage=5)
    pos.add(100.0, 1.0, 20.0)
    pos.add(95.0, 1.0, 19.0, is_dca=True)
    assert pos.dca_level == 2
    decision = strat.decide(_snap(price=90.0, rsi_15m=20.0), pos, equity=10_000)
    assert decision.action == ActionType.HOLD


def test_long_term_never_sells_at_loss():
    """No stop-loss: deep underwater position stays open."""
    cfg = StrategyConfig(
        long_term_mode=True,
        auto_take_profit=True,
        trailing_tp_enabled=True,
        partial_close_enabled=True,
    )
    strat = MeanReversionDCAStrategy(cfg, RiskManager(cfg, RiskConfig()))
    pos = Position(symbol="SOL/USDT:USDT", leverage=5)
    pos.add(100.0, 1.0, 20.0)
    decision = strat.decide(_snap(price=70.0, rsi_15m=50.0), pos, equity=100.0)
    assert decision.action != ActionType.FULL_TP
    assert decision.action != ActionType.TRAIL_EXIT
    assert decision.action != ActionType.PARTIAL_TP


def test_long_term_disables_trailing_exit():
    """Trailing pullback exit is off in long_term_mode."""
    cfg = StrategyConfig(long_term_mode=True, auto_take_profit=False, trailing_tp_enabled=True)
    strat = MeanReversionDCAStrategy(cfg, RiskManager(cfg, RiskConfig()))
    pos = Position(symbol="SOL/USDT:USDT", leverage=5)
    pos.add(100.0, 1.0, 20.0)
    pos.peak_price = 101.5
    pos.trailing_active = True
    decision = strat.decide(_snap(price=100.8), pos, equity=100.0)
    assert decision.action != ActionType.TRAIL_EXIT
