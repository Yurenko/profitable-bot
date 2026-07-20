"""Position model: average entry, DCA levels, liquidation distance, PnL."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Position:
    """Long-only isolated-margin perpetual position."""

    symbol: str
    leverage: int
    qty: float = 0.0
    avg_entry: float = 0.0
    margin: float = 0.0  # isolated margin posted (USDT)
    dca_level: int = 0  # 0 = flat; 1 = initial; 2..N = adds
    partial_taken: bool = False
    peak_price: float = 0.0
    trailing_active: bool = False
    opened_at: datetime | None = None
    fees_paid: float = 0.0
    funding_paid: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.qty > 0

    def notional_value(self) -> float:
        return self.qty * self.avg_entry

    def unrealized_pnl(self, mark: float) -> float:
        return (mark - self.avg_entry) * self.qty

    def unrealized_pnl_pct(self, mark: float) -> float:
        if self.avg_entry <= 0:
            return 0.0
        return (mark - self.avg_entry) / self.avg_entry

    def liquidation_price(self, maintenance_rate: float = 0.004) -> float:
        """
        Approximate isolated long liquidation price.

        Remaining buffer ≈ margin / qty; liq when price falls by that buffer
        adjusted for maintenance margin.
        """
        if self.qty <= 0:
            return 0.0
        buffer = self.margin / self.qty
        return max(0.0, self.avg_entry - buffer + self.avg_entry * maintenance_rate)

    def distance_to_liq_pct(self, mark: float, maintenance_rate: float = 0.004) -> float:
        liq = self.liquidation_price(maintenance_rate)
        if mark <= 0:
            return 0.0
        return (mark - liq) / mark

    def add(
        self,
        price: float,
        qty: float,
        margin_added: float,
        fee: float = 0.0,
        *,
        is_dca: bool = False,
    ) -> None:
        if qty <= 0:
            raise ValueError("qty must be positive")
        if not self.is_open:
            self.avg_entry = price
            self.qty = qty
            self.margin = margin_added
            self.dca_level = 1
            self.opened_at = utcnow()
            self.peak_price = price
        else:
            new_qty = self.qty + qty
            self.avg_entry = (self.avg_entry * self.qty + price * qty) / new_qty
            self.qty = new_qty
            self.margin += margin_added
            if is_dca:
                self.dca_level += 1
        self.meta["last_fill_price"] = price
        self.fees_paid += fee

    def add_margin(self, amount: float) -> None:
        if amount <= 0:
            raise ValueError("margin top-up must be positive")
        self.margin += amount

    def reduce(self, qty: float, price: float, fee: float = 0.0) -> float:
        """Reduce position; returns realized PnL net of this fill's fee."""
        if qty <= 0 or qty > self.qty + 1e-12:
            raise ValueError("invalid reduce qty")
        pnl = (price - self.avg_entry) * qty
        if self.qty - qty <= 1e-12:
            self.qty = 0.0
            self.margin = 0.0
            self.dca_level = 0
            self.partial_taken = False
            self.trailing_active = False
            self.peak_price = 0.0
            self.avg_entry = 0.0
        else:
            frac = qty / self.qty
            self.margin -= self.margin * frac
            self.qty -= qty
        self.fees_paid += fee
        return pnl - fee

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "leverage": self.leverage,
            "qty": self.qty,
            "avg_entry": self.avg_entry,
            "margin": self.margin,
            "dca_level": self.dca_level,
            "partial_taken": self.partial_taken,
            "peak_price": self.peak_price,
            "trailing_active": self.trailing_active,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "fees_paid": self.fees_paid,
            "funding_paid": self.funding_paid,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Position":
        opened = data.get("opened_at")
        return cls(
            symbol=data["symbol"],
            leverage=int(data["leverage"]),
            qty=float(data.get("qty", 0)),
            avg_entry=float(data.get("avg_entry", 0)),
            margin=float(data.get("margin", 0)),
            dca_level=int(data.get("dca_level", 0)),
            partial_taken=bool(data.get("partial_taken", False)),
            peak_price=float(data.get("peak_price", 0)),
            trailing_active=bool(data.get("trailing_active", False)),
            opened_at=datetime.fromisoformat(opened) if opened else None,
            fees_paid=float(data.get("fees_paid", 0)),
            funding_paid=float(data.get("funding_paid", 0)),
            meta=dict(data.get("meta") or {}),
        )
