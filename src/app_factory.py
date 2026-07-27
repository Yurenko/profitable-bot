"""Factory helpers — shared by CLI and dashboard."""
from __future__ import annotations

from src.bot import TradingBot
from src.config import AppConfig
from src.exchange.ccxt_client import CCXTExchange
from src.exchange.paper import PaperExchange
from src.news.calendar import EconomicCalendar
from src.state.store import StateStore
from src.runtime_env import is_vercel


def create_store(cfg: AppConfig) -> StateStore:
    # Vercel filesystem is read-only except /tmp.
    if is_vercel():
        return StateStore("/tmp/bot_state.sqlite", "/tmp/audit.jsonl")

    return StateStore(
        cfg.persistence.get("db_path", "data/bot_state.sqlite"),
        cfg.persistence.get("audit_log_path", "logs/audit.jsonl"),
    )


def _ws_kwargs(cfg: AppConfig) -> dict:
    loop = cfg.loop or {}
    return {
        "symbols": list(cfg.symbols),
        "use_ws": bool(loop.get("use_websocket", True)),
        "funding_poll_sec": float(loop.get("funding_poll_sec", 60)),
        "oi_poll_sec": float(loop.get("oi_poll_sec", 60)),
    }


def _sane_equity(value: float, capital: float) -> float:
    """Reject NaN/Inf and absurd equity vs configured deposit (corruption guard)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return capital
    if v != v or v in (float("inf"), float("-inf")):  # NaN / Inf
        return capital
    if v <= 0:
        return capital
    # More than 50× deposit is almost certainly a paper bug / bad restore
    if capital > 0 and v > max(capital * 50, capital + 1_000_000):
        return capital
    return v


def create_hybrid_paper(cfg: AppConfig, store: StateStore) -> tuple[object, PaperExchange]:
    capital = float(cfg.backtest.get("initial_capital", 10_000))
    last = store.last_equity(0)
    equity = _sane_equity(last, capital) if last > 0 else capital
    if last > 0 and equity == capital and abs(last - capital) > 1e-6:
        # Stored equity was corrupt — reset to deposit
        store.save_equity(capital)
        store.audit("equity_sanitized", previous=last, equity=capital)
    paper = PaperExchange(
        initial_equity=equity,
        taker_fee=cfg.risk.taker_fee,
        leverage=cfg.strategy.leverage,
    )
    data = CCXTExchange(cfg.exchange, **_ws_kwargs(cfg))

    class HybridPaper:
        def __init__(self) -> None:
            self.data = data
            self.paper = paper

        def start(self) -> None:
            self.data.start()

        def stop(self) -> None:
            self.data.stop()

        def fetch_ohlcv(self, *a, **k):
            return self.data.fetch_ohlcv(*a, **k)

        def _safe_mark(self, symbol: str, preferred: float | None = None) -> float:
            if preferred is not None and preferred > 0:
                return float(preferred)
            existing = self.paper.marks.get(symbol)
            if existing is not None and existing > 0:
                return float(existing)
            t = self.data.fetch_ticker(symbol)
            px = float(t.get("last") or t.get("close") or 0)
            if px <= 0:
                raise RuntimeError(f"Invalid mark price for {symbol}: {px}")
            return px

        def fetch_ticker(self, symbol):
            t = self.data.fetch_ticker(symbol)
            px = float(t.get("last") or t.get("close") or 0)
            if px > 0:
                self.paper.set_mark(symbol, px)
            return t

        def fetch_funding_rate(self, symbol):
            return self.data.fetch_funding_rate(symbol)

        def fetch_open_interest(self, symbol):
            return self.data.fetch_open_interest(symbol)

        def fetch_free_usdt(self):
            return self.paper.fetch_free_usdt()

        def equity(self):
            return self.paper.equity()

        def set_leverage(self, symbol, leverage):
            return self.paper.set_leverage(symbol, leverage)

        def set_margin_mode(self, symbol, mode="isolated"):
            return self.paper.set_margin_mode(symbol, mode)

        def set_mark(self, symbol, price):
            if price and float(price) > 0:
                self.paper.set_mark(symbol, float(price))

        def create_market_order(self, symbol, side, amount, params=None):
            # Prefer mark already set by bot from strategy snapshot — never overwrite with 0
            existing = self.paper.marks.get(symbol)
            if existing is None or existing <= 0:
                self.paper.set_mark(symbol, self._safe_mark(symbol))
            return self.paper.create_market_order(symbol, side, amount, params)

        def create_limit_order(self, symbol, side, amount, price, params=None):
            # Paper: do NOT fill at limit price as market — that invented fake PnL.
            # Grid DCA is filled by strategy when market price actually hits the level.
            params = params or {}
            return {
                "id": params.get("clientOrderId") or f"paper_limit_{symbol}",
                "clientOrderId": params.get("clientOrderId"),
                "symbol": symbol,
                "side": side,
                "amount": amount,
                "price": float(price),
                "type": "limit",
                "status": "open",
                "info": {"paper": True, "note": "pending_until_price_hit"},
            }

        def cancel_open_orders(self, symbol):
            # Paper limits are not resting on an exchange
            return 0

        def add_margin(self, symbol, amount):
            return self.paper.add_margin(symbol, amount)

        @property
        def account(self):
            return self.paper.account

    return HybridPaper(), paper


def create_bot(cfg: AppConfig, mode: str) -> TradingBot:
    cfg.mode = mode
    store = create_store(cfg)
    calendar = EconomicCalendar(cfg.news)
    if mode == "paper":
        exchange, _ = create_hybrid_paper(cfg, store)
        return TradingBot(cfg, exchange, store, calendar)
    exchange = CCXTExchange(cfg.exchange, **_ws_kwargs(cfg))
    return TradingBot(cfg, exchange, store, calendar)
