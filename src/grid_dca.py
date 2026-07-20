"""Grid / Martingale DCA planner — % step between buys, growing order size."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GridLevel:
    """One planned DCA buy below entry."""

    level: int  # 1 = first add below entry, 2 = second, …
    price: float
    qty: float
    notional: float
    drop_from_entry_pct: float
    size_vs_base: float  # qty / base_qty


@dataclass
class GridPlan:
    entry_price: float
    base_qty: float
    step_pct: float
    size_multiplier: float
    levels: list[GridLevel]

    def next_unfilled(self, filled_levels: int) -> GridLevel | None:
        """filled_levels = dca_level - 1 when dca_level counts entry as 1."""
        idx = filled_levels  # number of adds already done
        if idx < 0 or idx >= len(self.levels):
            return None
        return self.levels[idx]


def build_grid(
    entry_price: float,
    base_qty: float,
    *,
    step_pct: float = 0.04,
    size_multiplier: float = 1.25,
    num_adds: int = 4,
) -> GridPlan:
    """
    Build buy limits below entry.

    Example (like Bybit screenshot):
      step_pct=0.04, size_multiplier=1.25, num_adds=4
      prices ≈ entry*(1-0.04)^n
      qtys  ≈ base * 1.25^n
    """
    if entry_price <= 0 or base_qty <= 0:
        raise ValueError("entry_price and base_qty must be positive")
    if step_pct <= 0 or step_pct >= 1:
        raise ValueError("step_pct must be in (0, 1)")
    if size_multiplier < 1:
        raise ValueError("size_multiplier must be >= 1")

    levels: list[GridLevel] = []
    for n in range(1, num_adds + 1):
        price = entry_price * ((1.0 - step_pct) ** n)
        mult = size_multiplier**n
        qty = base_qty * mult
        levels.append(
            GridLevel(
                level=n,
                price=price,
                qty=qty,
                notional=qty * price,
                drop_from_entry_pct=1.0 - (price / entry_price),
                size_vs_base=mult,
            )
        )
    return GridPlan(
        entry_price=entry_price,
        base_qty=base_qty,
        step_pct=step_pct,
        size_multiplier=size_multiplier,
        levels=levels,
    )


def reverse_engineer_grid(
    prices: list[float],
    qtys: list[float],
) -> dict[str, float]:
    """Estimate step_% and size multiplier from observed order ladder."""
    if len(prices) < 2 or len(qtys) < 2:
        raise ValueError("need at least 2 prices and qtys")
    drops = [(prices[i] - prices[i + 1]) / prices[i] for i in range(len(prices) - 1)]
    # Prefer consecutive limit prices (skip entry if mixed)
    q_ratios = [qtys[i + 1] / qtys[i] for i in range(len(qtys) - 1) if qtys[i] > 0]
    step = sum(drops) / len(drops)
    # geometric mean of size ratios
    prod = 1.0
    for r in q_ratios:
        prod *= r
    mult = prod ** (1 / len(q_ratios))
    return {
        "avg_step_pct": step,
        "size_multiplier": mult,
        "min_step_pct": min(drops),
        "max_step_pct": max(drops),
    }
