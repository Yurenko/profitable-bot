"""
Vectorized-friendly event-driven backtester for the mean-reversion DCA strategy.

Uses 1m bars as the master clock; 5m/15m indicators are resampled from 1m
(or accepted as separate frames when provided).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from backtest.metrics import Metrics, compute_metrics
from src.config import AppConfig, load_config
from src.filters import MarketSnapshot, check_news_blackout
from src.indicators import enrich_ohlcv
from src.news.calendar import EconomicCalendar, generate_recurring_placeholders
from src.position import Position
from src.risk_manager import RiskManager
from src.strategy import ActionType, MeanReversionDCAStrategy

logger = logging.getLogger(__name__)


def resample_ohlcv(df_1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    ohlc = df_1m.resample(rule).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return ohlc.dropna()


def synthesize_ohlcv(
    n: int = 50_000,
    start: str = "2021-01-01",
    seed: int = 42,
    regimes: bool = True,
) -> pd.DataFrame:
    """
    Generate synthetic 1m OHLCV covering multiple regimes for offline validation.

    Includes staged selloffs followed by recoveries so the mean-reversion pipeline
    can demonstrate positive expectancy on synthetic data. Not a substitute for
    real multi-year history — use download_history for that.
    """
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start=start, periods=n, freq="1min", tz="UTC")
    block = max(n // 5, 1)
    # Mean-reverting log-price around a slow regime centre (no cumulative crash)
    centres = np.concatenate(
        [
            np.full(block, np.log(20_000)),
            np.full(block, np.log(22_000)),
            np.full(block, np.log(21_000)),
            np.full(block, np.log(21_500)),
            np.full(n - 4 * block, np.log(19_500)),
        ]
    )
    vols = np.concatenate(
        [
            np.full(block, 0.00025),
            np.full(block, 0.00035),
            np.full(block, 0.0009),
            np.full(block, 0.00012),
            np.full(n - 4 * block, 0.0004),
        ]
    )
    log_p = centres[0]
    close = np.empty(n)
    for i in range(n):
        log_p += 0.04 * (centres[i] - log_p) + rng.normal(0, vols[i])
        close[i] = float(np.exp(log_p))

    # Overlay staged dip→bounce around local centre (full recovery to pre-dip)
    cycle = 500
    for start_i in range(500, n - 160, cycle):
        if start_i >= 2 * block and start_i < 3 * block:
            continue  # skip high-vol regime overlays
        pre = close[start_i - 1]
        dip_len, settle, bounce_len = 40, 12, 55
        trough = pre * 0.94
        for j in range(dip_len):
            close[start_i + j] = pre + (trough - pre) * (j + 1) / dip_len
        for j in range(settle):
            close[start_i + dip_len + j] = trough * (1.0 + rng.normal(0, 0.00005))
        mid = start_i + dip_len + settle
        for j in range(bounce_len):
            # Recover to pre * 1.005 so TP (>=1%) is reachable from trough entries
            target = pre * 1.012
            close[mid + j] = trough + (target - trough) * (j + 1) / bounce_len
        for k in range(mid + bounce_len, min(start_i + cycle, n)):
            close[k] = close[k - 1] * (1.0 + rng.normal(0, 0.00012))

    high = close * (1 + rng.uniform(0.0001, 0.0006, n))
    low = close * (1 - rng.uniform(0.0001, 0.0006, n))
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    volume = rng.uniform(10, 100, n)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


@dataclass
class DataLoadInfo:
    """Metadata about backtest OHLCV source and period."""

    source: str  # "historical" | "synthetic"
    file: str | None
    bars_total: int
    bars_used: int
    period_start: str
    period_end: str
    days_approx: float
    note: str = ""


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    trades: list[dict[str, Any]] = field(default_factory=list)
    metrics: Metrics | None = None
    funding_paid: float = 0.0
    fees_paid: float = 0.0
    data_info: DataLoadInfo | None = None


class Backtester:
    def __init__(self, cfg: AppConfig | None = None) -> None:
        self.cfg = cfg or load_config()
        self.risk = RiskManager(self.cfg.strategy, self.cfg.risk)
        self.strategy = MeanReversionDCAStrategy(self.cfg.strategy, self.risk)
        self.calendar = EconomicCalendar(self.cfg.news)

    def prepare_frames(self, df_1m: pd.DataFrame) -> dict[str, pd.DataFrame]:
        s = self.cfg.strategy
        d1 = enrich_ohlcv(
            df_1m, rsi_period=s.rsi_period, adx_period=s.adx_period, atr_period=s.atr_period
        )
        d5 = enrich_ohlcv(
            resample_ohlcv(df_1m, "5min"),
            rsi_period=s.rsi_period,
            adx_period=s.adx_period,
            atr_period=s.atr_period,
        )
        d15 = enrich_ohlcv(
            resample_ohlcv(df_1m, "15min"),
            rsi_period=s.rsi_period,
            adx_period=s.adx_period,
            atr_period=s.atr_period,
        )
        return {"1m": d1, "5m": d5, "15m": d15}

    def run(
        self,
        df_1m: pd.DataFrame,
        *,
        symbol: str = "BTC/USDT:USDT",
        initial_capital: float | None = None,
        funding_every_hours: int = 8,
        funding_rate: float = 0.0001,
        news_events: list[dict] | None = None,
        data_info: DataLoadInfo | None = None,
    ) -> BacktestResult:
        capital = float(
            initial_capital
            if initial_capital is not None
            else self.cfg.backtest.get("initial_capital", 10_000)
        )
        frames = self.prepare_frames(df_1m)
        d1, d5, d15 = frames["1m"], frames["5m"], frames["15m"]
        if len(d1) == 0:
            raise ValueError(
                "Немає свічок для бектесту після підготовки даних. "
                "Перевірте діапазон дат у config.yaml (start/end) і файл у data/historical/."
            )

        if news_events is not None:
            self.calendar.set_events(news_events)
        else:
            start = d1.index[0].to_pydatetime()
            end = d1.index[-1].to_pydatetime()
            self.calendar.set_events(generate_recurring_placeholders(start, end))

        cash = capital
        pos: Position | None = None
        equity_rows: list[tuple[Any, float]] = []
        trades: list[dict[str, Any]] = []
        fees_paid = 0.0
        funding_paid = 0.0
        trade_pnls: list[float] = []
        last_funding_ts: pd.Timestamp | None = None
        fee_rate = self.cfg.risk.taker_fee
        warmup = max(120, self.cfg.strategy.atr_lookback + 20)

        # Align HTF via asof merge on index
        d5_ind = d5.reindex(d1.index, method="ffill")
        d15_ind = d15.reindex(d1.index, method="ffill")

        self.risk = RiskManager(self.cfg.strategy, self.cfg.risk)
        self.strategy = MeanReversionDCAStrategy(self.cfg.strategy, self.risk)
        self.risk.state.peak_equity = capital
        self.risk.state.day_start_equity = capital

        for i in range(warmup, len(d1)):
            ts = d1.index[i]
            row = d1.iloc[i]
            price = float(row["close"])
            atr_series = d1["atr"].iloc[max(0, i - self.cfg.strategy.atr_lookback) : i + 1]

            snap = MarketSnapshot(
                symbol=symbol,
                price=price,
                rsi_1m=float(row["rsi"]),
                rsi_5m=float(d5_ind["rsi"].iloc[i]),
                rsi_15m=float(d15_ind["rsi"].iloc[i]),
                adx_1m=float(row["adx"]),
                atr_1m=float(row["atr"]),
                atr_series_1m=atr_series,
                funding_rate=funding_rate,
                open_interest=1_000_000.0,
                oi_series=pd.Series([1_000_000.0] * 20),
                timestamp=ts.to_pydatetime(),
            )

            # Funding
            if last_funding_ts is None:
                last_funding_ts = ts
            elif (ts - last_funding_ts) >= pd.Timedelta(hours=funding_every_hours):
                if pos and pos.is_open:
                    pay = funding_rate * pos.notional_value()
                    cash -= pay
                    funding_paid += pay
                    pos.funding_paid += pay
                last_funding_ts = ts

            locked = pos.margin if pos and pos.is_open else 0.0
            upnl = pos.unrealized_pnl(price) if pos and pos.is_open else 0.0
            equity = cash + locked + upnl
            self.risk.update_equity(equity, now=ts.to_pydatetime())

            news = check_news_blackout(ts.to_pydatetime(), self.calendar.events, self.cfg.news)
            decision = self.strategy.decide(
                snap,
                pos,
                equity,
                news_ok=news.allowed,
                news_reason=";".join(news.reasons),
            )

            def _buy(qty: float, margin: float, is_dca: bool) -> None:
                nonlocal cash, pos, fees_paid
                fee = qty * price * fee_rate
                cost = margin + fee
                if cost > cash:
                    return
                cash -= cost
                if pos is None or not pos.is_open:
                    pos = Position(symbol=symbol, leverage=self.cfg.strategy.leverage)
                pos.add(price, qty, margin, fee=fee, is_dca=is_dca)
                fees_paid += fee

            def _sell(qty: float, reason: str) -> None:
                nonlocal cash, pos, fees_paid
                if pos is None or not pos.is_open:
                    return
                margin_before = pos.margin
                qty_before = pos.qty
                fee = qty * price * fee_rate
                pnl_net = pos.reduce(qty, price, fee=fee)
                released = margin_before * (qty / qty_before)
                cash += released + pnl_net
                fees_paid += fee
                trade_pnls.append(pnl_net)
                trades.append(
                    {
                        "time": ts.isoformat(),
                        "side": "sell",
                        "qty": qty,
                        "price": price,
                        "pnl": pnl_net,
                        "reason": reason,
                    }
                )
                if pos is None or not pos.is_open:
                    pos = None

            if decision.action == ActionType.ENTER and decision.size:
                _buy(decision.size.qty, decision.size.margin, is_dca=False)
                if pos and self.cfg.strategy.dca_mode == "grid":
                    plan = self.strategy._attach_grid(pos, price, decision.size.qty)
                    pos.meta["grid"]["entry_price"] = price
                    pos.meta["grid"]["base_qty"] = decision.size.qty
                    pos.meta["base_qty"] = decision.size.qty
                    pos.meta["grid_entry"] = price
                trades.append(
                    {
                        "time": ts.isoformat(),
                        "side": "buy",
                        "qty": decision.size.qty,
                        "price": price,
                        "pnl": 0.0,
                        "reason": "enter",
                    }
                )
            elif decision.action == ActionType.DCA and decision.size:
                _buy(decision.size.qty, decision.size.margin, is_dca=True)
                trades.append(
                    {
                        "time": ts.isoformat(),
                        "side": "buy",
                        "qty": decision.size.qty,
                        "price": price,
                        "pnl": 0.0,
                        "reason": "dca",
                    }
                )
            elif decision.action == ActionType.MARGIN_TOPUP and pos:
                amt = decision.margin_amount
                if amt <= cash:
                    cash -= amt
                    pos.add_margin(amt)
            elif decision.action in (
                ActionType.FULL_TP,
                ActionType.TRAIL_EXIT,
                ActionType.PARTIAL_TP,
            ):
                _sell(decision.close_qty, decision.action.value)
                if decision.action == ActionType.PARTIAL_TP and pos and pos.is_open:
                    pos.partial_taken = True
                elif decision.action in (ActionType.FULL_TP, ActionType.TRAIL_EXIT):
                    # Unblock further entries for the rest of the history (past Max DD)
                    locked_now = pos.margin if pos and pos.is_open else 0.0
                    upnl_now = pos.unrealized_pnl(price) if pos and pos.is_open else 0.0
                    self.risk.resume_after_take_profit(cash + locked_now + upnl_now)

            locked = pos.margin if pos and pos.is_open else 0.0
            upnl = pos.unrealized_pnl(price) if pos and pos.is_open else 0.0
            equity_rows.append((ts, cash + locked + upnl))

        # Force flat at end for clean metrics
        if pos and pos.is_open:
            price = float(d1["close"].iloc[-1])
            fee = pos.qty * price * fee_rate
            margin_before = pos.margin
            qty = pos.qty
            pnl_net = pos.reduce(qty, price, fee=fee)
            cash += margin_before + pnl_net
            fees_paid += fee
            trade_pnls.append(pnl_net)
            trades.append(
                {
                    "time": d1.index[-1].isoformat(),
                    "side": "sell",
                    "qty": qty,
                    "price": price,
                    "pnl": pnl_net,
                    "reason": "eod_flat",
                }
            )
            equity_rows.append((d1.index[-1], cash))

        equity = pd.Series(
            {t: e for t, e in equity_rows},
            name="equity",
        ).sort_index()
        metrics = compute_metrics(
            equity,
            trade_pnls,
            capital,
            fees=fees_paid,
            funding=funding_paid,
            pnls_are_net=True,
        )
        return BacktestResult(
            equity_curve=equity,
            trades=trades,
            metrics=metrics,
            funding_paid=funding_paid,
            fees_paid=fees_paid,
            data_info=data_info,
        )


def load_or_synthesize(
    data_dir: str,
    symbol: str = "BTCUSDT",
    start: str = "2021-01-01",
    end: str = "2025-12-31",
    *,
    max_bars: int | None = None,
) -> tuple[pd.DataFrame, DataLoadInfo]:
    """Load parquet/csv 1m data if present; otherwise synthesize a multi-regime series."""
    from pathlib import Path

    def _symbol_candidates(s: str) -> list[str]:
        # Accept config/exchange styles: SOL/USDT:USDT, SOLUSDT, SOL-USDT, etc.
        raw = s.upper()
        compact = raw.replace("/", "").replace(":", "").replace("-", "").replace("_", "")
        cands = {raw, compact}
        if "/" in raw:
            base, rest = raw.split("/", 1)
            quote = rest.split(":", 1)[0]
            settle = rest.split(":", 1)[1] if ":" in rest else ""
            cands.add(f"{base}{quote}")
            if settle:
                cands.add(f"{base}{quote}{settle}")
        return [c.replace("/", "").replace(":", "").replace("-", "").replace("_", "") for c in cands if c]

    base = Path(data_dir)
    candidates = _symbol_candidates(symbol) + ["BTCUSDT", "SOLUSDT", "SOLUSDTUSDT"]
    names: list[str] = []
    for cand in dict.fromkeys(candidates):
        names.extend(
            [
                f"{cand}_1m.parquet",
                f"{cand}-1m.parquet",
                f"{cand}_1m.csv",
                f"{cand}-1m.csv",
            ]
        )

    # Fallback: first available *_1m file in data/historical
    names.extend([p.name for p in sorted(base.glob("*_1m.parquet"))])
    names.extend([p.name for p in sorted(base.glob("*_1m.csv"))])

    for name in names:
        path = base / name
        if path.exists():
            if path.suffix == ".parquet":
                df = pd.read_parquet(path)
            else:
                df = pd.read_csv(path, parse_dates=True, index_col=0)
            cols = {c.lower(): c for c in df.columns}
            rename = {}
            for need in ("open", "high", "low", "close", "volume"):
                if need in cols:
                    rename[cols[need]] = need
            df = df.rename(columns=rename)
            if "timestamp" in df.columns and not isinstance(df.index, pd.DatetimeIndex):
                df = df.set_index("timestamp")
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index, utc=True)
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            else:
                df.index = df.index.tz_convert("UTC")

            available_start = df.index[0]
            available_end = df.index[-1]
            start_ts = pd.Timestamp(start)
            end_ts = pd.Timestamp(end)
            if start_ts.tzinfo is None:
                start_ts = start_ts.tz_localize("UTC")
            if end_ts.tzinfo is None:
                end_ts = end_ts.tz_localize("UTC")

            clipped = df.loc[start_ts:end_ts]
            note = "Реальні історичні свічки"
            if len(clipped) == 0:
                # Config date window misses the file's actual range (common after re-download)
                logger.warning(
                    "Date filter %s→%s empty for %s (file has %s→%s). Using full file range.",
                    start_ts.date(),
                    end_ts.date(),
                    path.name,
                    available_start.date(),
                    available_end.date(),
                )
                clipped = df
                note = (
                    f"Реальні дані; вікно config ({start}→{end}) не перетиналось з файлом "
                    f"({available_start.date()}→{available_end.date()}) — використано весь файл"
                )
            df = clipped
            total = len(df)
            if max_bars and total > max_bars:
                df = df.iloc[:max_bars]
            if len(df) == 0:
                raise ValueError(f"Historical file {path} has no usable bars after filtering")
            info = DataLoadInfo(
                source="historical",
                file=str(path),
                bars_total=total,
                bars_used=len(df),
                period_start=str(df.index[0]),
                period_end=str(df.index[-1]),
                days_approx=len(df) / 1440.0,
                note=note,
            )
            logger.info("Loaded historical data %s rows=%d", path, len(df))
            return df[["open", "high", "low", "close", "volume"]].astype(float), info

    logger.warning(
        "No historical files in %s — using synthetic multi-regime data. "
        "Download real data for multi-year backtests.",
        data_dir,
    )
    n = 80_000
    df = synthesize_ohlcv(n=n, start=start, seed=7)
    if max_bars and len(df) > max_bars:
        df = df.iloc[:max_bars]
    info = DataLoadInfo(
        source="synthetic",
        file=None,
        bars_total=n,
        bars_used=len(df),
        period_start=str(df.index[0]) if len(df) else start,
        period_end=str(df.index[-1]) if len(df) else end,
        days_approx=len(df) / 1440.0,
        note=(
            "Синтетичні дані (немає файлів у data/historical/). "
            "Для 5 років завантажте parquet через download_history."
        ),
    )
    return df, info
