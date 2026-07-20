"""Shared fixtures and synthetic OHLCV helpers."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.config import AppConfig, RiskConfig, StrategyConfig


@pytest.fixture
def strategy_cfg() -> StrategyConfig:
    return StrategyConfig()


@pytest.fixture
def risk_cfg() -> RiskConfig:
    return RiskConfig()


@pytest.fixture
def app_cfg(strategy_cfg, risk_cfg) -> AppConfig:
    return AppConfig(strategy=strategy_cfg, risk=risk_cfg, symbols=["BTC/USDT:USDT"])


def make_ohlcv(
    n: int = 200,
    start_price: float = 100.0,
    drift: float = 0.0,
    vol: float = 0.001,
    seed: int = 0,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    close = start_price * np.exp(np.cumsum(rets))
    high = close * 1.001
    low = close * 0.999
    open_ = np.roll(close, 1)
    open_[0] = start_price
    idx = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": 1.0},
        index=idx,
    )


def force_oversold_tail(df: pd.DataFrame, drop: float = 0.08) -> pd.DataFrame:
    """Crash the last 20 bars to drive RSI into oversold."""
    out = df.copy()
    n = len(out)
    start = out["close"].iloc[-21]
    for i, frac in enumerate(np.linspace(0, drop, 20)):
        idx = n - 20 + i
        px = start * (1 - frac)
        out.iloc[idx, out.columns.get_loc("close")] = px
        out.iloc[idx, out.columns.get_loc("open")] = px
        out.iloc[idx, out.columns.get_loc("high")] = px * 1.0005
        out.iloc[idx, out.columns.get_loc("low")] = px * 0.9995
    return out
