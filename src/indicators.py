"""Technical indicators used by the strategy (pure pandas, no TA-Lib required)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """Wilder ADX(period)."""
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = true_range(high, low, close)
    atr_s = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    plus_di = (
        100
        * pd.Series(plus_dm, index=high.index)
        .ewm(alpha=1 / period, min_periods=period, adjust=False)
        .mean()
        / atr_s.replace(0, np.nan)
    )
    minus_di = (
        100
        * pd.Series(minus_dm, index=high.index)
        .ewm(alpha=1 / period, min_periods=period, adjust=False)
        .mean()
        / atr_s.replace(0, np.nan)
    )
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(
        0.0
    )
    return dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean().fillna(0.0)


def atr_percentile(
    atr_series: pd.Series, lookback: int = 100, current: float | None = None
) -> float:
    """Percentile rank of current ATR within the last `lookback` values (0–100).

    Uses mid-rank for ties so a flat series yields ~50 rather than 100.
    """
    window = atr_series.dropna().iloc[-lookback:]
    if len(window) < max(10, lookback // 5):
        return 50.0
    value = float(window.iloc[-1] if current is None else current)
    less = float((window < value).sum())
    equal = float((window == value).sum())
    return (less + 0.5 * equal) / len(window) * 100.0


def enrich_ohlcv(
    df: pd.DataFrame,
    *,
    rsi_period: int = 14,
    adx_period: int = 14,
    atr_period: int = 14,
) -> pd.DataFrame:
    """Add RSI / ADX / ATR columns to an OHLCV frame."""
    out = df.copy()
    out["rsi"] = rsi(out["close"], rsi_period)
    out["adx"] = adx(out["high"], out["low"], out["close"], adx_period)
    out["atr"] = atr(out["high"], out["low"], out["close"], atr_period)
    out["atr_pct"] = out["atr"] / out["close"]
    return out
