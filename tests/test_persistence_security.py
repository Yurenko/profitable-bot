"""Security / reliability: persistence, no double entries, crash recovery."""
from __future__ import annotations

from src.exchange.paper import PaperExchange
from src.position import Position
from src.state.store import StateStore


def test_position_persists_across_store_reopen(tmp_path):
    db = tmp_path / "state.sqlite"
    audit = tmp_path / "audit.jsonl"
    store = StateStore(str(db), str(audit))
    pos = Position(symbol="BTC/USDT:USDT", leverage=5)
    pos.add(50_000, 0.01, 100.0, fee=0.2)
    store.save_position(pos)
    store.save_equity(10_000)
    store.audit("test_event", foo=1)

    store2 = StateStore(str(db), str(audit))
    loaded = store2.load_positions()
    assert "BTC/USDT:USDT" in loaded
    assert abs(loaded["BTC/USDT:USDT"].avg_entry - 50_000) < 1e-6
    assert abs(store2.last_equity() - 10_000) < 1e-9
    assert audit.exists()
    assert "test_event" in audit.read_text(encoding="utf-8")


def test_no_double_order_registration(tmp_path):
    store = StateStore(str(tmp_path / "s.sqlite"), str(tmp_path / "a.jsonl"))
    assert store.register_order("oid-1", "BTC/USDT:USDT", "enter", {"x": 1})
    assert not store.register_order("oid-1", "BTC/USDT:USDT", "enter", {"x": 1})
    assert store.order_exists("oid-1")


def test_paper_exchange_buy_sell_accounting():
    ex = PaperExchange(initial_equity=10_000, taker_fee=0.0004, leverage=5)
    ex.set_mark("BTC/USDT:USDT", 100.0)
    # margin for 10 qty * 100 / 5 = 200
    ex.create_market_order("BTC/USDT:USDT", "buy", 10.0, params={"margin": 200.0})
    pos = ex.account.positions["BTC/USDT:USDT"]
    assert abs(pos.avg_entry - 100) < 1e-9
    eq_after_open = ex.equity()
    assert eq_after_open < 10_000  # fees

    ex.set_mark("BTC/USDT:USDT", 101.0)
    ex.create_market_order("BTC/USDT:USDT", "sell", 10.0)
    assert "BTC/USDT:USDT" not in ex.account.positions
    # Profit ~10 minus fees
    assert ex.equity() > eq_after_open


def test_crash_restart_does_not_duplicate_position(tmp_path):
    """Simulate restart: reload state; same client order id blocked."""
    db = tmp_path / "state.sqlite"
    audit = tmp_path / "audit.jsonl"
    store = StateStore(str(db), str(audit))
    pos = Position(symbol="ETH/USDT:USDT", leverage=5)
    pos.add(2000, 1.0, 400.0)
    store.save_position(pos)
    coid = "mrdca_ETH_enter_1"
    assert store.register_order(coid, "ETH/USDT:USDT", "enter", {})

    # "Restart"
    store_b = StateStore(str(db), str(audit))
    positions = store_b.load_positions()
    assert len(positions) == 1
    assert not store_b.register_order(coid, "ETH/USDT:USDT", "enter", {})


def test_paper_rejects_zero_mark():
    ex = PaperExchange(initial_equity=1000, taker_fee=0.0004, leverage=5)
    try:
        ex.set_mark("X", 0)
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_sane_equity_resets_corruption():
    from src.app_factory import _sane_equity

    assert _sane_equity(200, 200) == 200
    assert _sane_equity(1e110, 200) == 200
    assert _sane_equity(float("nan"), 200) == 200
    assert abs(_sane_equity(250, 200) - 250) < 1e-9
