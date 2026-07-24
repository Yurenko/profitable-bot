"""Background bot / backtest runner for the dashboard."""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.app_factory import create_bot, create_store
from src.config import AppConfig, load_config
from src.config_io import save_config_patch
from src.logging_setup import setup_logging
from src.risk_manager import RiskManager
from src.strategy import MeanReversionDCAStrategy
from src.runtime_env import is_vercel

logger = logging.getLogger(__name__)


@dataclass
class BacktestSnapshot:
    running: bool = False
    error: str | None = None
    metrics: dict[str, Any] | None = None
    equity_points: list[dict[str, Any]] = field(default_factory=list)
    trades_count: int = 0
    closed_trades: list[dict[str, Any]] = field(default_factory=list)
    fees_paid: float = 0.0
    funding_paid: float = 0.0
    finished_at: str | None = None
    data_info: dict[str, Any] | None = None


@dataclass
class HistoryDownloadSnapshot:
    running: bool = False
    error: str | None = None
    message: str | None = None
    symbol: str | None = None
    out_file: str | None = None
    finished_at: str | None = None


class BotManager:
    """Thread-safe controller for paper/live bot and backtests."""

    def __init__(self, config_path: str = "config.yaml") -> None:
        self.config_path = config_path
        self._lock = threading.RLock()
        self._bot = None
        self._thread: threading.Thread | None = None
        self._mode: str | None = None
        self._started_at: str | None = None
        self._last_error: str | None = None
        self._last_tick: str | None = None
        self._tick_count = 0
        self._backtest = BacktestSnapshot()
        self._history_download = HistoryDownloadSnapshot()
        self._stop_event = threading.Event()
        self._exchange_bal_cache: dict[str, Any] | None = None
        self._exchange_bal_ts: float = 0.0

    def get_exchange_balance(self, *, force: bool = False) -> dict[str, Any]:
        """Futures USDT wallet from Binance (cached ~20s to avoid REST spam)."""
        now = time.time()
        if (
            not force
            and self._exchange_bal_cache is not None
            and (now - self._exchange_bal_ts) < 20.0
        ):
            return self._exchange_bal_cache
        cfg = load_config(self.config_path)
        from src.exchange.ccxt_client import fetch_futures_usdt_snapshot

        snap = fetch_futures_usdt_snapshot(cfg.exchange)
        self._exchange_bal_cache = snap
        self._exchange_bal_ts = now
        return snap

    def _persist_bot_intent(self, *, active: bool, mode: str | None) -> None:
        """Persist desired bot state so it can auto-resume after serverless cold start.

        Vercel may stop in-memory background threads between page visits.
        We store an "intent" flag in SQLite so next request can restart the bot.
        """
        try:
            cfg = load_config(self.config_path)
            store = create_store(cfg)
            store.set_kv("bot_active", bool(active))
            if mode is not None:
                store.set_kv("bot_mode", mode)
        except Exception:  # noqa: BLE001
            pass

    def ensure_started(self, *, default_mode: str = "paper") -> None:
        """Auto-start bot if persisted intent says it should be running."""
        if self.is_running():
            return
        try:
            cfg = load_config(self.config_path)
            store = create_store(cfg)
            active = bool(store.get_kv("bot_active", False))
            mode = store.get_kv("bot_mode", default_mode)
            if not active or not mode:
                return
            # start() acquires _lock internally; don't call it while holding _lock here
            self.start(str(mode), mainnet_ok=False)
        except Exception:  # noqa: BLE001
            return

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, mode: str = "paper", *, mainnet_ok: bool = False) -> dict[str, Any]:
        with self._lock:
            if self.is_running():
                return {"ok": False, "error": "Бот вже запущений"}
            cfg = load_config(self.config_path)
            setup_logging(cfg.logging)
            if mode == "live":
                if not cfg.exchange.api_key or not cfg.exchange.api_secret:
                    return {"ok": False, "error": "Потрібні API ключі в .env"}
                if not cfg.exchange.testnet and not mainnet_ok:
                    return {
                        "ok": False,
                        "error": "Mainnet заблоковано. Увімкніть testnet або підтвердіть ризик.",
                    }
            try:
                bot = create_bot(cfg, mode)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)}

            self._bot = bot
            self._mode = mode
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._last_error = None
            self._tick_count = 0
            self._stop_event.clear()
            # Persist "desired state" so we can resume after page/tab is closed.
            self._persist_bot_intent(active=True, mode=mode)

            def _loop() -> None:
                interval = float(cfg.loop.get("poll_interval_sec", 5))
                bot._running = True
                bot.store.audit("dashboard_start", mode=mode)
                logger.info("Dashboard started bot mode=%s", mode)
                if hasattr(bot.exchange, "start"):
                    try:
                        bot.exchange.start()
                        bot.store.audit("ws_feed_start")
                    except Exception as exc:  # noqa: BLE001
                        self._last_error = str(exc)
                        logger.warning("WS feed start failed (REST fallback): %s", exc)
                        bot.store.audit("ws_feed_start_error", error=str(exc))
                try:
                    while bot._running and not self._stop_event.is_set():
                        try:
                            bot.run_once()
                            self._last_tick = datetime.now(timezone.utc).isoformat()
                            self._tick_count += 1
                        except Exception as exc:  # noqa: BLE001
                            self._last_error = str(exc)
                            logger.exception("Bot tick error: %s", exc)
                            bot.store.audit("tick_error", error=str(exc))
                        time.sleep(interval)
                finally:
                    if hasattr(bot.exchange, "stop"):
                        try:
                            bot.exchange.stop()
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("exchange.stop: %s", exc)
                    bot.store.audit("dashboard_stop", mode=mode)
                    logger.info("Dashboard bot stopped")

            self._thread = threading.Thread(target=_loop, name="trading-bot", daemon=True)
            self._thread.start()
            return {"ok": True, "mode": mode}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self.is_running() or self._bot is None:
                return {"ok": True, "message": "Бот не запущений"}
            # Persist intent before stopping the in-memory thread.
            try:
                self._persist_bot_intent(active=False, mode=self._mode)
            except Exception:  # noqa: BLE001
                pass
            self._bot._running = False
            self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=30)
        with self._lock:
            self._bot = None
            self._thread = None
            self._mode = None
        return {"ok": True}

    def tick_once(self, mode: str = "paper") -> dict[str, Any]:
        cfg = load_config(self.config_path)
        bot = create_bot(cfg, mode)
        bot.run_once()
        self._last_tick = datetime.now(timezone.utc).isoformat()
        return {"ok": True}

    def apply_config(self, patch: dict[str, Any]) -> AppConfig:
        cfg = save_config_patch(self.config_path, patch)
        with self._lock:
            if self._bot is not None:
                self._bot.cfg = cfg
                self._bot.risk = RiskManager(cfg.strategy, cfg.risk)
                self._bot.strategy = MeanReversionDCAStrategy(cfg.strategy, self._bot.risk)
                # Drop positions for symbols removed from config (memory + SQLite)
                active = set(cfg.symbols)
                for sym in list(self._bot.positions.keys()):
                    if sym not in active:
                        logger.info(
                            "Config change: dropping position for %s (not in symbols)", sym
                        )
                        self._bot.store.audit("position_dropped_config_change", symbol=sym)
                        self._bot.store.delete_position(sym)
                        self._bot.positions.pop(sym, None)
        return cfg

    def reset_equity_to_deposit(self) -> dict[str, Any]:
        """Reset paper/sqlite equity to config backtest.initial_capital (e.g. $100)."""
        if self.is_running():
            return {"ok": False, "error": "Спочатку зупиніть бота"}
        cfg = load_config(self.config_path)
        capital = float(cfg.backtest.get("initial_capital", 100))
        store = create_store(cfg)
        store.clear_equity_history()
        store.save_equity(capital)
        store.set_kv("last_equity", capital)
        store.audit("equity_reset", equity=capital)
        return {"ok": True, "equity": capital}

    def list_history_files(self) -> list[dict[str, Any]]:
        cfg = load_config(self.config_path)
        base = Path(cfg.backtest.get("data_dir", "data/historical"))
        if not base.exists():
            return []
        files: list[dict[str, Any]] = []
        for path in sorted(base.glob("*_1m.parquet")) + sorted(base.glob("*_1m.csv")):
            rows: int | None = None
            try:
                if path.suffix == ".parquet":
                    import pyarrow.parquet as pq

                    rows = pq.read_metadata(path).num_rows
                else:
                    with path.open(encoding="utf-8") as f:
                        rows = sum(1 for _ in f) - 1
            except Exception:  # noqa: BLE001
                rows = None
            files.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "rows": rows,
                    "days_approx": round(rows / 1440.0, 1) if rows else None,
                    "size_mb": round(path.stat().st_size / (1024 * 1024), 2),
                }
            )
        return files

    def run_backtest(self, *, bars: int | None = 0, monte_carlo: bool = True) -> None:
        with self._lock:
            if self._backtest.running:
                raise RuntimeError("Backtest already running")
            self._backtest = BacktestSnapshot(running=True)

        def _job() -> None:
            try:
                from backtest.backtest import Backtester, DataLoadInfo, load_or_synthesize
                from backtest.monte_carlo import run_monte_carlo

                cfg = load_config(self.config_path)
                setup_logging(cfg.logging)
                primary_symbol = (cfg.symbols[0] if cfg.symbols else "BTC/USDT:USDT")
                max_bars = bars if bars and bars > 0 else None
                df, data_info = load_or_synthesize(
                    cfg.backtest.get("data_dir", "data/historical"),
                    symbol=primary_symbol,
                    start=cfg.backtest.get("start", "2021-01-01"),
                    end=cfg.backtest.get("end", "2025-12-31"),
                    max_bars=max_bars,
                )
                if data_info.bars_used != (bars or data_info.bars_used):
                    data_info = DataLoadInfo(
                        source=data_info.source,
                        file=data_info.file,
                        bars_total=data_info.bars_total,
                        bars_used=len(df),
                        period_start=str(df.index[0]) if len(df) else data_info.period_start,
                        period_end=str(df.index[-1]) if len(df) else data_info.period_end,
                        days_approx=len(df) / 1440.0,
                        note=data_info.note,
                    )
                result = Backtester(cfg).run(
                    df,
                    initial_capital=float(cfg.backtest.get("initial_capital", 10_000)),
                    data_info=data_info,
                )
                m = result.metrics
                mc_data = None
                if monte_carlo and m:
                    mc = run_monte_carlo(
                        result.equity_curve,
                        float(cfg.backtest.get("initial_capital", 10_000)),
                        n_simulations=200,
                        block_size=20,
                    )
                    mc_data = {
                        "p5": mc.final_equity_p5,
                        "p50": mc.final_equity_p50,
                        "p95": mc.final_equity_p95,
                        "prob_ruin": mc.prob_ruin,
                    }
                points = [
                    {"t": str(idx), "v": float(val)}
                    for idx, val in result.equity_curve.iloc[:: max(1, len(result.equity_curve) // 300)].items()
                ]
                out_path = Path("data/backtest_equity.csv")
                out_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    result.equity_curve.to_csv(out_path, header=["equity"])
                except OSError:
                    # Windows can occasionally fail on relative path writes
                    # when process cwd is unexpected; retry with absolute path.
                    abs_out = (Path(__file__).resolve().parent.parent / "data" / "backtest_equity.csv")
                    abs_out.parent.mkdir(parents=True, exist_ok=True)
                    result.equity_curve.to_csv(abs_out, header=["equity"])
                di = result.data_info
                sells = [t for t in result.trades if t.get("side") == "sell"]
                wins = sum(1 for t in sells if float(t.get("pnl", 0)) > 0)
                losses = sum(1 for t in sells if float(t.get("pnl", 0)) <= 0)
                metrics_out = {**(m.to_dict() if m else {}), "monte_carlo": mc_data}
                metrics_out["wins"] = wins
                metrics_out["losses"] = losses
                metrics_out["closed_trades"] = len(sells)
                with self._lock:
                    self._backtest = BacktestSnapshot(
                        running=False,
                        metrics=metrics_out,
                        equity_points=points,
                        trades_count=len(result.trades),
                        closed_trades=sells[-80:],  # last closes for UI
                        fees_paid=result.fees_paid,
                        funding_paid=result.funding_paid,
                        finished_at=datetime.now(timezone.utc).isoformat(),
                        data_info={
                            "source": di.source,
                            "file": di.file,
                            "bars_used": di.bars_used,
                            "period_start": di.period_start,
                            "period_end": di.period_end,
                            "days_approx": round(di.days_approx, 1),
                            "note": di.note,
                        }
                        if di
                        else None,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Backtest failed")
                with self._lock:
                    self._backtest = BacktestSnapshot(
                        running=False,
                        error=str(exc),
                        finished_at=datetime.now(timezone.utc).isoformat(),
                    )

        threading.Thread(target=_job, name="backtest", daemon=True).start()

    def get_backtest(self) -> BacktestSnapshot:
        with self._lock:
            return self._backtest

    def start_history_download(
        self,
        *,
        symbol: str = "SOL/USDT:USDT",
        since: str = "2021-01-01T00:00:00+00:00",
        timeframe: str = "1m",
    ) -> None:
        with self._lock:
            if self._history_download.running:
                raise RuntimeError("Завантаження вже виконується")
            # SOL/USDT:USDT → SOLUSDT (not SOLUSDTUSDT)
            parts = symbol.upper().replace("-", "/").split("/")
            base = parts[0]
            quote = (parts[1].split(":")[0] if len(parts) > 1 else "USDT")
            safe = f"{base}{quote}"
            out = f"data/historical/{safe}_{timeframe}.parquet"
            self._history_download = HistoryDownloadSnapshot(
                running=True,
                message="Завантаження історії з mainnet (публічні OHLCV)…",
                symbol=symbol,
                out_file=out,
            )

        def _job() -> None:
            try:
                from backtest.download_history import download

                cfg = load_config(self.config_path)
                # Always mainnet for history — testnet has very short candle history
                download(
                    exchange_id=cfg.exchange.id,
                    symbol=symbol,
                    timeframe=timeframe,
                    since=since,
                    until=None,
                    out=Path(out),
                    sandbox=False,
                )
                with self._lock:
                    self._history_download = HistoryDownloadSnapshot(
                        running=False,
                        message="Історію успішно завантажено",
                        symbol=symbol,
                        out_file=out,
                        finished_at=datetime.now(timezone.utc).isoformat(),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("History download failed")
                with self._lock:
                    self._history_download = HistoryDownloadSnapshot(
                        running=False,
                        error=str(exc),
                        symbol=symbol,
                        out_file=out,
                        finished_at=datetime.now(timezone.utc).isoformat(),
                    )

        threading.Thread(target=_job, name="history-download", daemon=True).start()

    def get_status(self) -> dict[str, Any]:
        # Vercel may suspend in-memory background threads between page visits.
        # Auto-resume from persisted intent so UI doesn't show "stopped" after reopen.
        self.ensure_started(default_mode="paper")
        cfg = load_config(self.config_path)
        store = create_store(cfg)
        from src.app_factory import _sane_equity

        capital = float(cfg.backtest.get("initial_capital", 10_000))
        raw_equity = store.last_equity(capital)
        paper_equity = _sane_equity(raw_equity, capital)
        if abs(paper_equity - raw_equity) > 1e-6 and self._mode != "live":
            store.clear_equity_history()
            store.save_equity(paper_equity)
            store.audit("equity_sanitized", previous=raw_equity, equity=paper_equity)

        # Always try to show real futures wallet (even in paper / stopped)
        exchange_bal = self.get_exchange_balance()
        exchange_total = exchange_bal.get("total") if exchange_bal.get("ok") else None

        # Display + sizing source:
        # - live running → Binance futures equity
        # - otherwise → paper equity from SQLite / initial_capital
        is_live = bool(self.is_running() and self._mode == "live")
        if is_live and exchange_total is not None:
            equity = float(exchange_total)
        elif is_live and self._bot is not None:
            try:
                equity = float(self._bot._equity())
            except Exception:  # noqa: BLE001
                equity = paper_equity
        else:
            equity = paper_equity

        active_symbols = set(cfg.symbols)
        positions = store.load_positions()
        pos_list = []
        for sym, p in positions.items():
            if p.is_open and sym in active_symbols:
                pos_list.append(
                    {
                        "symbol": sym,
                        "qty": p.qty,
                        "avg_entry": p.avg_entry,
                        "margin": p.margin,
                        "dca_level": p.dca_level,
                        "fees_paid": p.fees_paid,
                    }
                )
        live_curve = store.equity_history(limit=300)
        with self._lock:
            bt = self._backtest
            return {
                "bot_running": self.is_running(),
                "bot_mode": self._mode,
                "started_at": self._started_at,
                "last_tick": self._last_tick,
                "tick_count": self._tick_count,
                "last_error": self._last_error,
                "equity": equity,
                "paper_equity": paper_equity,
                "initial_capital": capital,
                "equity_points": live_curve,
                "exchange_balance": exchange_bal,
                "positions": pos_list,
                "symbols": cfg.symbols,
                "testnet": cfg.exchange.testnet,
                "backtest": {
                    "running": bt.running,
                    "error": bt.error,
                    "metrics": bt.metrics,
                    "equity_points": bt.equity_points,
                    "trades_count": bt.trades_count,
                    "closed_trades": bt.closed_trades,
                    "fees_paid": bt.fees_paid,
                    "funding_paid": bt.funding_paid,
                    "finished_at": bt.finished_at,
                    "data_info": bt.data_info,
                },
                "history_download": self._history_download.__dict__.copy(),
                "history_files": self.list_history_files(),
            }

    def read_audit(self, limit: int = 80) -> list[dict[str, Any]]:
        cfg = load_config(self.config_path)
        audit_log_path = cfg.persistence.get("audit_log_path", "logs/audit.jsonl")
        if is_vercel():
            audit_log_path = "/tmp/audit.jsonl"
        path = Path(audit_log_path)
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return list(reversed(out))

    def list_trades(self, limit: int = 100) -> list[dict[str, Any]]:
        cfg = load_config(self.config_path)
        store = create_store(cfg)
        return store.list_trades(limit=limit)

    def read_logs(self, limit: int = 60) -> list[str]:
        cfg = load_config(self.config_path)
        log_path = cfg.logging.get("file", "logs/bot.log")
        if is_vercel():
            # Keep only the filename, Vercel writes to /tmp/<name>.
            log_path = "/tmp/" + Path(log_path).name
        path = Path(log_path)
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-limit:]
