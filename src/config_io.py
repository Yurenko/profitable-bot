"""Save / merge configuration to YAML."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from src.config import AppConfig, load_config


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def config_to_api_dict(cfg: AppConfig) -> dict[str, Any]:
    """Serializable config for dashboard (no secrets in full form — mask keys)."""
    return {
        "mode": cfg.mode,
        "symbols": cfg.symbols,
        "exchange": {
            "id": cfg.exchange.id,
            "testnet": cfg.exchange.testnet,
            "api_key_set": bool(cfg.exchange.api_key),
            "api_secret_set": bool(cfg.exchange.api_secret),
        },
        "strategy": cfg.strategy.__dict__.copy(),
        "risk": cfg.risk.__dict__.copy(),
        "news": {
            "enabled": cfg.news.enabled,
            "blacklist_minutes_before": cfg.news.blacklist_minutes_before,
            "blacklist_minutes_after": cfg.news.blacklist_minutes_after,
        },
        "backtest": cfg.backtest,
        "loop": cfg.loop,
    }


def save_config_patch(path: str | Path, patch: dict[str, Any]) -> AppConfig:
    """Merge patch into YAML file and return reloaded config."""
    path = Path(path)
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    merged = _deep_merge(raw, patch)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(merged, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return load_config(path)
