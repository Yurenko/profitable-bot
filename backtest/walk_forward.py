"""Walk-forward optimization over a small parameter grid."""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd

from backtest.backtest import Backtester
from backtest.metrics import Metrics
from src.config import AppConfig, load_config

logger = logging.getLogger(__name__)


@dataclass
class WalkForwardFold:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    best_params: dict[str, Any]
    train_metrics: Metrics
    test_metrics: Metrics


DEFAULT_GRID: dict[str, list[Any]] = {
    "rsi_1m_entry": [25, 30],
    "rsi_5m_entry": [30, 35],
    "adx_max": [35, 40],
    "tp_min_pct": [0.008, 0.01],
}


def apply_params(cfg: AppConfig, params: dict[str, Any]) -> AppConfig:
    for k, v in params.items():
        if hasattr(cfg.strategy, k):
            setattr(cfg.strategy, k, v)
    return cfg


def score(metrics: Metrics) -> float:
    """Prefer positive expectancy with controlled drawdown."""
    if metrics.n_trades < 3:
        return -1e9
    return (
        metrics.net_profit_pct * 100
        + metrics.sharpe
        - metrics.max_drawdown_pct * 50
        + metrics.profit_factor
    )


def walk_forward(
    df_1m: pd.DataFrame,
    cfg: AppConfig | None = None,
    grid: dict[str, list[Any]] | None = None,
    train_days: int = 30,
    test_days: int = 10,
    symbol: str = "BTC/USDT:USDT",
) -> list[WalkForwardFold]:
    """
    Rolling walk-forward. Default day counts are modest for synthetic/CI data;
    for multi-year runs use config backtest.walk_forward train_days=180 test_days=60.
    """
    cfg = cfg or load_config()
    grid = grid or DEFAULT_GRID
    keys = list(grid.keys())
    combos = list(itertools.product(*[grid[k] for k in keys]))

    start = df_1m.index[0]
    end = df_1m.index[-1]
    folds: list[WalkForwardFold] = []
    cursor = start

    while True:
        train_end = cursor + pd.Timedelta(days=train_days)
        test_end = train_end + pd.Timedelta(days=test_days)
        if test_end > end:
            break
        train_df = df_1m.loc[cursor:train_end]
        test_df = df_1m.loc[train_end:test_end]
        if len(train_df) < 1000 or len(test_df) < 300:
            cursor += pd.Timedelta(days=test_days)
            continue

        best_score = -1e18
        best_params: dict[str, Any] = {}
        best_train: Metrics | None = None

        for combo in combos:
            params = dict(zip(keys, combo))
            local = load_config() if cfg is None else AppConfig(
                mode=cfg.mode,
                exchange=cfg.exchange,
                symbols=cfg.symbols,
                strategy=type(cfg.strategy)(**{**cfg.strategy.__dict__}),
                risk=cfg.risk,
                news=cfg.news,
                persistence=cfg.persistence,
                backtest=cfg.backtest,
                logging=cfg.logging,
                loop=cfg.loop,
                raw=cfg.raw,
            )
            apply_params(local, params)
            bt = Backtester(local)
            res = bt.run(train_df, symbol=symbol)
            assert res.metrics is not None
            sc = score(res.metrics)
            if sc > best_score:
                best_score = sc
                best_params = params
                best_train = res.metrics

        assert best_train is not None
        test_cfg = load_config()
        # copy strategy from original then apply
        if cfg:
            test_cfg.strategy = type(cfg.strategy)(**{**cfg.strategy.__dict__})
            test_cfg.risk = cfg.risk
            test_cfg.news = cfg.news
        apply_params(test_cfg, best_params)
        test_res = Backtester(test_cfg).run(test_df, symbol=symbol)
        assert test_res.metrics is not None
        folds.append(
            WalkForwardFold(
                train_start=cursor,
                train_end=train_end,
                test_start=train_end,
                test_end=test_end,
                best_params=best_params,
                train_metrics=best_train,
                test_metrics=test_res.metrics,
            )
        )
        logger.info(
            "WF fold test net=%.2f%% params=%s",
            test_res.metrics.net_profit_pct * 100,
            best_params,
        )
        cursor += pd.Timedelta(days=test_days)

    return folds
