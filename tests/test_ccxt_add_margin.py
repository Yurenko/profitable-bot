"""Binance USDM add_margin uses fapi positionMargin when unified fails."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.config import ExchangeConfig
from src.exchange.ccxt_client import CCXTExchange, ExchangeError


def _exchange() -> CCXTExchange:
    cfg = ExchangeConfig(id="binanceusdm", testnet=False)
    ex = CCXTExchange(cfg)
    ex.client = MagicMock()
    ex.client.has = {"addMargin": True}
    ex.client.market.return_value = {"id": "SOLUSDT"}
    return ex


def test_add_margin_fapi_fallback():
    ex = _exchange()
    ex.client.add_margin.side_effect = Exception("not supported")
    ex.client.fapiPrivatePostPositionMargin.return_value = {"amount": 21.84}

    with patch.object(ex, "_call", side_effect=lambda name, *a, **k: getattr(ex.client, name)(*a, **k)):
        result = ex.add_margin("SOL/USDT:USDT", 21.84)

    assert result == {"amount": 21.84}
    ex.client.load_markets.assert_called_once()
    ex.client.fapiPrivatePostPositionMargin.assert_called_once_with(
        {
            "symbol": "SOLUSDT",
            "amount": 21.84,
            "positionSide": "BOTH",
            "type": 1,
        }
    )


def test_add_margin_raises_when_both_fail():
    ex = _exchange()
    ex.client.add_margin.side_effect = Exception("unified fail")
    ex.client.fapiPrivatePostPositionMargin.side_effect = Exception("fapi fail")

    with patch.object(ex, "_call", side_effect=lambda name, *a, **k: getattr(ex.client, name)(*a, **k)):
        with pytest.raises(ExchangeError):
            ex.add_margin("SOL/USDT:USDT", 10.0)
