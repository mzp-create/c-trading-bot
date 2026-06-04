import sys
import csv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.import_trades_csv import import_csv
from persistence import TradingRepository


def _write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def test_import_full_format(tmp_path):
    csv_path = tmp_path / "trades.csv"
    _write_csv(csv_path,
               ["timestamp", "symbol", "side", "entry_price", "close_price",
                "amount", "pnl", "reason", "mode"],
               [["2026-06-01T10:00:00+00:00", "BTC/USDT", "buy", "100", "110",
                 "0.5", "5.0", "manual_close", "paper"]])
    repo = TradingRepository(str(tmp_path / "trading.db"),
                             instance="default", mode="paper")
    n = import_csv(str(csv_path), repo)
    assert n == 1
    assert len(repo.recent_trades()) == 1
    # Idempotent: re-import inserts nothing.
    assert import_csv(str(csv_path), repo) == 0
    assert len(repo.recent_trades()) == 1


def test_import_six_field_format(tmp_path):
    csv_path = tmp_path / "trades.csv"
    _write_csv(csv_path,
               ["timestamp", "symbol", "side", "amount", "price", "pnl"],
               [["2026-06-01T10:00:00+00:00", "ETH/USDT", "sell", "2", "50",
                 "-3.0"]])
    repo = TradingRepository(str(tmp_path / "trading.db"),
                             instance="short", mode="live")
    n = import_csv(str(csv_path), repo)
    assert n == 1
    t = repo.recent_trades()[0]
    assert t.symbol == "ETH/USDT"
    assert t.entry_price == 50.0 and t.close_price == 50.0
    assert t.pnl == -3.0
    assert t.reason == "imported"
