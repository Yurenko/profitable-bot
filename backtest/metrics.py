"""Performance metrics for backtests."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
import pandas as pd


@dataclass
class Metrics:
    net_profit: float
    net_profit_pct: float
    max_drawdown: float
    max_drawdown_pct: float
    sharpe: float
    win_rate: float
    profit_factor: float
    expectancy: float
    n_trades: int
    avg_trade: float
    fees_paid: float
    funding_paid: float

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def max_drawdown(equity: pd.Series) -> tuple[float, float]:
    peak = equity.cummax()
    dd = equity - peak
    dd_pct = dd / peak.replace(0, np.nan)
    return float(dd.min()), float(dd_pct.min())


def sharpe_ratio(returns: pd.Series, periods_per_year: float = 365 * 24 * 60) -> float:
    """Annualized Sharpe from per-bar returns (default assumes 1m bars)."""
    r = returns.dropna()
    if len(r) < 2 or r.std() == 0:
        return 0.0
    return float(np.sqrt(periods_per_year) * r.mean() / r.std())


def trade_metrics(pnls: Sequence[float], fees: float = 0.0, funding: float = 0.0) -> Metrics:
    arr = np.asarray(list(pnls), dtype=float)
    n = len(arr)
    if n == 0:
        return Metrics(
            net_profit=-fees - funding,
            net_profit_pct=0.0,
            max_drawdown=0.0,
            max_drawdown_pct=0.0,
            sharpe=0.0,
            win_rate=0.0,
            profit_factor=0.0,
            expectancy=0.0,
            n_trades=0,
            avg_trade=0.0,
            fees_paid=fees,
            funding_paid=funding,
        )
    wins = arr[arr > 0]
    losses = arr[arr <= 0]
    gross_profit = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0
    pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    net = float(arr.sum()) - fees - funding  # if pnls already net of fees, pass fees=0
    return Metrics(
        net_profit=net,
        net_profit_pct=0.0,
        max_drawdown=0.0,
        max_drawdown_pct=0.0,
        sharpe=0.0,
        win_rate=float(len(wins) / n),
        profit_factor=float(pf) if np.isfinite(pf) else 999.0,
        expectancy=float(arr.mean()),
        n_trades=n,
        avg_trade=float(arr.mean()),
        fees_paid=fees,
        funding_paid=funding,
    )


def compute_metrics(
    equity: pd.Series,
    trade_pnls: Sequence[float],
    initial_capital: float,
    fees: float = 0.0,
    funding: float = 0.0,
    *,
    pnls_are_net: bool = True,
    periods_per_year: float = 365 * 24 * 60,
) -> Metrics:
    rets = equity.pct_change().fillna(0.0)
    dd, dd_pct = max_drawdown(equity)
    fee_adj = 0.0 if pnls_are_net else fees
    fund_adj = 0.0 if pnls_are_net else funding
    base = trade_metrics(trade_pnls, fees=fee_adj, funding=fund_adj)
    net = float(equity.iloc[-1] - initial_capital) if len(equity) else base.net_profit
    return Metrics(
        net_profit=net,
        net_profit_pct=net / initial_capital if initial_capital else 0.0,
        max_drawdown=abs(dd),
        max_drawdown_pct=abs(dd_pct),
        sharpe=sharpe_ratio(rets, periods_per_year),
        win_rate=base.win_rate,
        profit_factor=base.profit_factor,
        expectancy=base.expectancy,
        n_trades=base.n_trades,
        avg_trade=base.avg_trade,
        fees_paid=fees,
        funding_paid=funding,
    )
