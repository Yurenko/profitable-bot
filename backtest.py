#!/usr/bin/env python3
"""Shortcut: python backtest.py [--bars N] [--monte-carlo]"""
from main import cmd_backtest, build_parser
import argparse
import sys

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--monte-carlo", action="store_true")
    p.add_argument("--bars", type=int, default=None)
    p.add_argument("--out", default="data/backtest_equity.csv")
    args = p.parse_args()
    cmd_backtest(args)
