#!/usr/bin/env python3
"""CLI entrypoint: paper | live | backtest | walk-forward | monte-carlo."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Ensure project root on path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load .env before config reads ${EXCHANGE_API_KEY} etc.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from src.config import load_config
from src.logging_setup import setup_logging


def cmd_paper(args: argparse.Namespace) -> None:
    from src.app_factory import create_bot
    from src.logging_setup import setup_logging

    cfg = load_config(args.config)
    setup_logging(cfg.logging)
    bot = create_bot(cfg, "paper")
    if args.once:
        bot.run_once()
    else:
        bot.run()


def cmd_live(args: argparse.Namespace) -> None:
    from src.app_factory import create_bot
    from src.logging_setup import setup_logging

    cfg = load_config(args.config)
    cfg.mode = "live"
    setup_logging(cfg.logging)
    if not cfg.exchange.api_key or not cfg.exchange.api_secret:
        raise SystemExit(
            "Live mode requires EXCHANGE_API_KEY and EXCHANGE_API_SECRET "
            "(or values in config). Prefer testnet=true until validated."
        )
    if not cfg.exchange.testnet and not args.i_understand_mainnet_risk:
        raise SystemExit(
            "Refusing mainnet live without --i-understand-mainnet-risk. "
            "Keep exchange.testnet: true in config.yaml for safety."
        )
    bot = create_bot(cfg, "live")
    if args.once:
        bot.run_once()
    else:
        bot.run()


def cmd_dashboard(args: argparse.Namespace) -> None:
    from dashboard.server import run_dashboard

    run_dashboard(host=args.host, port=args.port, open_browser=not args.no_browser)


def cmd_backtest(args: argparse.Namespace) -> None:
    from backtest.backtest import Backtester, load_or_synthesize
    from backtest.monte_carlo import run_monte_carlo, shuffle_trades_monte_carlo

    cfg = load_config(args.config)
    setup_logging(cfg.logging)
    data_dir = cfg.backtest.get("data_dir", "data/historical")
    primary_symbol = (cfg.symbols[0] if cfg.symbols else "BTC/USDT:USDT")
    df, data_info = load_or_synthesize(
        data_dir,
        symbol=primary_symbol,
        start=cfg.backtest.get("start", "2021-01-01"),
        end=cfg.backtest.get("end", "2025-12-31"),
        max_bars=args.bars,
    )
    bt = Backtester(cfg)
    result = bt.run(
        df,
        initial_capital=float(cfg.backtest.get("initial_capital", 10_000)),
        data_info=data_info,
    )
    m = result.metrics
    assert m is not None
    print("=== Backtest Metrics ===")
    print(json.dumps(m.to_dict(), indent=2))
    print(f"Fees paid: {result.fees_paid:.4f}  Funding paid: {result.funding_paid:.4f}")
    print(f"Trades: {len(result.trades)}")
    if result.data_info:
        di = result.data_info
        print(f"Data: {di.source} | {di.bars_used} bars (~{di.days_approx:.1f} days)")
        print(f"Period: {di.period_start[:10]} → {di.period_end[:10]}")
        if di.note:
            print(f"Note: {di.note}")

    if args.monte_carlo:
        mc_cfg = cfg.backtest.get("monte_carlo") or {}
        mc = run_monte_carlo(
            result.equity_curve,
            float(cfg.backtest.get("initial_capital", 10_000)),
            n_simulations=int(mc_cfg.get("n_simulations", 500)),
            block_size=int(mc_cfg.get("block_size", 20)),
        )
        print("=== Monte Carlo (block bootstrap) ===")
        print(
            json.dumps(
                {
                    "p5": mc.final_equity_p5,
                    "p50": mc.final_equity_p50,
                    "p95": mc.final_equity_p95,
                    "max_dd_p95": mc.max_dd_p95,
                    "prob_ruin": mc.prob_ruin,
                },
                indent=2,
            )
        )
        sells = [t["pnl"] for t in result.trades if t["side"] == "sell"]
        if sells:
            mc2 = shuffle_trades_monte_carlo(
                sells, float(cfg.backtest.get("initial_capital", 10_000))
            )
            print("=== Monte Carlo (trade shuffle) ===")
            print(
                json.dumps(
                    {
                        "p5": mc2.final_equity_p5,
                        "p50": mc2.final_equity_p50,
                        "p95": mc2.final_equity_p95,
                        "prob_ruin": mc2.prob_ruin,
                    },
                    indent=2,
                )
            )

    out = Path(args.out) if args.out else Path("data/backtest_equity.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    result.equity_curve.to_csv(out, header=["equity"])
    print(f"Equity curve written to {out}")


def cmd_walk_forward(args: argparse.Namespace) -> None:
    from backtest.backtest import load_or_synthesize
    from backtest.walk_forward import walk_forward

    cfg = load_config(args.config)
    setup_logging(cfg.logging)
    primary_symbol = (cfg.symbols[0] if cfg.symbols else "BTC/USDT:USDT")
    df, _ = load_or_synthesize(
        cfg.backtest.get("data_dir", "data/historical"),
        symbol=primary_symbol,
    )
    if args.bars:
        df = df.iloc[: args.bars]
    wf = cfg.backtest.get("walk_forward") or {}
    folds = walk_forward(
        df,
        cfg,
        train_days=int(args.train_days or wf.get("train_days", 30)),
        test_days=int(args.test_days or wf.get("test_days", 10)),
    )
    rows = []
    for f in folds:
        rows.append(
            {
                "train": f"{f.train_start.date()}→{f.train_end.date()}",
                "test": f"{f.test_start.date()}→{f.test_end.date()}",
                "params": f.best_params,
                "test_net_pct": f.test_metrics.net_profit_pct,
                "test_sharpe": f.test_metrics.sharpe,
                "test_dd": f.test_metrics.max_drawdown_pct,
                "test_trades": f.test_metrics.n_trades,
            }
        )
    print(json.dumps(rows, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Perpetual futures mean-reversion DCA bot")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="command", required=True)

    paper = sub.add_parser("paper", help="Paper trading (CCXT data + simulated fills)")
    paper.add_argument("--once", action="store_true")
    paper.set_defaults(func=cmd_paper)

    live = sub.add_parser("live", help="Live trading via CCXT (testnet by default)")
    live.add_argument("--once", action="store_true")
    live.add_argument("--i-understand-mainnet-risk", action="store_true")
    live.set_defaults(func=cmd_live)

    bt = sub.add_parser("backtest", help="Run historical / synthetic backtest")
    bt.add_argument("--monte-carlo", action="store_true")
    bt.add_argument("--bars", type=int, default=None, help="Limit bars (debug/CI)")
    bt.add_argument("--out", default="data/backtest_equity.csv")
    bt.set_defaults(func=cmd_backtest)

    wf = sub.add_parser("walk-forward", help="Walk-forward parameter optimization")
    wf.add_argument("--train-days", type=int, default=None)
    wf.add_argument("--test-days", type=int, default=None)
    wf.add_argument("--bars", type=int, default=None)
    wf.set_defaults(func=cmd_walk_forward)

    dash = sub.add_parser("dashboard", help="Web dashboard (GUI)")
    dash.add_argument("--host", default="127.0.0.1")
    dash.add_argument("--port", type=int, default=8080)
    dash.add_argument("--no-browser", action="store_true")
    dash.set_defaults(func=cmd_dashboard)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
