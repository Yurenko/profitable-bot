"""Backtest smoke + metrics sanity on synthetic regimes."""
from __future__ import annotations

from backtest.backtest import Backtester, synthesize_ohlcv
from backtest.metrics import compute_metrics
from backtest.monte_carlo import run_monte_carlo
from src.config import AppConfig, RiskConfig, StrategyConfig
import pandas as pd


def test_backtest_runs_and_returns_metrics():
    df = synthesize_ohlcv(n=8_000, seed=99)
    cfg = AppConfig(
        strategy=StrategyConfig(
            trailing_tp_enabled=True,
            partial_close_enabled=True,
            dynamic_sizing=True,
        ),
        risk=RiskConfig(),
    )
    cfg.news.enabled = True
    result = Backtester(cfg).run(df, initial_capital=10_000)
    assert result.metrics is not None
    assert result.metrics.n_trades >= 0
    assert len(result.equity_curve) > 100
    # Fees accounted
    assert result.fees_paid >= 0


def test_metrics_and_monte_carlo():
    eq = pd.Series([10_000, 10_100, 9_900, 10_200, 10_150])
    m = compute_metrics(eq, [100, -200, 300, -50], 10_000, fees=5, funding=2)
    assert m.n_trades == 4
    assert m.max_drawdown_pct >= 0
    mc = run_monte_carlo(eq, 10_000, n_simulations=50, block_size=2, seed=1)
    assert mc.n == 50
    assert mc.final_equity_p5 <= mc.final_equity_p95
