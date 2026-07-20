"""Monte Carlo robustness: shuffle trade PnLs / block-bootstrap equity returns."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class MonteCarloResult:
    n: int
    final_equity_p5: float
    final_equity_p50: float
    final_equity_p95: float
    max_dd_p95: float
    prob_ruin: float  # P(final < 0.5 * initial)
    samples_final: np.ndarray
    samples_max_dd: np.ndarray


def block_bootstrap(
    returns: np.ndarray, block_size: int, n_blocks: int, rng: np.random.Generator
) -> np.ndarray:
    n = len(returns)
    if n == 0:
        return returns.copy()
    block_size = max(1, min(block_size, n))
    starts = rng.integers(0, n, size=n_blocks)
    chunks = []
    for s in starts:
        chunk = returns[s : s + block_size]
        if len(chunk) < block_size:
            chunk = np.concatenate([chunk, returns[: block_size - len(chunk)]])
        chunks.append(chunk)
    return np.concatenate(chunks)[:n]


def run_monte_carlo(
    equity: pd.Series,
    initial_capital: float,
    n_simulations: int = 500,
    block_size: int = 20,
    ruin_fraction: float = 0.5,
    seed: int = 42,
) -> MonteCarloResult:
    rets = equity.pct_change().fillna(0.0).to_numpy()
    rng = np.random.default_rng(seed)
    finals = np.zeros(n_simulations)
    max_dds = np.zeros(n_simulations)
    n_blocks = max(1, int(np.ceil(len(rets) / max(block_size, 1))))

    for i in range(n_simulations):
        boot = block_bootstrap(rets, block_size, n_blocks, rng)
        path = initial_capital * np.cumprod(1.0 + boot)
        finals[i] = path[-1] if len(path) else initial_capital
        peak = np.maximum.accumulate(path)
        dd = (path - peak) / np.where(peak == 0, 1, peak)
        max_dds[i] = float(dd.min()) if len(dd) else 0.0

    return MonteCarloResult(
        n=n_simulations,
        final_equity_p5=float(np.percentile(finals, 5)),
        final_equity_p50=float(np.percentile(finals, 50)),
        final_equity_p95=float(np.percentile(finals, 95)),
        max_dd_p95=float(np.percentile(max_dds, 5)),  # 5th pct = worse DD (more negative)
        prob_ruin=float(np.mean(finals < initial_capital * ruin_fraction)),
        samples_final=finals,
        samples_max_dd=max_dds,
    )


def shuffle_trades_monte_carlo(
    trade_pnls: list[float],
    initial_capital: float,
    n_simulations: int = 500,
    seed: int = 42,
) -> MonteCarloResult:
    """Order-invariant stress: randomly permute trade sequence."""
    rng = np.random.default_rng(seed)
    arr = np.asarray(trade_pnls, dtype=float)
    finals = np.zeros(n_simulations)
    max_dds = np.zeros(n_simulations)
    for i in range(n_simulations):
        seq = rng.permutation(arr)
        eq = initial_capital + np.cumsum(seq)
        finals[i] = eq[-1] if len(eq) else initial_capital
        peak = np.maximum.accumulate(np.concatenate([[initial_capital], eq]))
        path = np.concatenate([[initial_capital], eq])
        dd = (path - peak) / np.where(peak == 0, 1, peak)
        max_dds[i] = float(dd.min())
    return MonteCarloResult(
        n=n_simulations,
        final_equity_p5=float(np.percentile(finals, 5)),
        final_equity_p50=float(np.percentile(finals, 50)),
        final_equity_p95=float(np.percentile(finals, 95)),
        max_dd_p95=float(np.percentile(max_dds, 5)),
        prob_ruin=float(np.mean(finals < initial_capital * 0.5)),
        samples_final=finals,
        samples_max_dd=max_dds,
    )
