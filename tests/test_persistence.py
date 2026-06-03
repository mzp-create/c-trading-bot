import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from persistence.models import (
    OrderRecord, FillRecord, PositionRecord,
    TradeRecord, EquitySnapshot, SignalRecord,
)


def test_order_record_defaults():
    o = OrderRecord(ts="2026-06-03T00:00:00+00:00", symbol="BTC/USDT",
                    side="buy", order_type="market", amount=0.001)
    assert o.price is None
    assert o.reduce_only is False
    assert o.status == "unknown"
    assert o.filled == 0.0


def test_trade_record_fields():
    t = TradeRecord(ts="2026-06-03T00:00:00+00:00", symbol="BTC/USDT",
                    side="buy", entry_price=100.0, close_price=101.0,
                    amount=0.5, pnl=0.5)
    assert t.fee == 0.0
    assert t.reason is None
    assert t.position_id is None


from persistence.schema import connect, init_db, SCHEMA_VERSION


def test_schema_creates_and_versions(tmp_path):
    conn = connect(str(tmp_path / "trading.db"))
    init_db(conn)

    # All six tables + schema_version exist.
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    for t in ("orders", "fills", "trades", "positions",
              "equity_snapshots", "signals", "schema_version"):
        assert t in names

    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == SCHEMA_VERSION == 1
    conn.close()


def test_pragmas_enabled(tmp_path):
    conn = connect(str(tmp_path / "trading.db"))
    init_db(conn)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_init_db_is_idempotent(tmp_path):
    db = str(tmp_path / "trading.db")
    conn = connect(db)
    init_db(conn)
    init_db(conn)  # second call must not duplicate the version row
    rows = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    assert rows == 1
    conn.close()


from persistence.repository import TradingRepository
from persistence.models import (
    OrderRecord as OR, FillRecord as FR, PositionRecord as PR,
    TradeRecord as TR, EquitySnapshot as ES, SignalRecord as SR,
)


def _repo(tmp_path):
    return TradingRepository(str(tmp_path / "trading.db"),
                             instance="long", mode="paper")


def test_record_order_roundtrip(tmp_path):
    repo = _repo(tmp_path)
    oid = repo.record_order(OR(ts="2026-06-03T00:00:00+00:00", symbol="BTC/USDT",
                               side="buy", order_type="market", amount=0.001,
                               reason="entry"))
    assert oid > 0
    row = repo._conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    assert row["instance"] == "long"
    assert row["mode"] == "paper"
    assert row["status"] == "unknown"
    assert row["reduce_only"] == 0


def test_update_order(tmp_path):
    repo = _repo(tmp_path)
    oid = repo.record_order(OR(ts="t", symbol="BTC/USDT", side="buy",
                               order_type="market", amount=0.001))
    repo.update_order(oid, status="filled", filled=0.001, avg_price=100.0)
    row = repo._conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    assert row["status"] == "filled"
    assert row["filled"] == 0.001
    assert row["avg_price"] == 100.0


def test_position_open_close(tmp_path):
    repo = _repo(tmp_path)
    pid = repo.open_position(PR(symbol="BTC/USDT", side="buy", amount=0.5,
                                entry_price=100.0, opened_at="t0"))
    assert pid > 0
    repo.close_position(pid, closed_at="t1", close_price=110.0, realized_pnl=5.0)
    row = repo._conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
    assert row["status"] == "closed"
    assert row["closed_at"] == "t1"
    assert row["realized_pnl"] == 5.0


def test_record_trade_fill_signal_equity(tmp_path):
    repo = _repo(tmp_path)
    tid = repo.record_trade(TR(ts="t", symbol="BTC/USDT", side="buy",
                               entry_price=100.0, close_price=101.0,
                               amount=0.5, pnl=0.5, reason="take_profit"))
    fid = repo.record_fill(FR(ts="t", symbol="BTC/USDT", side="sell",
                              amount=0.5, price=101.0, fee=0.1))
    sid = repo.record_signal(SR(ts="t", symbol="BTC/USDT", decision="BUY",
                                confidence=0.8, acted=True, order_id=None))
    eid = repo.snapshot_equity(ES(ts="t", balance=500.0, equity=505.0,
                                  open_count=1, daily_pnl=5.0))
    assert all(x > 0 for x in (tid, fid, sid, eid))
    assert repo._conn.execute("SELECT acted FROM signals WHERE id=?",
                              (sid,)).fetchone()[0] == 1


def test_db_failure_is_isolated(tmp_path):
    """A broken connection must make writes return sentinels, not raise."""
    repo = _repo(tmp_path)
    repo._conn.close()  # subsequent writes will raise sqlite3.ProgrammingError
    assert repo.record_order(OR(ts="t", symbol="X", side="buy",
                                order_type="market", amount=1.0)) == -1
    assert repo.record_fill(FR(ts="t", symbol="X", side="sell",
                               amount=1.0, price=1.0)) == -1
    assert repo.open_position(PR(symbol="X", side="buy", amount=1.0,
                                 entry_price=1.0, opened_at="t")) == -1
    assert repo.record_trade(TR(ts="t", symbol="X", side="buy",
                                entry_price=1, close_price=1,
                                amount=1, pnl=0)) == -1
    assert repo.snapshot_equity(ES(ts="t", balance=1.0, equity=1.0)) == -1
    assert repo.record_signal(SR(ts="t", symbol="X", decision="HOLD",
                                 confidence=0.0)) == -1
    # update/close return None without raising
    assert repo.update_order(1, status="filled", filled=1.0,
                             avg_price=1.0) is None
    assert repo.close_position(1, closed_at="t", close_price=1.0,
                               realized_pnl=0.0) is None
