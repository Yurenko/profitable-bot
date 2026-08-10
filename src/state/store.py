"""SQLite-backed persistent state + JSONL audit trail."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from src.position import Position

logger = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    """Crash-safe store for positions, equity, and idempotent order keys."""

    def __init__(self, db_path: str, audit_path: str) -> None:
        self.db_path = Path(db_path)
        self.audit_path = Path(audit_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=FULL;")
        return conn

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                yield conn
            finally:
                conn.close()

    def _init_db(self) -> None:
        with self._db() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    client_order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    equity REAL NOT NULL,
                    ts TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    action TEXT NOT NULL,
                    price REAL NOT NULL,
                    qty REAL NOT NULL,
                    notional REAL NOT NULL,
                    fee REAL NOT NULL DEFAULT 0,
                    avg_entry REAL,
                    pnl REAL,
                    dca_level INTEGER,
                    mode TEXT,
                    client_order_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts DESC);
                """
            )

    # --- KV ---
    def set_kv(self, key: str, value: Any) -> None:
        with self._db() as conn:
            conn.execute(
                "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value), _utcnow()),
            )

    def get_kv(self, key: str, default: Any = None) -> Any:
        with self._db() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            if not row:
                return default
            return json.loads(row["value"])

    # --- Positions ---
    def save_position(self, position: Position) -> None:
        with self._db() as conn:
            if not position.is_open:
                conn.execute("DELETE FROM positions WHERE symbol=?", (position.symbol,))
            else:
                conn.execute(
                    "INSERT INTO positions(symbol, payload, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(symbol) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
                    (position.symbol, json.dumps(position.to_dict()), _utcnow()),
                )

    def load_positions(self) -> dict[str, Position]:
        with self._db() as conn:
            rows = conn.execute("SELECT payload FROM positions").fetchall()
        out: dict[str, Position] = {}
        for row in rows:
            pos = Position.from_dict(json.loads(row["payload"]))
            out[pos.symbol] = pos
        return out

    def delete_position(self, symbol: str) -> None:
        with self._db() as conn:
            conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))

    # --- Idempotent orders ---
    def register_order(
        self, client_order_id: str, symbol: str, side: str, payload: dict[str, Any]
    ) -> bool:
        """Return True if newly registered; False if duplicate (already seen)."""
        with self._db() as conn:
            existing = conn.execute(
                "SELECT client_order_id FROM orders WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
            if existing:
                return False
            conn.execute(
                "INSERT INTO orders(client_order_id, symbol, side, payload, status, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    client_order_id,
                    symbol,
                    side,
                    json.dumps(payload),
                    "submitted",
                    _utcnow(),
                ),
            )
            return True

    def update_order_status(self, client_order_id: str, status: str) -> None:
        with self._db() as conn:
            conn.execute(
                "UPDATE orders SET status=? WHERE client_order_id=?",
                (status, client_order_id),
            )

    def order_exists(self, client_order_id: str) -> bool:
        with self._db() as conn:
            row = conn.execute(
                "SELECT 1 FROM orders WHERE client_order_id=?", (client_order_id,)
            ).fetchone()
            return row is not None

    def save_equity(self, equity: float) -> None:
        with self._db() as conn:
            conn.execute(
                "INSERT INTO equity_snapshots(equity, ts) VALUES(?,?)",
                (equity, _utcnow()),
            )
        self.set_kv("last_equity", equity)

    def last_equity(self, default: float = 0.0) -> float:
        val = self.get_kv("last_equity", default)
        return float(val if val is not None else default)

    def equity_history(self, limit: int = 300) -> list[dict[str, Any]]:
        """Recent equity snapshots for live/paper chart (oldest → newest)."""
        limit = max(1, min(int(limit), 5000))
        with self._db() as conn:
            rows = conn.execute(
                "SELECT equity, ts FROM equity_snapshots ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        points = [{"t": row["ts"], "v": float(row["equity"])} for row in rows]
        points.reverse()
        return points

    def clear_equity_history(self) -> None:
        with self._db() as conn:
            conn.execute("DELETE FROM equity_snapshots")

    # --- Trade history (open / DCA / close) ---
    def record_trade(
        self,
        *,
        symbol: str,
        side: str,
        action: str,
        price: float,
        qty: float,
        fee: float = 0.0,
        avg_entry: float | None = None,
        pnl: float | None = None,
        dca_level: int | None = None,
        mode: str | None = None,
        client_order_id: str | None = None,
    ) -> None:
        notional = float(price) * float(qty)
        with self._db() as conn:
            conn.execute(
                "INSERT INTO trades("
                "ts, symbol, side, action, price, qty, notional, fee, "
                "avg_entry, pnl, dca_level, mode, client_order_id"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _utcnow(),
                    symbol,
                    side,
                    action,
                    float(price),
                    float(qty),
                    notional,
                    float(fee),
                    None if avg_entry is None else float(avg_entry),
                    None if pnl is None else float(pnl),
                    dca_level,
                    mode,
                    client_order_id,
                ),
            )

    def list_trades(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._db() as conn:
            rows = conn.execute(
                "SELECT id, ts, symbol, side, action, price, qty, notional, fee, "
                "avg_entry, pnl, dca_level, mode, client_order_id "
                "FROM trades ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def trade_stats(
        self,
        *,
        mode: str = "live",
        equity_ref: float | None = None,
    ) -> dict[str, Any]:
        """Aggregate closed-position stats for dashboard (live/paper).

        - positions_opened: count of enter fills
        - positions_closed: count of full closes (full_tp / trail_exit)
        - wins / losses: closes with pnl > 0 / <= 0
        - total_pnl: sum of close PnL (incl. partial_tp)
        - total_pnl_pct: total_pnl / equity_ref (account ROI)
        """
        mode_s = (mode or "live").strip().lower()
        close_full = ("full_tp", "trail_exit")
        close_any = ("full_tp", "trail_exit", "partial_tp")

        with self._db() as conn:
            opened = conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE lower(coalesce(mode,''))=? AND action='enter'",
                (mode_s,),
            ).fetchone()["n"]
            closed_rows = conn.execute(
                "SELECT pnl, avg_entry, qty, notional FROM trades "
                "WHERE lower(coalesce(mode,''))=? AND action IN (?, ?) "
                "AND pnl IS NOT NULL",
                (mode_s, *close_full),
            ).fetchall()
            pnl_rows = conn.execute(
                "SELECT coalesce(SUM(pnl), 0) AS s FROM trades "
                "WHERE lower(coalesce(mode,''))=? AND action IN (?, ?, ?) "
                "AND pnl IS NOT NULL",
                (mode_s, *close_any),
            ).fetchone()

        closed = len(closed_rows)
        wins = sum(1 for r in closed_rows if float(r["pnl"] or 0) > 0)
        losses = sum(1 for r in closed_rows if float(r["pnl"] or 0) <= 0)
        total_pnl = float(pnl_rows["s"] or 0)

        # Position-notional ROI (sum pnl / sum entry notional of full closes)
        notional_sum = 0.0
        for r in closed_rows:
            n = r["notional"]
            if n is None:
                ae = float(r["avg_entry"] or 0)
                q = float(r["qty"] or 0)
                n = ae * q if ae > 0 and q > 0 else 0.0
            notional_sum += float(n or 0)

        ref = float(equity_ref) if equity_ref is not None and equity_ref > 0 else 0.0
        total_pnl_pct = (total_pnl / ref) if ref > 0 else 0.0
        notional_pnl_pct = (total_pnl / notional_sum) if notional_sum > 0 else 0.0
        win_rate = (wins / closed) if closed > 0 else 0.0

        return {
            "mode": mode_s,
            "positions_opened": int(opened),
            "positions_closed": int(closed),
            "wins": int(wins),
            "losses": int(losses),
            "win_rate": float(win_rate),
            "total_pnl": float(total_pnl),
            "total_pnl_pct": float(total_pnl_pct),
            "notional_pnl_pct": float(notional_pnl_pct),
            "equity_ref": float(ref) if ref > 0 else None,
        }

    # --- Audit ---
    def audit(self, event: str, **fields: Any) -> None:
        record = {"ts": _utcnow(), "event": event, **fields}
        line = json.dumps(record, default=str)
        with self._lock:
            with self.audit_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
        logger.info("AUDIT %s %s", event, fields)
