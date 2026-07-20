"""Filters: funding/OI, news, correlation."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from src.config import NewsConfig, StrategyConfig
from src.filters import (
    MarketSnapshot,
    check_funding_oi,
    check_news_blackout,
    would_breach_correlation,
)
from src.news.calendar import EconomicCalendar


def test_funding_oi_overheat():
    cfg = StrategyConfig(funding_rate_max=0.0005, oi_rise_threshold_pct=0.03, oi_change_lookback=5)
    oi = pd.Series([100, 101, 102, 103, 104, 110.0])  # >3% rise
    snap = MarketSnapshot(
        symbol="BTC/USDT:USDT",
        price=1.0,
        rsi_1m=20,
        rsi_5m=20,
        rsi_15m=20,
        adx_1m=10,
        atr_1m=0.1,
        atr_series_1m=pd.Series([0.1] * 50),
        funding_rate=0.0006,
        oi_series=oi,
    )
    assert not check_funding_oi(snap, cfg).allowed

    snap2 = MarketSnapshot(**{**snap.__dict__, "funding_rate": 0.0001})
    assert check_funding_oi(snap2, cfg).allowed


def test_news_blackout_window():
    cfg = NewsConfig(enabled=True, blacklist_minutes_before=15, blacklist_minutes_after=15)
    event_time = datetime(2024, 6, 12, 18, 0, tzinfo=timezone.utc)
    events = [{"title": "FOMC", "impact": "high", "time": event_time}]
    # 10 min before
    now = datetime(2024, 6, 12, 17, 50, tzinfo=timezone.utc)
    assert not check_news_blackout(now, events, cfg).allowed
    # 30 min before — ok
    now2 = datetime(2024, 6, 12, 17, 30, tzinfo=timezone.utc)
    assert check_news_blackout(now2, events, cfg).allowed


def test_calendar_wrapper(tmp_path):
    cfg = NewsConfig(cache_path=str(tmp_path / "cal.json"), blacklist_minutes_before=15, blacklist_minutes_after=15)
    cal = EconomicCalendar(cfg)
    when = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
    cal.add_synthetic_high_impact(when, "Test")
    blocked, reason = cal.is_blackout(when)
    assert blocked
    assert "news_blackout" in reason


def test_correlation_filter():
    idx = pd.date_range("2024-01-01", periods=120, freq="1min", tz="UTC")
    a = pd.Series(range(120), index=idx, dtype=float)
    b = a * 1.0 + 0.01  # nearly perfect corr
    c = pd.Series(pd.Series(range(120, 0, -1), dtype=float).values, index=idx)
    returns = pd.DataFrame({"A": a, "B": b, "C": c}).pct_change().dropna()
    # Open A and B already; candidate C vs max_correlated=1 with high threshold on A/B
    # Candidate A with open [B] — corr high
    fr = would_breach_correlation("A", ["B"], returns, threshold=0.9, max_correlated=1)
    assert not fr.allowed


def test_max_open_positions_blocks_second_coin():
    from src.filters import check_max_open_positions

    # Already have SOL open — cannot enter NEAR when max=1
    fr = check_max_open_positions(["SOL/USDT:USDT"], max_open_positions=1)
    assert not fr.allowed
    assert "max_open_positions" in fr.reasons[0]

    # No other opens — entry allowed
    assert check_max_open_positions([], max_open_positions=1).allowed

    # Two opens already, max=2 — block third
    fr2 = check_max_open_positions(["A", "B"], max_open_positions=2)
    assert not fr2.allowed
    # One open, max=2 — still ok
    assert check_max_open_positions(["A"], max_open_positions=2).allowed
