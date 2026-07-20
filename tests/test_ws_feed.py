"""Unit tests for WS candle merge / OHLCV cache helpers (no live network)."""
from __future__ import annotations

from src.exchange.ws_feed import _merge_candles, _ohlcv_to_df


def test_merge_candles_updates_forming_bar():
    base = [
        [1000, 1, 2, 0.5, 1.5, 10],
        [2000, 1.5, 2.5, 1.4, 2.0, 11],
    ]
    upd = [
        [2000, 1.5, 2.6, 1.4, 2.1, 12],  # same open time — replace
        [3000, 2.1, 2.2, 2.0, 2.15, 5],
    ]
    merged = _merge_candles(base, upd, keep=500)
    assert len(merged) == 3
    assert merged[1][4] == 2.1
    assert merged[2][0] == 3000


def test_ohlcv_to_df_limit():
    raw = [[i * 60_000, 1, 2, 0.5, 1.5, 10] for i in range(10)]
    df = _ohlcv_to_df(raw, limit=3)
    assert len(df) == 3
    assert float(df["close"].iloc[-1]) == 1.5


def test_ccxt_exchange_without_ws_still_builds():
    from src.config import ExchangeConfig
    from src.exchange.ccxt_client import CCXTExchange

    ex = CCXTExchange(ExchangeConfig(id="binanceusdm", testnet=True), use_ws=False)
    assert ex._feed is None
    ex.start()  # no-op
    ex.stop()
