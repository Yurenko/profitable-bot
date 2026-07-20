"""Entry / DCA / funding / news / correlation filters."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

import numpy as np
import pandas as pd

from src.config import NewsConfig, StrategyConfig
from src.indicators import atr_percentile


@dataclass
class MarketSnapshot:
    """Point-in-time multi-TF + funding/OI view for one symbol."""

    symbol: str
    price: float
    rsi_1m: float
    rsi_5m: float
    rsi_15m: float
    adx_1m: float
    atr_1m: float
    atr_series_1m: pd.Series
    funding_rate: float = 0.0
    open_interest: float = 0.0
    oi_series: pd.Series | None = None
    timestamp: datetime | None = None


@dataclass
class FilterResult:
    allowed: bool
    reasons: list[str]

    @classmethod
    def ok(cls) -> "FilterResult":
        return cls(True, [])

    @classmethod
    def reject(cls, *reasons: str) -> "FilterResult":
        return cls(False, list(reasons))


def check_entry_indicators(snap: MarketSnapshot, cfg: StrategyConfig) -> FilterResult:
    reasons: list[str] = []
    if snap.rsi_1m >= cfg.rsi_1m_entry:
        reasons.append(f"rsi_1m={snap.rsi_1m:.2f}>={cfg.rsi_1m_entry}")
    if snap.rsi_5m >= cfg.rsi_5m_entry:
        reasons.append(f"rsi_5m={snap.rsi_5m:.2f}>={cfg.rsi_5m_entry}")
    if snap.adx_1m >= cfg.adx_max:
        reasons.append(f"adx={snap.adx_1m:.2f}>={cfg.adx_max}")
    pct = atr_percentile(snap.atr_series_1m, cfg.atr_lookback, snap.atr_1m)
    if pct >= cfg.atr_percentile_max:
        reasons.append(f"atr_pctile={pct:.1f}>={cfg.atr_percentile_max}")
    if reasons:
        return FilterResult.reject(*reasons)
    return FilterResult.ok()


def check_funding_oi(snap: MarketSnapshot, cfg: StrategyConfig) -> FilterResult:
    """Avoid entries when funding is very positive AND OI is rising (long heat)."""
    if snap.funding_rate > cfg.funding_rate_max:
        oi_rising = False
        if snap.oi_series is not None and len(snap.oi_series) >= cfg.oi_change_lookback + 1:
            past = float(snap.oi_series.iloc[-(cfg.oi_change_lookback + 1)])
            now = float(snap.oi_series.iloc[-1])
            if past > 0 and (now - past) / past >= cfg.oi_rise_threshold_pct:
                oi_rising = True
        if oi_rising or snap.funding_rate > cfg.funding_rate_max * 2:
            return FilterResult.reject(
                f"overheated funding={snap.funding_rate:.6f} oi_rising={oi_rising}"
            )
    return FilterResult.ok()


def check_dca_rsi(snap: MarketSnapshot, cfg: StrategyConfig) -> FilterResult:
    if snap.rsi_15m >= cfg.rsi_15m_dca:
        return FilterResult.reject(f"rsi_15m={snap.rsi_15m:.2f}>={cfg.rsi_15m_dca}")
    return FilterResult.ok()


def check_news_blackout(
    now: datetime,
    events: Sequence[dict],
    cfg: NewsConfig,
) -> FilterResult:
    if not cfg.enabled:
        return FilterResult.ok()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    impact_ok = {x.lower() for x in cfg.impact_levels}
    before = cfg.blacklist_minutes_before
    after = cfg.blacklist_minutes_after
    for ev in events:
        impact = str(ev.get("impact", "")).lower()
        if impact not in impact_ok:
            continue
        ts = ev.get("time")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if not isinstance(ts, datetime):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta_min = (now - ts).total_seconds() / 60.0
        if -before <= delta_min <= after:
            return FilterResult.reject(
                f"news_blackout event={ev.get('title', '?')} impact={impact}"
            )
    return FilterResult.ok()


def correlation_matrix(returns: pd.DataFrame) -> pd.DataFrame:
    return returns.corr()


def check_max_open_positions(
    open_other_symbols: Sequence[str],
    max_open_positions: int,
) -> FilterResult:
    """Reject a new entry if too many other symbols already have open positions.

    Does not affect managing / DCA on an already-open position for the current symbol.
    """
    if max_open_positions <= 0:
        return FilterResult.ok()
    n = len(open_other_symbols)
    if n >= max_open_positions:
        return FilterResult.reject(
            f"max_open_positions={max_open_positions} already_open={list(open_other_symbols)}"
        )
    return FilterResult.ok()


def would_breach_correlation(
    candidate: str,
    open_symbols: Sequence[str],
    returns: pd.DataFrame,
    threshold: float,
    max_correlated: int,
) -> FilterResult:
    """Reject if candidate is highly correlated with too many open positions."""
    if candidate not in returns.columns or not open_symbols:
        return FilterResult.ok()
    corr = returns.corr()
    hot = 0
    details: list[str] = []
    for sym in open_symbols:
        if sym == candidate or sym not in corr.columns:
            continue
        c = float(corr.loc[candidate, sym])
        if abs(c) >= threshold:
            hot += 1
            details.append(f"{sym}:{c:.2f}")
    if hot >= max_correlated:
        return FilterResult.reject(f"correlation_limit hot={hot} {details}")
    return FilterResult.ok()


def returns_from_closes(closes: dict[str, pd.Series], lookback: int = 100) -> pd.DataFrame:
    frame = pd.DataFrame({k: v for k, v in closes.items()}).ffill().dropna()
    if len(frame) > lookback:
        frame = frame.iloc[-lookback:]
    return frame.pct_change().dropna()
