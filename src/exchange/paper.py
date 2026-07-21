"""Paper / simulation exchange — fills at mark with fees & funding."""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from src.position import Position

logger = logging.getLogger(__name__)


@dataclass
class PaperAccount:
    cash: float  # free USDT (not locked as margin)
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    funding_paid: float = 0.0


class PaperExchange:
    """In-memory exchange for paper trading and tests."""

    def __init__(
        self,
        initial_equity: float = 10_000.0,
        taker_fee: float = 0.0004,
        leverage: int = 5,
    ) -> None:
        self.taker_fee = taker_fee
        self.leverage = leverage
        self.account = PaperAccount(cash=initial_equity)
        self.marks: dict[str, float] = {}
        self.ohlcv: dict[tuple[str, str], pd.DataFrame] = {}
        self.funding_rates: dict[str, float] = {}
        self.open_interest: dict[str, float] = {}
        self.oi_history: dict[str, list[float]] = {}
        self.orders: list[dict[str, Any]] = []

    def set_mark(self, symbol: str, price: float) -> None:
        px = float(price)
        if px <= 0 or px != px:  # NaN
            raise ValueError(f"Invalid mark price for {symbol}: {price}")
        self.marks[symbol] = px

    def set_ohlcv(self, symbol: str, timeframe: str, df: pd.DataFrame) -> None:
        self.ohlcv[(symbol, timeframe)] = df

    def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1m", limit: int = 200, since: int | None = None
    ) -> pd.DataFrame:
        df = self.ohlcv.get((symbol, timeframe))
        if df is None:
            raise KeyError(f"No OHLCV for {symbol} {timeframe}")
        return df.iloc[-limit:].copy()

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        price = self.marks.get(symbol)
        if price is None and (symbol, "1m") in self.ohlcv:
            price = float(self.ohlcv[(symbol, "1m")]["close"].iloc[-1])
        return {"symbol": symbol, "last": price, "close": price}

    def fetch_free_usdt(self) -> float:
        return float(self.account.cash)

    def equity(self) -> float:
        locked = sum(p.margin for p in self.account.positions.values() if p.is_open)
        upnl = 0.0
        for sym, pos in self.account.positions.items():
            if pos.is_open:
                mark = self.marks.get(sym, pos.avg_entry)
                if mark and mark > 0:
                    upnl += pos.unrealized_pnl(mark)
        return self.account.cash + locked + upnl

    def fetch_funding_rate(self, symbol: str) -> float:
        return float(self.funding_rates.get(symbol, 0.0))

    def fetch_open_interest(self, symbol: str) -> float:
        return float(self.open_interest.get(symbol, 0.0))

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self.leverage = leverage

    def set_margin_mode(self, symbol: str, mode: str = "isolated") -> None:
        return None

    def create_market_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = params or {}
        price = self.marks.get(symbol)
        if price is None or price <= 0:
            raise RuntimeError(f"No valid mark price for {symbol}")
        if amount <= 0:
            raise RuntimeError(f"Invalid order amount: {amount}")
        notional = amount * price
        fee = abs(notional * self.taker_fee)
        order_id = params.get("clientOrderId") or str(uuid.uuid4())
        pos = self.account.positions.get(symbol) or Position(
            symbol=symbol, leverage=self.leverage
        )

        if side.lower() == "buy":
            margin = float(params.get("margin") or (notional / self.leverage))
            if margin <= 0:
                raise RuntimeError(f"Invalid margin: {margin}")
            cost = margin + fee
            if cost > self.account.cash + 1e-9:
                raise RuntimeError("Insufficient free margin")
            self.account.cash -= cost
            is_dca = pos.is_open
            pos.add(price, amount, margin, fee=fee, is_dca=is_dca)
            if pos.avg_entry <= 0:
                raise RuntimeError("avg_entry must be positive after buy")
            self.account.positions[symbol] = pos
            self.account.fees_paid += fee
        elif side.lower() == "sell":
            if not pos.is_open:
                raise RuntimeError("No position to sell")
            if pos.avg_entry <= 0:
                raise RuntimeError("Corrupt position avg_entry — refuse sell")
            margin_before = pos.margin
            qty_before = pos.qty
            if amount > qty_before + 1e-12:
                amount = qty_before
            pnl_net = pos.reduce(amount, price, fee=fee)
            # Cap absurd PnL (e.g. if prices were ever corrupted)
            max_abs_pnl = max(abs(margin_before) * 20, notional * 2, 1.0)
            if abs(pnl_net) > max_abs_pnl:
                logger.error(
                    "Refusing absurd paper PnL %.4e on %s (cap %.4e) — check mark/entry",
                    pnl_net,
                    symbol,
                    max_abs_pnl,
                )
                raise RuntimeError(f"Absurd paper PnL {pnl_net:.4e} rejected")
            released = margin_before * (amount / qty_before)
            self.account.cash += released + pnl_net
            self.account.realized_pnl += pnl_net
            self.account.fees_paid += fee
            if not pos.is_open:
                self.account.positions.pop(symbol, None)
            else:
                self.account.positions[symbol] = pos
        else:
            raise ValueError(side)

        order = {
            "id": order_id,
            "clientOrderId": order_id,
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "price": price,
            "fee": fee,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "closed",
        }
        self.orders.append(order)
        return order

    def add_margin(self, symbol: str, amount: float) -> Any:
        pos = self.account.positions.get(symbol)
        if not pos or not pos.is_open:
            raise RuntimeError("No position")
        if amount > self.account.cash:
            raise RuntimeError("Insufficient free margin for top-up")
        self.account.cash -= amount
        pos.add_margin(amount)
        return {"amount": amount}

    def apply_funding(self, symbol: str, rate: float) -> float:
        pos = self.account.positions.get(symbol)
        if not pos or not pos.is_open:
            return 0.0
        payment = rate * pos.notional_value()
        self.account.cash -= payment
        pos.funding_paid += payment
        self.account.funding_paid += payment
        return payment
