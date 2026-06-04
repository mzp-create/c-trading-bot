#!/usr/bin/env python3
"""One-time importer: legacy trades.csv -> SQLite trades table.

Handles both CSV layouts found in this repo:
  full (9 cols):  timestamp,symbol,side,entry_price,close_price,amount,pnl,reason,mode
  slim (6 cols):  timestamp,symbol,side,amount,price,pnl   (entry==close==price)

Idempotent: a row already present (same ts+symbol+amount) is skipped. Run once
per instance:

    python scripts/import_trades_csv.py --csv data/trades.csv --db data/trading.db --instance default
"""

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from persistence import TradingRepository, TradeRecord


def _row_to_trade(row: dict) -> TradeRecord:
    ts = row.get("timestamp", "")
    symbol = row.get("symbol", "")
    side = row.get("side", "")
    pnl = float(row.get("pnl", 0) or 0)
    if "entry_price" in row and "close_price" in row:
        entry = float(row.get("entry_price", 0) or 0)
        close = float(row.get("close_price", 0) or 0)
        reason = row.get("reason") or "imported"
    else:  # slim 6-field layout: single price for entry & close
        price = float(row.get("price", 0) or 0)
        entry = close = price
        reason = "imported"
    return TradeRecord(ts=ts, symbol=symbol, side=side, entry_price=entry,
                       close_price=close, amount=abs(float(row.get("amount", 0)
                       or 0)), pnl=pnl, reason=reason)


def import_csv(csv_path: str, repo: TradingRepository) -> int:
    """Import rows into the repo. Returns the number of NEW rows inserted."""
    path = Path(csv_path)
    if not path.exists():
        return 0
    inserted = 0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if not row.get("timestamp"):
                continue
            trade = _row_to_trade(row)
            if repo.trade_exists(trade.ts, trade.symbol, trade.amount):
                continue
            if repo.record_trade(trade) != -1:
                inserted += 1
    return inserted


def main():
    p = argparse.ArgumentParser(description="Import legacy trades.csv into SQLite")
    p.add_argument("--csv", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--instance", default="default")
    p.add_argument("--mode", default="paper", choices=["paper", "live"])
    args = p.parse_args()

    repo = TradingRepository(args.db, instance=args.instance, mode=args.mode)
    n = import_csv(args.csv, repo)
    print(f"Imported {n} new trade(s) from {args.csv} into {args.db}")


if __name__ == "__main__":
    main()
