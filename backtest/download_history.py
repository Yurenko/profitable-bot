"""Download historical 1m OHLCV via CCXT (chunked). For full 5y runs, expect hours."""
from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import pandas as pd

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("download_history")


def download(
    exchange_id: str,
    symbol: str,
    timeframe: str,
    since: str,
    until: str | None,
    out: Path,
    sandbox: bool = False,
) -> pd.DataFrame:
    ex_cls = getattr(ccxt, exchange_id)
    ex = ex_cls({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    if sandbox:
        try:
            ex.set_sandbox_mode(True)
        except Exception:  # noqa: BLE001
            pass
    since_ms = int(datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp() * 1000)
    until_ms = (
        int(datetime.fromisoformat(until.replace("Z", "+00:00")).timestamp() * 1000)
        if until
        else int(datetime.now(timezone.utc).timestamp() * 1000)
    )
    all_rows: list[list] = []
    cursor = since_ms
    tf_ms = int(ex.parse_timeframe(timeframe) * 1000)
    while cursor < until_ms:
        try:
            batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=1000)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch error %s — retry", exc)
            time.sleep(2)
            continue
        if not batch:
            break
        all_rows.extend(batch)
        last = batch[-1][0]
        cursor = last + tf_ms
        logger.info("Downloaded up to %s rows=%d", datetime.utcfromtimestamp(last / 1000), len(all_rows))
        if last >= until_ms:
            break
        time.sleep(ex.rateLimit / 1000)

    df = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop_duplicates("timestamp").set_index("timestamp").sort_index()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix == ".csv":
        df.to_csv(out)
    else:
        df.to_parquet(out)
    logger.info("Wrote %s (%d rows)", out, len(df))
    return df


def main() -> None:
    p = argparse.ArgumentParser(description="Download futures OHLCV for backtests")
    p.add_argument("--exchange", default="binanceusdm")
    p.add_argument("--symbol", default="BTC/USDT:USDT")
    p.add_argument("--timeframe", default="1m")
    p.add_argument("--since", default="2021-01-01T00:00:00+00:00")
    p.add_argument("--until", default=None)
    p.add_argument("--out", default="data/historical/BTCUSDT_1m.parquet")
    p.add_argument("--sandbox", action="store_true")
    args = p.parse_args()
    download(args.exchange, args.symbol, args.timeframe, args.since, args.until, Path(args.out), args.sandbox)


if __name__ == "__main__":
    main()
