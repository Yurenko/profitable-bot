"""Factory helpers — shared by CLI and dashboard."""
from __future__ import annotations

from src.bot import TradingBot
from src.config import AppConfig
from src.exchange.ccxt_client import CCXTExchange
from src.exchange.paper import PaperExchange
from src.news.calendar import EconomicCalendar
from src.state.store import StateStore


def create_store(cfg: AppConfig) -> StateStore:
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


def create_hybrid_paper(cfg: AppConfig, store: StateStore) -> tuple[object, PaperExchange]:
    capital = float(cfg.backtest.get("initial_capital", 10_000))
    last = store.last_equity(0)
    # Prefer config deposit if stored equity looks like an old default (e.g. 10k vs 100)
    if last <= 0:
        equity = capital
    elif last >= capital * 20 and capital <= 500:
        equity = capital
        store.save_equity(capital)
    else:
        equity = last
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

        def fetch_ticker(self, symbol):
            t = self.data.fetch_ticker(symbol)
            self.paper.set_mark(symbol, float(t.get("last") or t.get("close") or 0))
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

        def create_market_order(self, symbol, side, amount, params=None):
            t = self.data.fetch_ticker(symbol)
            self.paper.set_mark(symbol, float(t.get("last") or t.get("close") or 0))
            return self.paper.create_market_order(symbol, side, amount, params)

        def create_limit_order(self, symbol, side, amount, price, params=None):
            self.paper.set_mark(symbol, float(price))
            if hasattr(self.paper, "create_limit_order"):
                return self.paper.create_limit_order(symbol, side, amount, price, params)
            return self.paper.create_market_order(symbol, side, amount, params)

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
