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
