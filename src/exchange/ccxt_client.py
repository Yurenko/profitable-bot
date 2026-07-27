"""CCXT exchange client with optional WebSocket market-data cache.

Public market reads prefer the WS feed (no per-tick REST spam).
Orders / leverage / margin stay on REST — same trading logic as before.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import ccxt
import pandas as pd

from src.config import ExchangeConfig
from src.exchange.ws_feed import WsMarketFeed

logger = logging.getLogger(__name__)


class ExchangeError(RuntimeError):
    pass


def fetch_futures_usdt_snapshot(cfg: ExchangeConfig) -> dict[str, Any]:
    """One-shot REST USDT futures balance (no WebSocket). For dashboard display.

    Uses fapi account endpoint directly to avoid spot /sapi/capital/config calls
    that often fail first on geo-restricted hosts.
    """
    if not (cfg.api_key and cfg.api_secret):
        return {
            "ok": False,
            "error": "Немає EXCHANGE_API_KEY / EXCHANGE_API_SECRET у .env",
            "total": None,
            "free": None,
            "used": None,
            "testnet": bool(cfg.testnet),
        }
    exchange_cls = getattr(ccxt, cfg.id, None)
    if exchange_cls is None:
        return {"ok": False, "error": f"Unknown exchange: {cfg.id}", "testnet": bool(cfg.testnet)}
    ex = None
    try:
        ex = exchange_cls(
            {
                "apiKey": cfg.api_key,
                "secret": cfg.api_secret,
                "enableRateLimit": True,
                "options": {
                    "defaultType": cfg.default_type or "swap",
                    # Skip currency/sapi bootstrap where possible
                    "fetchCurrencies": False,
                    "warnOnFetchOpenOrdersWithoutSymbol": False,
                },
            }
        )
        if cfg.testnet:
            try:
                ex.set_sandbox_mode(True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Sandbox mode: %s", exc)

        # Prefer raw USD-M futures wallet — avoids api.binance.com/sapi/...
        free = used = total_f = 0.0
        try:
            acct = ex.fapiPrivateV2GetAccount()
            assets = acct.get("assets") or []
            for a in assets:
                if str(a.get("asset", "")).upper() == "USDT":
                    # walletBalance ≈ equity; availableBalance ≈ free
                    wb = float(a.get("walletBalance") or 0)
                    ab = float(a.get("availableBalance") or 0)
                    total_f = wb
                    free = ab
                    used = max(0.0, wb - ab)
                    break
            if total_f == 0 and not assets:
                # fallback: older shape
                total_f = float(acct.get("totalWalletBalance") or 0)
                free = float(acct.get("availableBalance") or 0)
                used = max(0.0, total_f - free)
        except AttributeError:
            bal = ex.fetch_balance({"type": "swap"})
            usdt = bal.get("USDT") or {}
            free = float(usdt.get("free") or 0.0)
            used = float(usdt.get("used") or 0.0)
            total = usdt.get("total")
            total_f = float(total) if total is not None else free + used

        return {
            "ok": True,
            "total": total_f,
            "free": free,
            "used": used,
            "testnet": bool(cfg.testnet),
            "exchange": cfg.id,
        }
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        logger.warning("fetch_futures_usdt_snapshot failed: %s", exc)
        if "451" in msg or "restricted location" in msg.lower() or "Eligibility" in msg:
            msg = (
                "Binance блокує цей сервер за геолокацією (часто AWS США). "
                "Перенесіть EC2 у дозволений регіон: eu-central-1, eu-west-1, "
                "ap-southeast-1, ap-northeast-1 — не us-east / us-west."
            )
        return {
            "ok": False,
            "error": msg,
            "total": None,
            "free": None,
            "used": None,
            "testnet": bool(cfg.testnet),
        }
    finally:
        if ex is not None:
            try:
                ex.close()
            except Exception:  # noqa: BLE001
                pass


class CCXTExchange:
    def __init__(
        self,
        cfg: ExchangeConfig,
        max_retries: int = 5,
        *,
        symbols: list[str] | None = None,
        use_ws: bool = True,
        timeframes: list[str] | None = None,
        funding_poll_sec: float = 60.0,
        oi_poll_sec: float = 60.0,
    ) -> None:
        self.cfg = cfg
        self.max_retries = max_retries
        self.symbols = list(symbols or [])
        self.use_ws = bool(use_ws)
        self.client = self._build()
        self._feed: WsMarketFeed | None = None
        if self.use_ws and self.symbols:
            self._feed = WsMarketFeed(
                cfg,
                self.symbols,
                timeframes=timeframes or ["1m", "5m", "15m"],
                funding_poll_sec=funding_poll_sec,
                oi_poll_sec=oi_poll_sec,
            )

    def _build(self) -> ccxt.Exchange:
        exchange_cls = getattr(ccxt, self.cfg.id, None)
        if exchange_cls is None:
            raise ExchangeError(f"Unknown exchange id: {self.cfg.id}")
        params: dict[str, Any] = {
            "apiKey": self.cfg.api_key or None,
            "secret": self.cfg.api_secret or None,
            "enableRateLimit": self.cfg.enable_rate_limit,
            "options": {"defaultType": self.cfg.default_type},
        }
        ex = exchange_cls(params)
        if self.cfg.testnet:
            try:
                ex.set_sandbox_mode(True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Sandbox mode not fully supported: %s", exc)
        return ex

    def start(self) -> None:
        """Start WebSocket market feed (idempotent)."""
        if self._feed is not None:
            logger.info("Starting WebSocket market feed…")
            self._feed.start()

    def stop(self) -> None:
        if self._feed is not None:
            self._feed.stop()

    def reconnect(self) -> None:
        logger.warning("Reconnecting exchange client…")
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass
        self.client = self._build()

    def _call(self, fn_name: str, *args: Any, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                fn = getattr(self.client, fn_name)
                return fn(*args, **kwargs)
            except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.DDoSProtection) as exc:
                last_exc = exc
                sleep_s = min(2 ** attempt, 30)
                logger.warning(
                    "%s failed attempt %d/%d: %s — sleep %ss",
                    fn_name,
                    attempt,
                    self.max_retries,
                    exc,
                    sleep_s,
                )
                time.sleep(sleep_s)
                if attempt >= 3:
                    self.reconnect()
            except ccxt.RateLimitExceeded as exc:
                last_exc = exc
                time.sleep(min(2 ** attempt, 60))
            except ccxt.ExchangeError as exc:
                raise ExchangeError(str(exc)) from exc
        raise ExchangeError(f"{fn_name} failed after retries: {last_exc}")

    def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1m", limit: int = 200, since: int | None = None
    ) -> pd.DataFrame:
        # Historical range downloads always use REST
        if since is None and self._feed is not None:
            cached = self._feed.get_ohlcv(symbol, timeframe, limit)
            if cached is not None and len(cached) > 0:
                return cached
        raw = self._call("fetch_ohlcv", symbol, timeframe=timeframe, limit=limit, since=since)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp")
        return df.astype(float)

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        if self._feed is not None:
            cached = self._feed.get_ticker(symbol)
            if cached is not None:
                return cached
        return self._call("fetch_ticker", symbol)

    def fetch_balance(self) -> dict[str, Any]:
        return self._call("fetch_balance")

    def fetch_available_usdt(self) -> float:
        """Futures wallet available USDT (Дост. on Binance app) — for margin top-up."""
        try:
            acct = self.client.fapiPrivateV2GetAccount()
            top = float(acct.get("availableBalance") or 0)
            if top > 0:
                return top
            for a in acct.get("assets") or []:
                if str(a.get("asset", "")).upper() == "USDT":
                    return float(a.get("availableBalance") or 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("fapiPrivateV2GetAccount availableBalance: %s", exc)
        bal = self.fetch_balance()
        usdt = bal.get("USDT") or {}
        return float(usdt.get("free") or bal.get("free", {}).get("USDT") or 0.0)

    def fetch_free_usdt(self) -> float:
        if self._feed is not None:
            cached = self._feed.get_free_usdt()
            if cached is not None:
                return float(cached)
        return self.fetch_available_usdt()

    def equity(self) -> float:
        """Wallet equity for live: USDT total (free+used) when available, else free."""
        try:
            bal = self.fetch_balance()
            usdt = bal.get("USDT") or {}
            total = usdt.get("total")
            if total is not None:
                return float(total)
            free = float(usdt.get("free") or 0.0)
            used = float(usdt.get("used") or 0.0)
            if free or used:
                return free + used
        except Exception as exc:  # noqa: BLE001
            logger.debug("equity from balance failed: %s", exc)
        return self.fetch_free_usdt()

    def fetch_funding_rate(self, symbol: str) -> float:
        if self._feed is not None:
            cached = self._feed.get_funding_rate(symbol)
            if cached is not None:
                return float(cached)
        try:
            fr = self._call("fetch_funding_rate", symbol)
            return float(fr.get("fundingRate") or 0.0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("funding rate unavailable: %s", exc)
            return 0.0

    def fetch_open_interest(self, symbol: str) -> float:
        if self._feed is not None:
            cached = self._feed.get_open_interest(symbol)
            if cached is not None:
                return float(cached)
        try:
            if self.client.has.get("fetchOpenInterest"):
                oi = self._call("fetch_open_interest", symbol)
                return float(oi.get("openInterestAmount") or oi.get("openInterest") or 0.0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("OI unavailable: %s", exc)
        return 0.0

    def set_leverage(self, symbol: str, leverage: int) -> None:
        try:
            self._call("set_leverage", leverage, symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_leverage: %s", exc)

    def set_margin_mode(self, symbol: str, mode: str = "isolated") -> None:
        try:
            if self.client.has.get("setMarginMode"):
                self._call("set_margin_mode", mode, symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_margin_mode: %s", exc)

    def create_market_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = params or {}
        return self._call("create_order", symbol, "market", side, amount, None, params)

    def create_limit_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = params or {}
        return self._call("create_order", symbol, "limit", side, amount, price, params)

    def cancel_open_orders(self, symbol: str) -> int:
        """Cancel all open orders for symbol (DCA grid). Returns cancelled count."""
        try:
            before = self._call("fetch_open_orders", symbol) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch_open_orders(%s) failed: %s", symbol, exc)
            before = []

        if not before:
            # Still try cancel_all in case fetch failed but orders exist
            try:
                if self.client.has.get("cancelAllOrders"):
                    self._call("cancel_all_orders", symbol)
            except Exception:  # noqa: BLE001
                pass
            return 0

        try:
            if self.client.has.get("cancelAllOrders"):
                self._call("cancel_all_orders", symbol)
                return len(before)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel_all_orders(%s) failed, trying one-by-one: %s", symbol, exc)

        cancelled = 0
        for o in before:
            oid = o.get("id")
            if not oid:
                continue
            try:
                self._call("cancel_order", oid, symbol)
                cancelled += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("cancel_order %s %s: %s", symbol, oid, exc)
        return cancelled

    def add_margin(self, symbol: str, amount: float) -> Any:
        """Add USDT to isolated position margin (lowers liquidation price)."""
        if amount <= 0:
            return None
        amt = round(float(amount), 2)
        if amt <= 0:
            return None
        self.client.load_markets()
        market = self.client.market(symbol)
        errors: list[str] = []

        # 1) CCXT unified
        try:
            if self.client.has.get("addMargin"):
                return self._call("add_margin", symbol, amt)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"add_margin: {exc}")

        # 2) Binance USDM POST /fapi/v1/positionMargin (isolated, one-way BOTH)
        try:
            return self._call(
                "fapiPrivatePostPositionMargin",
                {
                    "symbol": market["id"],
                    "amount": amt,
                    "positionSide": "BOTH",
                    "type": 1,
                },
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"fapi positionMargin: {exc}")
            raise ExchangeError("; ".join(errors)) from exc
