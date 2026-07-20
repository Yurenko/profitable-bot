"""Unit tests: multi-TF oversold entry detection."""
from __future__ import annotations

import pandas as pd

from src.config import StrategyConfig
from src.filters import MarketSnapshot, check_entry_indicators
from src.indicators import rsi
from tests.conftest import force_oversold_tail, make_ohlcv


def test_rsi_detects_oversold():
    df = force_oversold_tail(make_ohlcv(300, vol=0.0005, seed=1), drop=0.12)
    r = rsi(df["close"], 14)
    assert r.iloc[-1] < 30


def test_entry_requires_all_indicator_filters():
    cfg = StrategyConfig(adx_max=40, atr_percentile_max=95)
    # Flat ATR history so current ATR sits mid-percentile (crash would spike ATR)
    atr_hist = pd.Series([1.0] * 100)

    snap_ok = MarketSnapshot(
        symbol="BTC/USDT:USDT",
        price=90.0,
        rsi_1m=25.0,
        rsi_5m=30.0,
        rsi_15m=40.0,
        adx_1m=20.0,
        atr_1m=1.0,
        atr_series_1m=atr_hist,
    )
    assert check_entry_indicators(snap_ok, cfg).allowed

    snap_rsi_fail = MarketSnapshot(**{**snap_ok.__dict__, "rsi_1m": 40.0})
    assert not check_entry_indicators(snap_rsi_fail, cfg).allowed

    snap_adx_fail = MarketSnapshot(**{**snap_ok.__dict__, "adx_1m": 50.0})
    assert not check_entry_indicators(snap_adx_fail, cfg).allowed


def test_atr_percentile_filter_blocks_spikes():
    cfg = StrategyConfig(atr_percentile_max=90)
    series = pd.Series([1.0] * 95 + [10.0] * 5)
    snap = MarketSnapshot(
        symbol="X",
        price=100.0,
        rsi_1m=20.0,
        rsi_5m=20.0,
        rsi_15m=20.0,
        adx_1m=10.0,
        atr_1m=10.0,
        atr_series_1m=series,
    )
    res = check_entry_indicators(snap, cfg)
    assert not res.allowed
    assert any("atr_pctile" in r for r in res.reasons)
