"""Risk manager: sizing, exposure, daily loss, circuit breaker, margin top-ups."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable

from src.config import RiskConfig, StrategyConfig
from src.position import Position


@dataclass
class RiskState:
    day: date | None = None
    day_start_equity: float = 0.0
    peak_equity: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    daily_pnl: float = 0.0


@dataclass
class SizeDecision:
    notional: float
    qty: float
    margin: float
    equity_pct: float
    reasons: list[str] = field(default_factory=list)


class RiskManager:
    def __init__(self, strategy: StrategyConfig, risk: RiskConfig) -> None:
        self.strategy = strategy
        self.risk = risk
        self.state = RiskState()

    def update_equity(self, equity: float, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        d = now.date()
        if self.state.day != d:
            self.state.day = d
            self.state.day_start_equity = equity
            self.state.daily_pnl = 0.0
            # Daily halt resets each day; circuit breaker does not.
            if self.state.halted and self.state.halt_reason.startswith("daily_loss"):
                self.state.halted = False
                self.state.halt_reason = ""
        self.state.peak_equity = max(self.state.peak_equity, equity)
        if self.state.day_start_equity > 0:
            self.state.daily_pnl = equity - self.state.day_start_equity

        # Circuit breaker on peak drawdown
        if self.state.peak_equity > 0:
            dd = (self.state.peak_equity - equity) / self.state.peak_equity
            if dd >= self.risk.circuit_breaker_drawdown_pct:
                self.state.halted = True
                self.state.halt_reason = f"circuit_breaker dd={dd:.2%}"

        # Max daily loss
        if self.state.day_start_equity > 0:
            day_loss = -self.state.daily_pnl / self.state.day_start_equity
            if day_loss >= self.risk.max_daily_loss_pct:
                self.state.halted = True
                self.state.halt_reason = f"daily_loss={day_loss:.2%}"

    def can_open_new(self) -> tuple[bool, str]:
        if self.state.halted:
            return False, self.state.halt_reason or "halted"
        return True, ""

    def resume_after_take_profit(self, equity: float) -> None:
        """
        After a profitable full close — always allow the next entry.

        Resets halt (daily / circuit breaker) and rebases peak equity so a past
        Max DD does not immediately re-block new trades for the rest of the run.
        """
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.peak_equity = max(float(equity), 0.0)
        if self.state.day_start_equity <= 0:
            self.state.day_start_equity = float(equity)

    def equity_pct_for_atr(self, atr_pct: float) -> float:
        """Inverse-vol sizing around configured equity %. Higher ATR → smaller size."""
        base = self.strategy.initial_equity_pct
        if not self.strategy.dynamic_sizing or atr_pct <= 0:
            return base
        ref = self.strategy.atr_sizing_ref_pct
        scaled = base * (ref / atr_pct)
        return float(
            max(self.strategy.min_equity_pct, min(self.strategy.max_equity_pct, scaled))
        )

    def required_margin_for_survival(
        self, notional: float, leverage: int
    ) -> float:
        """
        Margin needed so an isolated long survives `min_adverse_move_pct` move.

        Adverse move loss ≈ notional * move. Need margin >= that loss (+ buffer).
        Also respect exchange initial margin = notional / leverage.
        """
        initial = notional / leverage
        survival = notional * self.risk.min_adverse_move_pct
        return max(initial, survival)

    def size_order(
        self,
        equity: float,
        price: float,
        atr_pct: float,
        leverage: int | None = None,
    ) -> SizeDecision:
        lev = leverage or self.strategy.leverage
        pct = self.equity_pct_for_atr(atr_pct)
        # Fixed USDT margin for entry if configured (handy for $100 deposit)
        if self.strategy.entry_margin_usdt and self.strategy.entry_margin_usdt > 0:
            margin_budget = float(self.strategy.entry_margin_usdt)
            pct = margin_budget / equity if equity > 0 else 0.0
            reasons = [f"fixed_margin_usdt={margin_budget:.2f}"]
        else:
            margin_budget = equity * pct
            reasons = [f"atr_pct={atr_pct:.5f}", f"equity_pct={pct:.4f}"]
        min_margin_rate = max(1.0 / lev, self.risk.min_adverse_move_pct)
        notional = margin_budget / min_margin_rate
        qty = notional / price if price > 0 else 0.0
        margin = notional * min_margin_rate
        return SizeDecision(
            notional=notional,
            qty=qty,
            margin=margin,
            equity_pct=pct,
            reasons=reasons,
        )

    def needs_margin_topup(
        self, position: Position, mark: float
    ) -> tuple[bool, float]:
        """Return (needed, amount) to restore liq distance buffer."""
        if not position.is_open:
            return False, 0.0
        dist = position.distance_to_liq_pct(mark)
        if dist >= self.risk.margin_topup_buffer_pct:
            return False, 0.0
        # Top up enough to push distance back to ~2x buffer
        target_dist = self.risk.margin_topup_buffer_pct * 2
        # Increasing margin by Δ raises liq distance roughly by Δ/(mark*qty)
        # We want (mark - liq) / mark = target_dist
        # => mark - liq = target_dist * mark
        # liq ≈ entry - margin/qty + entry*mmr
        # Solve for margin' : mark - (entry - margin'/qty + entry*mmr) = target * mark
        mmr = 0.004
        needed_margin = position.qty * (
            position.avg_entry
            - mark
            + target_dist * mark
            + position.avg_entry * mmr
        )
        amount = max(0.0, needed_margin - position.margin)
        return amount > 0, amount

    def max_total_margin(self, equity: float) -> float:
        """Soft cap: don't commit more than ~50% equity as isolated margin."""
        return equity * 0.5

    def exposure_ok(
        self, equity: float, open_positions: Iterable[Position], extra_margin: float
    ) -> tuple[bool, str]:
        used = sum(p.margin for p in open_positions) + extra_margin
        cap = self.max_total_margin(equity)
        if used > cap:
            return False, f"exposure {used:.2f}>{cap:.2f}"
        return True, ""
