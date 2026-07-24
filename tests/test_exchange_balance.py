"""Exchange futures balance snapshot helper."""
from __future__ import annotations

from src.config import ExchangeConfig
from src.exchange.ccxt_client import fetch_futures_usdt_snapshot


def test_snapshot_without_keys():
    cfg = ExchangeConfig(api_key="", api_secret="")
    snap = fetch_futures_usdt_snapshot(cfg)
    assert snap["ok"] is False
    assert "API" in (snap.get("error") or "")
