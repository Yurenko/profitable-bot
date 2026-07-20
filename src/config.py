"""Configuration loading with env-var interpolation."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            return os.environ.get(m.group(1), "")

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


@dataclass
class StrategyConfig:
    leverage: int = 5
    margin_mode: str = "isolated"
    initial_equity_pct: float = 0.01
    entry_margin_usdt: float = 0.0  # if >0, fixed USDT margin for entry (else % of equity)
    max_dca_levels: int = 4
    dca_min_adverse_pct: float = 0.005  # RSI-mode: min adverse before next DCA
    # Grid / Martingale DCA (як на скріні Bybit): фіксований % крок + зростаючий розмір
    dca_mode: str = "grid"  # grid | rsi
    grid_step_pct: float = 0.04  # ~4% між лімітами
    grid_size_multiplier: float = 1.25  # кожен наступний ордер ×1.25
    rsi_period: int = 14
    rsi_1m_entry: float = 30.0
    rsi_5m_entry: float = 35.0
    rsi_15m_dca: float = 30.0
    adx_period: int = 14
    adx_max: float = 38.0
    atr_period: int = 14
    atr_lookback: int = 100
    atr_percentile_max: float = 90.0
    funding_rate_max: float = 0.0005
    oi_change_lookback: int = 12
    oi_rise_threshold_pct: float = 0.03
    tp_min_pct: float = 0.01
    tp_strong_pct: float = 0.02
    rsi_5m_strong_oversold: float = 25.0
    trailing_tp_enabled: bool = True
    trailing_activate_pct: float = 0.008
    trailing_callback_pct: float = 0.004
    partial_close_enabled: bool = True
    partial_close_pct: float = 0.5
    # Long-term: no SL, no trailing exit on pullback; hold + DCA + margin top-up
    long_term_mode: bool = True
    use_stop_loss: bool = False  # never sell because price went down (SL not implemented)
    auto_take_profit: bool = True  # close only when profit target hit (if False — hold until manual)
    dynamic_sizing: bool = True
    atr_sizing_ref_pct: float = 0.005
    min_equity_pct: float = 0.005
    max_equity_pct: float = 0.015


@dataclass
class RiskConfig:
    min_adverse_move_pct: float = 0.12
    margin_topup_buffer_pct: float = 0.03
    max_daily_loss_pct: float = 0.03
    circuit_breaker_drawdown_pct: float = 0.10
    # Max concurrent open positions across all symbols (1 = only one coin at a time)
    max_open_positions: int = 1
    max_correlated_positions: int = 2
    correlation_threshold: float = 0.75
    correlation_lookback: int = 100
    taker_fee: float = 0.0004
    maker_fee: float = 0.0002


@dataclass
class NewsConfig:
    enabled: bool = True
    blacklist_minutes_before: int = 15
    blacklist_minutes_after: int = 15
    impact_levels: list[str] = field(default_factory=lambda: ["high"])
    cache_path: str = "data/economic_calendar.json"


@dataclass
class ExchangeConfig:
    id: str = "binanceusdm"
    testnet: bool = True
    api_key: str = ""
    api_secret: str = ""
    enable_rate_limit: bool = True
    default_type: str = "swap"


@dataclass
class AppConfig:
    mode: str = "paper"
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    symbols: list[str] = field(default_factory=lambda: ["BTC/USDT:USDT"])
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    persistence: dict[str, str] = field(default_factory=dict)
    backtest: dict[str, Any] = field(default_factory=dict)
    logging: dict[str, Any] = field(default_factory=dict)
    loop: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def _from_dict(cls: type, data: dict[str, Any] | None) -> Any:
    if not data:
        return cls()
    fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in data.items() if k in fields})


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    path = Path(path)
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    raw = _interpolate(raw)
    return AppConfig(
        mode=raw.get("mode", "paper"),
        exchange=_from_dict(ExchangeConfig, raw.get("exchange")),
        symbols=list(raw.get("symbols") or ["BTC/USDT:USDT"]),
        strategy=_from_dict(StrategyConfig, raw.get("strategy")),
        risk=_from_dict(RiskConfig, raw.get("risk")),
        news=_from_dict(NewsConfig, raw.get("news")),
        persistence=dict(raw.get("persistence") or {}),
        backtest=dict(raw.get("backtest") or {}),
        logging=dict(raw.get("logging") or {}),
        loop=dict(raw.get("loop") or {}),
        raw=raw,
    )
