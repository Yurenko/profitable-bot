"""Position sizing, leverage, margin survival, top-ups."""
from __future__ import annotations

from src.config import RiskConfig, StrategyConfig
from src.position import Position
from src.risk_manager import RiskManager


def test_size_respects_leverage_and_survival():
    rm = RiskManager(StrategyConfig(initial_equity_pct=0.01, dynamic_sizing=False), RiskConfig(min_adverse_move_pct=0.12))
    decision = rm.size_order(equity=10_000, price=50_000, atr_pct=0.005, leverage=5)
    # margin rate = max(1/5, 0.12) = 0.20  (5x initial margin already covers 12%)
    assert abs(decision.margin - 100.0) < 1e-6  # 1% of equity
    assert abs(decision.notional - 100.0 / 0.20) < 1e-6
    assert abs(decision.qty - decision.notional / 50_000) < 1e-12
    # Survives >=12% and matches 5x initial margin: loss at 20% ≈ margin
    assert decision.notional * 0.12 <= decision.margin + 1e-6
    assert abs(decision.notional / 5 - decision.margin) < 1e-6


def test_dynamic_sizing_shrinks_with_high_atr():
    rm = RiskManager(
        StrategyConfig(
            initial_equity_pct=0.01,
            dynamic_sizing=True,
            atr_sizing_ref_pct=0.005,
            min_equity_pct=0.005,
            max_equity_pct=0.015,
        ),
        RiskConfig(),
    )
    low = rm.equity_pct_for_atr(0.002)
    high = rm.equity_pct_for_atr(0.02)
    assert low > high
    assert high >= 0.005
    assert low <= 0.015


def test_liquidation_and_margin_topup():
    pos = Position(symbol="BTC/USDT:USDT", leverage=5)
    pos.add(price=100.0, qty=1.0, margin_added=20.0)  # 5x on 100 notional would be 20
    liq = pos.liquidation_price()
    assert liq < 100.0
    # Mark near liq
    mark = liq + 1.0
    rm = RiskManager(StrategyConfig(), RiskConfig(margin_topup_buffer_pct=0.05))
    need, amount = rm.needs_margin_topup(pos, mark)
    assert need
    assert amount > 0
    pos.add_margin(amount)
    need2, _ = rm.needs_margin_topup(pos, mark)
    assert not need2 or pos.distance_to_liq_pct(mark) > 0.05


def test_daily_loss_and_circuit_breaker():
    rm = RiskManager(StrategyConfig(), RiskConfig(max_daily_loss_pct=0.03, circuit_breaker_drawdown_pct=0.10))
    rm.update_equity(10_000)
    rm.update_equity(9_600)  # -4% day
    can, reason = rm.can_open_new()
    assert not can
    assert "daily_loss" in reason

    rm2 = RiskManager(StrategyConfig(), RiskConfig(max_daily_loss_pct=0.50, circuit_breaker_drawdown_pct=0.10))
    rm2.update_equity(10_000)
    rm2.update_equity(8_500)  # -15% from peak
    can2, reason2 = rm2.can_open_new()
    assert not can2
    assert "circuit_breaker" in reason2

    # After TP — resume even with large historical DD
    rm2.resume_after_take_profit(8_500)
    can3, _ = rm2.can_open_new()
    assert can3
    # Peak rebased — small further drop should not immediately re-halt at 10% of old peak
    rm2.update_equity(8_400)
    can4, _ = rm2.can_open_new()
    assert can4
