"""Core strategy: long-only mean-reversion with DCA and TP logic."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from src.config import StrategyConfig
from src.filters import (
    FilterResult,
    MarketSnapshot,
    check_dca_rsi,
    check_entry_indicators,
    check_funding_oi,
)
from src.position import Position
from src.grid_dca import GridPlan, build_grid
from src.risk_manager import RiskManager, SizeDecision


class ActionType(str, Enum):
    HOLD = "hold"
    ENTER = "enter"
    DCA = "dca"
    PLACE_GRID = "place_grid"
    PARTIAL_TP = "partial_tp"
    FULL_TP = "full_tp"
    TRAIL_EXIT = "trail_exit"
    MARGIN_TOPUP = "margin_topup"


@dataclass
class StrategyAction:
    action: ActionType
    reason: str = ""
    size: Optional[SizeDecision] = None
    close_qty: float = 0.0
    margin_amount: float = 0.0
    tp_pct: float = 0.0
    grid: Optional[GridPlan] = None
    limit_price: float = 0.0


class MeanReversionDCAStrategy:
    def __init__(self, cfg: StrategyConfig, risk: RiskManager) -> None:
        self.cfg = cfg
        self.risk = risk

    def tp_target_pct(self, rsi_5m: float) -> float:
        if rsi_5m < self.cfg.rsi_5m_strong_oversold:
            return self.cfg.tp_strong_pct
        return self.cfg.tp_min_pct

    def evaluate_entry_filters(self, snap: MarketSnapshot) -> FilterResult:
        r1 = check_entry_indicators(snap, self.cfg)
        if not r1.allowed:
            return r1
        return check_funding_oi(snap, self.cfg)

    def _attach_grid(self, pos: Position, entry_price: float, base_qty: float) -> GridPlan:
        num_adds = max(0, self.cfg.max_dca_levels - 1)
        plan = build_grid(
            entry_price,
            base_qty,
            step_pct=self.cfg.grid_step_pct,
            size_multiplier=self.cfg.grid_size_multiplier,
            num_adds=num_adds,
        )
        pos.meta["grid"] = {
            "step_pct": plan.step_pct,
            "size_multiplier": plan.size_multiplier,
            "levels": [
                {"level": lv.level, "price": lv.price, "qty": lv.qty, "filled": False}
                for lv in plan.levels
            ],
        }
        return plan

    def _grid_from_meta(self, pos: Position) -> GridPlan | None:
        raw = pos.meta.get("grid")
        if not raw:
            return None
        levels_raw = raw.get("levels") or []
        from src.grid_dca import GridLevel

        levels = [
            GridLevel(
                level=int(x["level"]),
                price=float(x["price"]),
                qty=float(x["qty"]),
                notional=float(x["qty"]) * float(x["price"]),
                drop_from_entry_pct=1.0 - float(x["price"]) / pos.avg_entry if pos.avg_entry else 0.0,
                size_vs_base=1.0,
            )
            for x in levels_raw
        ]
        return GridPlan(
            entry_price=float(raw.get("entry_price", pos.avg_entry)),
            base_qty=float(raw.get("base_qty", 0)),
            step_pct=float(raw.get("step_pct", self.cfg.grid_step_pct)),
            size_multiplier=float(raw.get("size_multiplier", self.cfg.grid_size_multiplier)),
            levels=levels,
        )

    def _decide_grid_dca(
        self, snap: MarketSnapshot, pos: Position, equity: float
    ) -> StrategyAction:
        plan = self._grid_from_meta(pos)
        if plan is None or not plan.levels:
            # Rebuild if missing (restart)
            base_qty = float(pos.meta.get("base_qty", pos.qty / max(pos.dca_level, 1)))
            entry = float(pos.meta.get("grid_entry", pos.avg_entry))
            plan = self._attach_grid(pos, entry, base_qty)
            pos.meta["grid"]["entry_price"] = entry
            pos.meta["grid"]["base_qty"] = base_qty

        adds_done = max(0, pos.dca_level - 1)
        if adds_done >= len(plan.levels) or pos.dca_level >= self.cfg.max_dca_levels:
            return StrategyAction(ActionType.HOLD, reason="grid_complete")

        nxt = plan.levels[adds_done]
        # Live exchange already has resting limit orders — never market-DCA the same level
        # (fills are applied by bot._sync_grid_limit_fills). Paper keeps price-hit market path.
        if pos.meta.get("grid", {}).get("limits_live"):
            return StrategyAction(
                ActionType.HOLD,
                reason=f"wait_grid_L{nxt.level}@{nxt.price:.4f}",
            )

        # Paper / no live limits: fill when price trades at/below planned level
        if snap.price > nxt.price:
            return StrategyAction(
                ActionType.HOLD,
                reason=f"wait_grid_L{nxt.level}@{nxt.price:.4f}",
            )

        # DCA / grid adds are never blocked by circuit breaker — only new entries are
        notional = nxt.qty * snap.price
        min_margin_rate = max(1.0 / self.cfg.leverage, self.risk.risk.min_adverse_move_pct)
        margin = notional * min_margin_rate
        size = SizeDecision(
            notional=notional,
            qty=nxt.qty,
            margin=margin,
            equity_pct=margin / equity if equity else 0.0,
            reasons=[f"grid_L{nxt.level}", f"mult={self.cfg.grid_size_multiplier}"],
        )
        ok, exp = self.risk.exposure_ok(equity, [pos], size.margin)
        if not ok:
            return StrategyAction(ActionType.HOLD, reason=exp)

        # Mark planned level filled in meta (persisted after execute)
        levels = pos.meta.get("grid", {}).get("levels", [])
        if adds_done < len(levels):
            levels[adds_done]["filled"] = True

        return StrategyAction(
            ActionType.DCA,
            reason=f"grid_fill_L{nxt.level}_drop={nxt.drop_from_entry_pct:.2%}",
            size=size,
            limit_price=nxt.price,
        )

    def decide(
        self,
        snap: MarketSnapshot,
        position: Position | None,
        equity: float,
        *,
        news_ok: bool = True,
        news_reason: str = "",
        correlation_ok: bool = True,
        correlation_reason: str = "",
    ) -> StrategyAction:
        pos = position if position and position.is_open else None

        if pos is not None:
            need, amount = self.risk.needs_margin_topup(pos, snap.price)
            if need:
                return StrategyAction(
                    ActionType.MARGIN_TOPUP,
                    reason="liq_buffer",
                    margin_amount=amount,
                )

            if snap.price > pos.peak_price:
                pos.peak_price = snap.price

            pnl_pct = pos.unrealized_pnl_pct(snap.price)
            target = self.tp_target_pct(snap.rsi_5m)
            lt = self.cfg.long_term_mode

            if self.cfg.trailing_tp_enabled and not lt:
                if pnl_pct >= self.cfg.trailing_activate_pct:
                    pos.trailing_active = True
                if pos.trailing_active and pos.peak_price > 0:
                    drawdown_from_peak = (pos.peak_price - snap.price) / pos.peak_price
                    if drawdown_from_peak >= self.cfg.trailing_callback_pct:
                        return StrategyAction(
                            ActionType.TRAIL_EXIT,
                            reason=f"trail peak={pos.peak_price:.4f}",
                            close_qty=pos.qty,
                            tp_pct=pnl_pct,
                        )

            if self.cfg.auto_take_profit and pnl_pct >= target:
                partial_ok = (
                    self.cfg.partial_close_enabled
                    and not lt
                    and not pos.partial_taken
                    and target <= self.cfg.tp_min_pct + 1e-12
                )
                if partial_ok:
                    qty = pos.qty * self.cfg.partial_close_pct
                    return StrategyAction(
                        ActionType.PARTIAL_TP,
                        reason=f"partial_tp={target:.2%}",
                        close_qty=qty,
                        tp_pct=target,
                    )
                return StrategyAction(
                    ActionType.FULL_TP,
                    reason=f"tp={target:.2%} rsi5m={snap.rsi_5m:.1f}",
                    close_qty=pos.qty,
                    tp_pct=target,
                )

            # --- DCA ---
            if self.cfg.dca_mode == "grid":
                return self._decide_grid_dca(snap, pos, equity)

            min_adverse = self.cfg.dca_min_adverse_pct
            last_fill = float(pos.meta.get("last_fill_price", pos.avg_entry))
            adverse_from_last = (last_fill - snap.price) / last_fill if last_fill else 0.0
            if (
                pnl_pct < -min_adverse
                and adverse_from_last >= min_adverse
                and pos.dca_level < self.cfg.max_dca_levels
            ):
                dca_f = check_dca_rsi(snap, self.cfg)
                if dca_f.allowed:
                    atr_pct = snap.atr_1m / snap.price if snap.price else 0.0
                    size = self.risk.size_order(equity, snap.price, atr_pct)
                    ok, exp = self.risk.exposure_ok(equity, [pos], size.margin)
                    if not ok:
                        return StrategyAction(ActionType.HOLD, reason=exp)
                    return StrategyAction(
                        ActionType.DCA,
                        reason="rsi_15m_oversold",
                        size=size,
                    )

            return StrategyAction(ActionType.HOLD, reason="manage_open")

        # Flat — consider entry
        can, why = self.risk.can_open_new()
        if not can:
            return StrategyAction(ActionType.HOLD, reason=why)
        if not news_ok:
            return StrategyAction(ActionType.HOLD, reason=news_reason or "news")
        if not correlation_ok:
            return StrategyAction(ActionType.HOLD, reason=correlation_reason or "corr")

        filt = self.evaluate_entry_filters(snap)
        if not filt.allowed:
            return StrategyAction(ActionType.HOLD, reason=";".join(filt.reasons))

        atr_pct = snap.atr_1m / snap.price if snap.price else 0.0
        size = self.risk.size_order(equity, snap.price, atr_pct)
        ok, exp = self.risk.exposure_ok(equity, [], size.margin)
        if not ok:
            return StrategyAction(ActionType.HOLD, reason=exp)

        return StrategyAction(
            ActionType.ENTER,
            reason="entry_filters_ok",
            size=size,
        )
