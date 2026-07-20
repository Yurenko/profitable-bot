"""Background ccxt.pro WebSocket market-data feed with thread-safe cache.

Strategy / bot keep calling sync fetch_* — this layer fills the cache from WS
so the decision loop no longer hammers REST every tick.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

import ccxt
import ccxt.pro as ccxtpro
import pandas as pd

from src.config import ExchangeConfig

logger = logging.getLogger(__name__)


def _ohlcv_to_df(raw: list[list[float]], limit: int) -> pd.DataFrame:
    rows = raw[-limit:] if limit and len(raw) > limit else raw
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp")
    return df.astype(float)


def _merge_candles(existing: list[list[float]], incoming: list[list[float]], keep: int = 500) -> list[list[float]]:
    """Merge by candle open time; last write wins (forming candle updates)."""
    by_ts: dict[int, list[float]] = {}
    for row in existing:
        by_ts[int(row[0])] = row
    for row in incoming:
        by_ts[int(row[0])] = row
    merged = [by_ts[k] for k in sorted(by_ts)]
    return merged[-keep:] if len(merged) > keep else merged


class WsMarketFeed:
    """Daemon-thread asyncio loop: watch OHLCV + tickers; REST for rare funding/OI."""

    def __init__(
        self,
        cfg: ExchangeConfig,
        symbols: list[str],
        timeframes: list[str] | None = None,
        *,
        funding_poll_sec: float = 60.0,
        oi_poll_sec: float = 60.0,
        ohlcv_limit: int = 200,
        ready_timeout_sec: float = 90.0,
    ) -> None:
        self.cfg = cfg
        self.symbols = list(symbols)
        self.timeframes = list(timeframes or ["1m", "5m", "15m"])
        self.funding_poll_sec = float(funding_poll_sec)
        self.oi_poll_sec = float(oi_poll_sec)
        self.ohlcv_limit = int(ohlcv_limit)
        self.ready_timeout_sec = float(ready_timeout_sec)

        self._lock = threading.RLock()
        self._ohlcv: dict[tuple[str, str], list[list[float]]] = {}
        self._tickers: dict[str, dict[str, Any]] = {}
        self._funding: dict[str, float] = {}
        self._oi: dict[str, float] = {}
        self._balance_usdt: float | None = None
        self._last_error: str | None = None
        self._ready = threading.Event()
        self._running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pro: Any = None
        self._rest: ccxt.Exchange | None = None

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not self.symbols:
            logger.warning("WsMarketFeed: no symbols — skip start")
            self._ready.set()
            return
        self._ready.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._thread_main,
            name="ws-market-feed",
            daemon=True,
        )
        self._thread.start()
        ok = self._ready.wait(timeout=self.ready_timeout_sec)
        if not ok:
            logger.warning(
                "WsMarketFeed: seed timeout after %.0fs — REST fallback until cache fills (%s)",
                self.ready_timeout_sec,
                self._last_error,
            )

    def stop(self) -> None:
        self._running = False
        loop = self._loop
        pro = self._pro
        if loop is not None and loop.is_running() and pro is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(self._close_pro(), loop)
                fut.result(timeout=15)
            except Exception as exc:  # noqa: BLE001
                logger.debug("WsMarketFeed close: %s", exc)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=20)
        self._thread = None
        self._loop = None
        self._pro = None
        if self._rest is not None:
            try:
                self._rest.close()
            except Exception:  # noqa: BLE001
                pass
            self._rest = None
        logger.info("WsMarketFeed stopped")

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame | None:
        with self._lock:
            raw = self._ohlcv.get((symbol, timeframe))
            if not raw:
                return None
            copy = [list(r) for r in raw]
        return _ohlcv_to_df(copy, limit)

    def get_ticker(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            t = self._tickers.get(symbol)
            return dict(t) if t else None

    def get_funding_rate(self, symbol: str) -> float | None:
        with self._lock:
            if symbol not in self._funding:
                return None
            return float(self._funding[symbol])

    def get_open_interest(self, symbol: str) -> float | None:
        with self._lock:
            if symbol not in self._oi:
                return None
            return float(self._oi[symbol])

    def get_free_usdt(self) -> float | None:
        with self._lock:
            return self._balance_usdt

    def _build_rest(self) -> ccxt.Exchange:
        cls = getattr(ccxt, self.cfg.id, None)
        if cls is None:
            raise RuntimeError(f"Unknown exchange id: {self.cfg.id}")
        ex = cls(
            {
                "apiKey": self.cfg.api_key or None,
                "secret": self.cfg.api_secret or None,
                "enableRateLimit": True,
                "options": {"defaultType": self.cfg.default_type},
            }
        )
        if self.cfg.testnet:
            try:
                ex.set_sandbox_mode(True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("REST sandbox: %s", exc)
        return ex

    def _build_pro(self) -> Any:
        cls = getattr(ccxtpro, self.cfg.id, None)
        if cls is None:
            raise RuntimeError(f"ccxt.pro has no exchange: {self.cfg.id}")
        ex = cls(
            {
                "apiKey": self.cfg.api_key or None,
                "secret": self.cfg.api_secret or None,
                "enableRateLimit": True,
                "options": {"defaultType": self.cfg.default_type},
            }
        )
        if self.cfg.testnet:
            try:
                ex.set_sandbox_mode(True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("WS sandbox: %s", exc)
        return ex

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._async_main())
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            logger.exception("WsMarketFeed crashed: %s", exc)
            self._ready.set()

    async def _async_main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._rest = self._build_rest()
        self._pro = self._build_pro()
        logger.info(
            "WsMarketFeed starting exchange=%s symbols=%d tfs=%s",
            self.cfg.id,
            len(self.symbols),
            self.timeframes,
        )
        await asyncio.to_thread(self._seed_ohlcv_sync)
        self._ready.set()

        tasks = [
            asyncio.create_task(self._watch_ohlcv(sym, tf), name=f"ohlcv:{sym}:{tf}")
            for sym in self.symbols
            for tf in self.timeframes
        ]
        tasks.append(asyncio.create_task(self._watch_tickers(), name="tickers"))
        tasks.append(asyncio.create_task(self._poll_funding(), name="funding"))
        tasks.append(asyncio.create_task(self._poll_oi(), name="oi"))
        if self.cfg.api_key and self.cfg.api_secret:
            tasks.append(asyncio.create_task(self._watch_balance(), name="balance"))

        try:
            while self._running:
                await asyncio.sleep(0.5)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._close_pro()

    async def _close_pro(self) -> None:
        if self._pro is not None:
            try:
                await self._pro.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("pro.close: %s", exc)

    def _seed_ohlcv_sync(self) -> None:
        assert self._rest is not None
        for sym in self.symbols:
            for tf in self.timeframes:
                if not self._running:
                    return
                try:
                    raw = self._rest.fetch_ohlcv(sym, timeframe=tf, limit=self.ohlcv_limit)
                    with self._lock:
                        self._ohlcv[(sym, tf)] = [list(r) for r in raw]
                    logger.debug("Seeded %s %s (%d bars)", sym, tf, len(raw))
                except Exception as exc:  # noqa: BLE001
                    self._last_error = str(exc)
                    logger.warning("Seed OHLCV %s %s failed: %s", sym, tf, exc)
                time.sleep(0.05)
        # One-shot funding / OI so first tick has values
        for sym in self.symbols:
            try:
                fr = self._rest.fetch_funding_rate(sym)
                rate = float(fr.get("fundingRate") or 0.0)
                with self._lock:
                    self._funding[sym] = rate
            except Exception:  # noqa: BLE001
                pass
            try:
                if self._rest.has.get("fetchOpenInterest"):
                    oi = self._rest.fetch_open_interest(sym)
                    val = float(oi.get("openInterestAmount") or oi.get("openInterest") or 0.0)
                    with self._lock:
                        self._oi[sym] = val
            except Exception:  # noqa: BLE001
                pass

    async def _watch_ohlcv(self, symbol: str, timeframe: str) -> None:
        assert self._pro is not None
        while self._running:
            try:
                candles = await self._pro.watch_ohlcv(
                    symbol, timeframe, limit=self.ohlcv_limit
                )
                if not candles:
                    continue
                with self._lock:
                    key = (symbol, timeframe)
                    prev = self._ohlcv.get(key, [])
                    self._ohlcv[key] = _merge_candles(prev, [list(c) for c in candles])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                logger.warning("watch_ohlcv %s %s: %s", symbol, timeframe, exc)
                await asyncio.sleep(1.0)

    async def _watch_tickers(self) -> None:
        assert self._pro is not None
        use_batch = bool(self._pro.has.get("watchTickers"))
        while self._running:
            try:
                if use_batch:
                    tickers = await self._pro.watch_tickers(self.symbols)
                    with self._lock:
                        for sym, t in tickers.items():
                            self._tickers[sym] = t
                else:
                    for sym in self.symbols:
                        if not self._running:
                            break
                        t = await self._pro.watch_ticker(sym)
                        with self._lock:
                            self._tickers[sym] = t
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                logger.warning("watch_tickers: %s", exc)
                await asyncio.sleep(1.0)

    async def _watch_balance(self) -> None:
        assert self._pro is not None
        if not self._pro.has.get("watchBalance"):
            return
        while self._running:
            try:
                bal = await self._pro.watch_balance()
                usdt = bal.get("USDT") or {}
                free = float(usdt.get("free") or bal.get("free", {}).get("USDT") or 0.0)
                with self._lock:
                    self._balance_usdt = free
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                logger.debug("watch_balance: %s", exc)
                await asyncio.sleep(2.0)

    async def _poll_funding(self) -> None:
        assert self._rest is not None
        while self._running:
            for sym in self.symbols:
                if not self._running:
                    break
                try:
                    fr = await asyncio.to_thread(self._rest.fetch_funding_rate, sym)
                    rate = float(fr.get("fundingRate") or 0.0)
                    with self._lock:
                        self._funding[sym] = rate
                except Exception as exc:  # noqa: BLE001
                    logger.debug("funding poll %s: %s", sym, exc)
            await asyncio.sleep(self.funding_poll_sec)

    async def _poll_oi(self) -> None:
        assert self._rest is not None
        while self._running:
            if not self._rest.has.get("fetchOpenInterest"):
                await asyncio.sleep(self.oi_poll_sec)
                continue
            for sym in self.symbols:
                if not self._running:
                    break
                try:
                    oi = await asyncio.to_thread(self._rest.fetch_open_interest, sym)
                    val = float(
                        oi.get("openInterestAmount") or oi.get("openInterest") or 0.0
                    )
                    with self._lock:
                        self._oi[sym] = val
                except Exception as exc:  # noqa: BLE001
                    logger.debug("OI poll %s: %s", sym, exc)
            await asyncio.sleep(self.oi_poll_sec)
