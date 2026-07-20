# Mean-Reversion DCA Perpetual Futures Bot

Production-oriented Python bot for USDT-M perpetual futures (Binance / Bybit via **CCXT**).  
Strategy: **long-only multi-timeframe mean reversion** with **DCA**, **isolated 5x**, **no hard stop-loss** (margin top-ups instead), plus news / funding / correlation / circuit-breaker risk controls.

> Trading involves substantial risk of loss. This software is for research and education. Test on **testnet** and paper mode extensively before any mainnet use. Past or backtested performance does not guarantee future results.

---

## Architecture

```
main.py                 CLI (paper | live | backtest | walk-forward)
config.yaml             Parameters (API keys via ${ENV})
src/
  strategy.py           Entry / DCA / TP / trailing / partial close
  risk_manager.py       Sizing, survival margin, daily loss, circuit breaker
  filters.py            RSI/ADX/ATR, funding+OI, news, correlation
  indicators.py         RSI, ADX, ATR (pandas, no TA-Lib required)
  position.py           Avg entry, liq distance, DCA levels
  bot.py                Polling loop, graceful shutdown, audit
  exchange/ccxt_client.py   Retries, rate limits, reconnect, testnet
  exchange/paper.py     Simulated fills + fees + funding
  state/store.py        SQLite WAL persistence + JSONL audit trail
  news/calendar.py      Economic calendar blackout window
backtest/
  backtest.py           Event-driven multi-TF backtester
  metrics.py            PnL, DD, Sharpe, PF, expectancy
  monte_carlo.py        Block bootstrap + trade shuffle
  walk_forward.py       Rolling parameter search
  download_history.py   Chunked CCXT 1m history downloader
tests/                  Pytest: entry, sizing, TP/DCA, filters, persistence
```

### Strategy rules (implemented)

| Rule | Detail |
|------|--------|
| Leverage | Fixed 5x, isolated margin |
| Size | ~1% equity as margin budget; optional inverse-ATR scaling |
| Entry | RSI(14) 1m < 30 **and** RSI 5m < 35 **and** ADX < ~38 **and** ATR not in top ~10% of 100 **and** funding/OI not overheated |
| TP | ≥1% from average entry; 2% if 5m RSI < 25; optional partial close + trailing |
| DCA | Add ~1% equity when underwater and RSI 15m < 30; max DCA levels (default 4) |
| SL | None — proactive margin top-ups when liquidation distance < buffer |
| News | No new entries ±15m around high-impact calendar events |
| Risk | Max daily loss, peak equity circuit breaker, correlation cap, fee/funding in PnL |

---

## Setup

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env   # then edit keys
```

Set environment variables (preferred over putting secrets in YAML):

```text
EXCHANGE_API_KEY=...
EXCHANGE_API_SECRET=...
```

`config.yaml` already references `${EXCHANGE_API_KEY}` / `${EXCHANGE_API_SECRET}`.

Keep `exchange.testnet: true` until you have validated paper + testnet thoroughly.

---

## Tests (run these first)

```bash
pytest -v
```

Coverage includes:

- Multi-TF oversold / ADX / ATR percentile entry filters  
- Position sizing, 5x leverage, 12% adverse-move survival margin  
- TP / DCA / average price / max DCA levels  
- Funding+OI, news blackout, correlation  
- SQLite persistence, audit trail, **no double entries** after crash/restart  

---

## Backtesting

### Quick synthetic run (CI / no data files)

If `data/historical/` has no parquet/csv, the backtester builds a **multi-regime synthetic** 1m series (trend / range / high-vol / low-vol / bear) so you can validate the pipeline immediately:

```bash
python main.py backtest --bars 15000 --monte-carlo
```

### Real multi-year 1m data (recommended)

Download chunked history (can take a long time for 5 years of 1m bars):

```bash
python -m backtest.download_history --exchange binanceusdm --symbol BTC/USDT:USDT --since 2021-01-01T00:00:00+00:00 --out data/historical/BTCUSDT_1m.parquet
```

Then:

```bash
python main.py backtest --monte-carlo
python main.py walk-forward --train-days 180 --test-days 60
```

### Metrics reported

Net profit (after fees & funding), max drawdown, Sharpe, win rate, profit factor, expectancy, Monte Carlo final-equity percentiles and ruin probability.

### Expectancy note

Positive expectancy on **real** 5y data is **not guaranteed** out of the box — mean-reversion longs on perps are regime-dependent. Use walk-forward to tune `rsi_*`, `adx_max`, `tp_min_pct`, and always validate on paper/testnet. The synthetic generator injects dips so unit/integration paths exercise entries; treat synthetic PnL as a pipeline check, not proof of edge.

---

## Paper trading

Uses **public CCXT market data** + **simulated fills** (fees applied), with SQLite state:

```bash
python main.py paper --once    # single iteration
python main.py paper           # loop until Ctrl+C
```

---

## Live trading (testnet first)

1. Create testnet API keys on Binance / Bybit.  
2. Put them in `.env`.  
3. Confirm `exchange.testnet: true` and `mode` via CLI:

```bash
python main.py live --once
python main.py live
```

Mainnet requires an explicit flag:

```bash
python main.py live --i-understand-mainnet-risk
```

---

## Parameter tuning

Primary knobs in `config.yaml` → `strategy` / `risk`:

- `rsi_1m_entry`, `rsi_5m_entry`, `rsi_15m_dca` — sensitivity vs trade frequency  
- `adx_max` — avoid strong trends (35–40 band)  
- `atr_percentile_max` — skip volatility spikes  
- `tp_min_pct` / `tp_strong_pct` — reward vs hold time  
- `max_dca_levels` — inventory risk  
- `min_adverse_move_pct` — margin sized to survive 10–15% adverse moves at 5x  
- `max_daily_loss_pct`, `circuit_breaker_drawdown_pct` — hard brakes  

Walk-forward grid (edit `backtest/walk_forward.py` `DEFAULT_GRID`) searches a small RSI/ADX/TP space per fold.

---

## Persistence & safety

- Open positions, equity, and client order IDs stored in SQLite (`PRAGMA synchronous=FULL`, WAL).  
- Every fill / top-up / error appended to `logs/audit.jsonl`.  
- Duplicate `clientOrderId` registrations are rejected (restart-safe).  
- SIGINT/SIGTERM triggers graceful loop exit.  
- CCXT wrapper retries network/rate-limit errors and reconnects.

---

## Clarifications / defaults chosen

| Topic | Choice |
|-------|--------|
| Exchange | `binanceusdm` default; swap `bybit` in config |
| ADX timeframe | Computed on **1m** (fast veto); can be moved to 5m if preferred |
| OI series live | Single-point OI from exchange; full series when paper/backtest provides history |
| News API | Cached JSON + placeholders (no fragile scrape dependency); drop events into `data/economic_calendar.json` |
| Partial TP | 50% at min TP, remainder trails |

If you want different defaults (Bybit-only, ADX on 5m, live Investing.com fetch), say so and we can adjust.

---

## License

MIT — use at your own risk.
