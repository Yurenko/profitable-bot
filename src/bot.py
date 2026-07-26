"""Live / paper trading loop."""
from __future__ import annotations

import logging
import signal
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from src.config import AppConfig
from src.filters import (
    MarketSnapshot,
    check_max_open_positions,
    returns_from_closes,
    would_breach_correlation,
)
from src.indicators import enrich_ohlcv
from src.news.calendar import EconomicCalendar
from src.notify.telegram import TelegramNotifier
from src.position import Position
from src.risk_manager import RiskManager
from src.state.store import StateStore
from src.strategy import ActionType, MeanReversionDCAStrategy

logger = logging.getLogger(__name__)


class TradingBot:
    def __init__(
        self,
        cfg: AppConfig,
        exchange: Any,
        store: StateStore,
        calendar: EconomicCalendar | None = None,
    ) -> None:
        self.cfg = cfg
        self.exchange = exchange
        self.store = store
        self.calendar = calendar or EconomicCalendar(cfg.news)
        self.risk = RiskManager(cfg.strategy, cfg.risk)
        self.strategy = MeanReversionDCAStrategy(cfg.strategy, self.risk)
        self.notify = TelegramNotifier()
        self.positions: dict[str, Position] = {}
        self._running = False
        self._close_prices: dict[str, pd.Series] = {}
        self._oi_history: dict[str, list[float]] = {}

        if self.notify.enabled:
            logger.info("Telegram notifications enabled (chat_id set)")
        else:
            logger.info(
                "Telegram notifications disabled — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"
            )

        # Restore persistent state, but drop positions for symbols no longer in config
        all_positions = store.load_positions()
        active_symbols = set(cfg.symbols)
        for sym in list(all_positions.keys()):
            if sym not in active_symbols:
                logger.warning(
                    "Dropping restored position for %s — not in current symbol list", sym
                )
                store.audit("position_dropped_not_in_symbols", symbol=sym)
                store.delete_position(sym)
                all_positions.pop(sym)
        self.positions = all_positions
        last_eq = store.last_equity(0.0)
        if last_eq > 0:
            self.risk.update_equity(last_eq)
            self.risk.state.peak_equity = max(self.risk.state.peak_equity, last_eq)

    def _client_order_id(self, symbol: str, action: str) -> str:
        """Binance requires clientOrderId length < 36 chars."""
        base = symbol.split("/")[0].replace("-", "")[:6]
        act_map = {
            "enter": "en",
            "dca": "dc",
            "full_tp": "tp",
            "partial_tp": "pt",
            "trail_exit": "tr",
            "margin_topup": "mg",
        }
        if action.startswith("grid"):
            act = "g" + "".join(c for c in action if c.isdigit())[:2] or "g"
        else:
            act = act_map.get(action, (action or "x")[:3])
        # last 8 digits of ms + 6 hex ≈ unique, keeps total well under 36
        ts = f"{int(time.time() * 1000) % 10**8:08d}"
        rnd = uuid.uuid4().hex[:6]
        coid = f"m{base}{act}{ts}{rnd}"
        return coid[:35]

    def _fetch_mtf(self, symbol: str) -> dict[str, pd.DataFrame]:
        s = self.cfg.strategy
        out: dict[str, pd.DataFrame] = {}
        for tf, lim in (("1m", 200), ("5m", 200), ("15m", 200)):
            df = self.exchange.fetch_ohlcv(symbol, timeframe=tf, limit=lim)
            out[tf] = enrich_ohlcv(
                df,
                rsi_period=s.rsi_period,
                adx_period=s.adx_period,
                atr_period=s.atr_period,
            )  # keyword-only indicator periods
        return out

    def build_snapshot(self, symbol: str) -> MarketSnapshot:
        mtf = self._fetch_mtf(symbol)
        d1, d5, d15 = mtf["1m"], mtf["5m"], mtf["15m"]
        price = float(d1["close"].iloc[-1])
        self._close_prices[symbol] = d1["close"]
        funding = float(self.exchange.fetch_funding_rate(symbol))
        oi = float(self.exchange.fetch_open_interest(symbol))
        hist = self._oi_history.setdefault(symbol, [])
        hist.append(oi)
        max_oi = max(self.cfg.strategy.oi_change_lookback + 5, 50)
        if len(hist) > max_oi:
            del hist[: len(hist) - max_oi]
        oi_series = pd.Series(hist) if len(hist) >= 2 else None
        return MarketSnapshot(
            symbol=symbol,
            price=price,
            rsi_1m=float(d1["rsi"].iloc[-1]),
            rsi_5m=float(d5["rsi"].iloc[-1]),
            rsi_15m=float(d15["rsi"].iloc[-1]),
            adx_1m=float(d1["adx"].iloc[-1]),
            atr_1m=float(d1["atr"].iloc[-1]),
            atr_series_1m=d1["atr"],
            funding_rate=funding,
            open_interest=oi,
            oi_series=oi_series,
            timestamp=datetime.now(timezone.utc),
        )

    def _equity(self) -> float:
        if hasattr(self.exchange, "equity"):
            return float(self.exchange.equity())
        return float(self.exchange.fetch_free_usdt())

    def _sanitize_and_save_equity(self, equity: float) -> float:
        """Persist equity; if absurd vs deposit, reset paper cash and SQLite.

        Live mode: never reset to config initial_capital — equity comes from the exchange.
        """
        from src.app_factory import _sane_equity

        # Live: trust Binance futures wallet; only reject NaN/Inf/<=0
        if getattr(self.cfg, "mode", "") == "live":
            try:
                v = float(equity)
            except (TypeError, ValueError):
                logger.error("Live equity invalid %r — skip save", equity)
                return float(self.store.last_equity(0.0) or 0.0)
            if v != v or v in (float("inf"), float("-inf")) or v <= 0:
                logger.error("Live equity absurd %s — skip save", v)
                return float(self.store.last_equity(0.0) or 0.0)
            self.store.save_equity(v)
            return v

        capital = float(self.cfg.backtest.get("initial_capital", 10_000))
        sane = _sane_equity(equity, capital)
        if sane != equity and abs(sane - equity) > 1e-6:
            logger.error(
                "Corrupt equity %.6e → reset to deposit %.2f",
                equity,
                capital,
            )
            self.store.audit("equity_sanitized", previous=equity, equity=capital)
            acct = getattr(self.exchange, "account", None)
            if acct is None and hasattr(self.exchange, "paper"):
                acct = self.exchange.paper.account
            if acct is not None:
                acct.cash = capital
                acct.positions.clear()
                self.positions.clear()
            self.store.clear_equity_history()
            self.store.save_equity(capital)
            self.risk.update_equity(capital)
            self.risk.state.peak_equity = capital
            return capital
        self.store.save_equity(sane)
        return sane

    def _execute(self, symbol: str, action_type: ActionType, decision: Any, snap: MarketSnapshot) -> None:
        coid = self._client_order_id(symbol, action_type.value)
        if not self.store.register_order(
            coid, symbol, action_type.value, {"decision": action_type.value}
        ):
            logger.warning("Duplicate order blocked: %s", coid)
            return

        try:
            if action_type in (ActionType.ENTER, ActionType.DCA):
                size = decision.size
                assert size is not None
                self.exchange.set_leverage(symbol, self.cfg.strategy.leverage)
                self.exchange.set_margin_mode(symbol, self.cfg.strategy.margin_mode)
                if hasattr(self.exchange, "set_mark"):
                    self.exchange.set_mark(symbol, snap.price)
                order = self.exchange.create_market_order(
                    symbol,
                    "buy",
                    size.qty,
                    params={"clientOrderId": coid, "margin": size.margin},
                )
                # Sync local position from paper/live
                if hasattr(self.exchange, "account"):
                    self.positions[symbol] = self.exchange.account.positions[symbol]
                else:
                    pos = self.positions.get(symbol) or Position(
                        symbol=symbol, leverage=self.cfg.strategy.leverage
                    )
                    pos.add(
                        snap.price,
                        size.qty,
                        size.margin,
                        fee=size.notional * self.cfg.risk.taker_fee,
                        is_dca=(action_type == ActionType.DCA),
                    )
                    self.positions[symbol] = pos
                self.store.save_position(self.positions[symbol])
                # After market entry — build grid of growing limit DCA levels
                if (
                    action_type == ActionType.ENTER
                    and self.cfg.strategy.dca_mode == "grid"
                    and symbol in self.positions
                ):
                    pos = self.positions[symbol]
                    plan = self.strategy._attach_grid(pos, snap.price, size.qty)
                    pos.meta["grid"]["entry_price"] = snap.price
                    pos.meta["grid"]["base_qty"] = size.qty
                    pos.meta["base_qty"] = size.qty
                    pos.meta["grid_entry"] = snap.price
                    self.store.save_position(pos)
                    self.store.audit(
                        "grid_planned",
                        symbol=symbol,
                        step_pct=plan.step_pct,
                        size_multiplier=plan.size_multiplier,
                        levels=[
                            {"level": lv.level, "price": lv.price, "qty": lv.qty}
                            for lv in plan.levels
                        ],
                    )
                    # Best-effort: place real limit orders if exchange supports it
                    if hasattr(self.exchange, "create_limit_order"):
                        for lv in plan.levels:
                            try:
                                self.exchange.create_limit_order(
                                    symbol,
                                    "buy",
                                    lv.qty,
                                    lv.price,
                                    params={"clientOrderId": self._client_order_id(symbol, f"grid{lv.level}")},
                                )
                            except Exception as exc:  # noqa: BLE001
                                logger.warning("Limit place failed L%s: %s", lv.level, exc)

                fill_price = float(order.get("price") or snap.price)
                fill_qty = float(order.get("amount") or size.qty)
                raw_fee = order.get("fee")
                if isinstance(raw_fee, dict):
                    fill_fee = float(raw_fee.get("cost") or 0.0)
                else:
                    fill_fee = float(raw_fee or 0.0)
                pos_now = self.positions[symbol]
                self.store.record_trade(
                    symbol=symbol,
                    side="buy",
                    action=action_type.value,
                    price=fill_price,
                    qty=fill_qty,
                    fee=fill_fee,
                    avg_entry=pos_now.avg_entry,
                    dca_level=pos_now.dca_level,
                    mode=self.cfg.mode,
                    client_order_id=coid,
                )
                self.store.audit(
                    "fill_buy",
                    symbol=symbol,
                    action=action_type.value,
                    price=fill_price,
                    qty=fill_qty,
                    avg_entry=pos_now.avg_entry,
                    dca_level=pos_now.dca_level,
                )
                margin = float(getattr(pos_now, "margin", 0) or 0) or None
                if action_type == ActionType.ENTER:
                    self.notify.notify_enter(
                        symbol=symbol,
                        price=fill_price,
                        qty=fill_qty,
                        avg_entry=pos_now.avg_entry,
                        dca_level=pos_now.dca_level,
                        mode=self.cfg.mode,
                        margin=float(size.margin) if size else margin,
                    )
                else:
                    self.notify.notify_dca(
                        symbol=symbol,
                        price=fill_price,
                        qty=fill_qty,
                        avg_entry=pos_now.avg_entry,
                        dca_level=pos_now.dca_level,
                        mode=self.cfg.mode,
                        margin=float(size.margin) if size else margin,
                    )

            elif action_type in (
                ActionType.FULL_TP,
                ActionType.TRAIL_EXIT,
                ActionType.PARTIAL_TP,
            ):
                qty = decision.close_qty
                pos_before = self.positions.get(symbol)
                avg_before = float(pos_before.avg_entry) if pos_before else 0.0
                dca_before = int(pos_before.dca_level) if pos_before else None
                if hasattr(self.exchange, "set_mark"):
                    self.exchange.set_mark(symbol, snap.price)
                order = self.exchange.create_market_order(
                    symbol, "sell", qty, params={"clientOrderId": coid}
                )
                fill_price = float(order.get("price") or snap.price)
                fill_qty = float(order.get("amount") or qty or 0.0)
                raw_fee = order.get("fee")
                if isinstance(raw_fee, dict):
                    fill_fee = float(raw_fee.get("cost") or 0.0)
                else:
                    fill_fee = float(raw_fee or 0.0)
                if fill_fee <= 0:
                    fill_fee = fill_qty * fill_price * self.cfg.risk.taker_fee
                pnl = (fill_price - avg_before) * fill_qty - fill_fee if avg_before > 0 else None
                if hasattr(self.exchange, "account"):
                    if symbol in self.exchange.account.positions:
                        self.positions[symbol] = self.exchange.account.positions[symbol]
                        if action_type == ActionType.PARTIAL_TP:
                            self.positions[symbol].partial_taken = True
                    else:
                        self.positions.pop(symbol, None)
                        self.store.delete_position(symbol)
                else:
                    pos = self.positions[symbol]
                    pos.reduce(qty, snap.price, fee=fill_fee)
                    if action_type == ActionType.PARTIAL_TP and pos.is_open:
                        pos.partial_taken = True
                        self.store.save_position(pos)
                    else:
                        self.positions.pop(symbol, None)
                        self.store.delete_position(symbol)
                if symbol in self.positions and self.positions[symbol].is_open:
                    self.store.save_position(self.positions[symbol])
                self.store.record_trade(
                    symbol=symbol,
                    side="sell",
                    action=action_type.value,
                    price=fill_price,
                    qty=fill_qty,
                    fee=fill_fee,
                    avg_entry=avg_before,
                    pnl=pnl,
                    dca_level=dca_before,
                    mode=self.cfg.mode,
                    client_order_id=coid,
                )
                self.store.audit(
                    "fill_sell",
                    symbol=symbol,
                    action=action_type.value,
                    price=fill_price,
                    qty=fill_qty,
                    avg_entry=avg_before,
                    pnl=pnl,
                )
                self.notify.notify_close(
                    symbol=symbol,
                    action=action_type.value,
                    price=fill_price,
                    qty=fill_qty,
                    avg_entry=avg_before,
                    pnl=pnl,
                    mode=self.cfg.mode,
                )
                # After full TP — never permanently block the next entry due to past Max DD
                if action_type in (ActionType.FULL_TP, ActionType.TRAIL_EXIT):
                    if symbol not in self.positions or not self.positions[symbol].is_open:
                        self.risk.resume_after_take_profit(self._equity())
                        self.store.audit("risk_resume_after_tp", equity=self._equity())

            elif action_type == ActionType.MARGIN_TOPUP:
                amount = decision.margin_amount
                self.exchange.add_margin(symbol, amount)
                pos = self.positions.get(symbol)
                if pos and not hasattr(self.exchange, "account"):
                    pos.add_margin(amount)
                elif hasattr(self.exchange, "account") and symbol in self.exchange.account.positions:
                    self.positions[symbol] = self.exchange.account.positions[symbol]
                if symbol in self.positions:
                    self.store.save_position(self.positions[symbol])
                self.store.audit("margin_topup", symbol=symbol, amount=amount)
                self.notify.notify_margin_topup(
                    symbol=symbol, amount=float(amount), mode=self.cfg.mode
                )

            self.store.update_order_status(coid, "filled")
        except Exception as exc:  # noqa: BLE001
            self.store.update_order_status(coid, "error")
            self.store.audit("order_error", symbol=symbol, error=str(exc), coid=coid)
            logger.exception("Order execution failed: %s", exc)
            self.notify.notify_order_error(
                symbol=symbol, error=str(exc), mode=self.cfg.mode
            )

    def process_symbol(self, symbol: str) -> None:
        snap = self.build_snapshot(symbol)
        equity = self._equity()
        self.risk.update_equity(equity)

        blackout, news_reason = self.calendar.is_blackout(snap.timestamp)
        # Count ALL open positions (including current symbol) to enforce the hard cap.
        # This correctly handles the case where another symbol was entered earlier in the
        # same run_once() cycle — self.positions is updated immediately after each fill.
        open_syms = [s for s, p in self.positions.items() if p.is_open and s != symbol]
        # Hard cap: if another coin already has a position — no new entries
        slot = check_max_open_positions(open_syms, self.cfg.risk.max_open_positions)
        corr_ok, corr_reason = slot.allowed, ";".join(slot.reasons)
        if corr_ok and self._close_prices and len(self._close_prices) >= 2:
            rets = returns_from_closes(
                self._close_prices, self.cfg.risk.correlation_lookback
            )
            fr = would_breach_correlation(
                symbol,
                open_syms,
                rets,
                self.cfg.risk.correlation_threshold,
                self.cfg.risk.max_correlated_positions,
            )
            corr_ok, corr_reason = fr.allowed, ";".join(fr.reasons)

        pos = self.positions.get(symbol)
        decision = self.strategy.decide(
            snap,
            pos,
            equity,
            news_ok=not blackout,
            news_reason=news_reason,
            correlation_ok=corr_ok,
            correlation_reason=corr_reason,
        )
        logger.info(
            "%s action=%s reason=%s price=%.4f rsi1=%.1f rsi5=%.1f",
            symbol,
            decision.action.value,
            decision.reason,
            snap.price,
            snap.rsi_1m,
            snap.rsi_5m,
        )
        if decision.action != ActionType.HOLD:
            self._execute(symbol, decision.action, decision, snap)

    def run_once(self) -> None:
        for symbol in self.cfg.symbols:
            try:
                self.process_symbol(symbol)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Error processing %s: %s", symbol, exc)
                self.store.audit("symbol_error", symbol=symbol, error=str(exc))
        # Persist equity once per cycle (after all fills this tick)
        try:
            self._sanitize_and_save_equity(self._equity())
        except Exception as exc:  # noqa: BLE001
            logger.warning("equity save failed: %s", exc)

    def run(self) -> None:
        self._running = True

        def _stop(signum: int, frame: Any) -> None:
            logger.info("Signal %s received — graceful shutdown", signum)
            self._running = False

        signal.signal(signal.SIGINT, _stop)
        try:
            signal.signal(signal.SIGTERM, _stop)
        except Exception:  # noqa: BLE001
            pass  # Windows may not support SIGTERM the same way

        interval = float(self.cfg.loop.get("poll_interval_sec", 5))
        logger.info("Bot starting mode=%s symbols=%s", self.cfg.mode, self.cfg.symbols)
        self.store.audit("bot_start", mode=self.cfg.mode, symbols=self.cfg.symbols)
        if hasattr(self.exchange, "start"):
            try:
                self.exchange.start()
                self.store.audit("ws_feed_start")
            except Exception as exc:  # noqa: BLE001
                logger.warning("WebSocket feed start failed (REST fallback): %s", exc)
                self.store.audit("ws_feed_start_error", error=str(exc))
        try:
            while self._running:
                self.run_once()
                time.sleep(interval)
        finally:
            if hasattr(self.exchange, "stop"):
                try:
                    self.exchange.stop()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("exchange.stop: %s", exc)
            self.store.audit("bot_stop")
            logger.info("Bot stopped cleanly")
