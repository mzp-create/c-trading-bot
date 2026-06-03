import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from persistence import TradingRepository, TradeRecord


def test_read_trades_from_db(tmp_path, monkeypatch):
    db = tmp_path / "trading.db"
    repo = TradingRepository(str(db), instance="long", mode="paper")
    repo.record_trade(TradeRecord(ts="2026-06-01T10:00:00+00:00",
                                  symbol="BTC/USDT", side="buy",
                                  entry_price=100, close_price=110,
                                  amount=0.5, pnl=5.0, reason="take_profit"))

    import dashboard.api_server as api
    # Point the dashboard at our temp DB only.
    monkeypatch.setattr(api, "_db_paths", lambda: [db])
    rows = api.read_trades()

    assert len(rows) == 1
    r = rows[0]
    assert r["timestamp"] == "2026-06-01T10:00:00+00:00"
    assert r["symbol"] == "BTC/USDT"
    assert r["side"] == "buy"
    assert float(r["pnl"]) == 5.0
    assert float(r["entry_price"]) == 100.0
    assert float(r["close_price"]) == 110.0
    assert r["mode"] == "paper"
