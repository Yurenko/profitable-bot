"""Live trade statistics aggregation."""
from __future__ import annotations

from src.state.store import StateStore


def test_trade_stats_live_counts_and_pnl(tmp_path):
    store = StateStore(str(tmp_path / "s.sqlite"), str(tmp_path / "a.jsonl"))

    store.record_trade(
        symbol="SOL/USDT:USDT",
        side="buy",
        action="enter",
        price=76.0,
        qty=0.19,
        mode="live",
    )
    store.record_trade(
        symbol="SOL/USDT:USDT",
        side="buy",
        action="dca",
        price=73.0,
        qty=0.24,
        mode="live",
    )
    store.record_trade(
        symbol="SOL/USDT:USDT",
        side="sell",
        action="full_tp",
        price=76.87,
        qty=0.43,
        avg_entry=74.59,
        pnl=0.99,
        mode="live",
    )
    store.record_trade(
        symbol="NEAR/USDT:USDT",
        side="buy",
        action="enter",
        price=5.0,
        qty=1.0,
        mode="live",
    )
    store.record_trade(
        symbol="NEAR/USDT:USDT",
        side="sell",
        action="full_tp",
        price=5.1,
        qty=1.0,
        avg_entry=5.0,
        pnl=0.08,
        mode="live",
    )
    # Paper trade must not count
    store.record_trade(
        symbol="SOL/USDT:USDT",
        side="sell",
        action="full_tp",
        price=80.0,
        qty=1.0,
        pnl=10.0,
        mode="paper",
    )

    stats = store.trade_stats(mode="live", equity_ref=100.0)
    assert stats["positions_opened"] == 2
    assert stats["positions_closed"] == 2
    assert stats["wins"] == 2
    assert stats["losses"] == 0
    assert abs(stats["total_pnl"] - 1.07) < 1e-9
    assert abs(stats["total_pnl_pct"] - 0.0107) < 1e-9
    assert abs(stats["win_rate"] - 1.0) < 1e-12


def test_trade_stats_counts_loss(tmp_path):
    store = StateStore(str(tmp_path / "s.sqlite"), str(tmp_path / "a.jsonl"))
    store.record_trade(
        symbol="SOL/USDT:USDT",
        side="buy",
        action="enter",
        price=70.0,
        qty=1.0,
        mode="live",
    )
    store.record_trade(
        symbol="SOL/USDT:USDT",
        side="sell",
        action="trail_exit",
        price=69.0,
        qty=1.0,
        avg_entry=70.0,
        pnl=-1.2,
        mode="live",
    )
    stats = store.trade_stats(mode="live", equity_ref=100.0)
    assert stats["positions_opened"] == 1
    assert stats["positions_closed"] == 1
    assert stats["wins"] == 0
    assert stats["losses"] == 1
    assert abs(stats["total_pnl"] - (-1.2)) < 1e-9
    assert abs(stats["win_rate"] - 0.0) < 1e-12
