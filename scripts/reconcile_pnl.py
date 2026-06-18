#!/usr/bin/env python3
"""Reconcile realized P&L + fees from the Bitfinex ledger (the source of truth)
into an instance DB, and print truth vs the bot's recorded trade log.

On Bitfinex margin the per-trade fee is 0 and realized P&L is booked as wallet
ledger entries; exchange-side closes (catastrophe stops, reconciled phantoms)
never reach the bot's close path, so the trade log overstates P&L. This pulls
the ledger and (optionally) backfills the missed closes.

READ-ONLY on the exchange (no orders). Writes only to the local instance DB.

  python scripts/reconcile_pnl.py --config config/long.yaml --instance long
  python scripts/reconcile_pnl.py --config config/long.yaml --instance long --backfill
"""
import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_envf = ROOT / ".env"
if _envf.exists():
    for _line in _envf.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#"):
            continue
        if _line.startswith("export "):
            _line = _line[7:]
        if "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

import yaml  # noqa: E402
from bitfinex.rest import BfxRest  # noqa: E402
from persistence.repository import TradingRepository  # noqa: E402
from execution.ledger_reconciler import reconcile  # noqa: E402

_REF = re.compile(r"\$\{([^}]+)\}")


def _resolve(s):
    return _REF.sub(lambda m: os.environ.get(m.group(1), ""), s or "")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/long.yaml")
    ap.add_argument("--instance", default="long")
    ap.add_argument("--backfill", action="store_true",
                    help="Insert reconciled trades for unrecorded exchange-side closes.")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(ROOT / args.config)) or {}
    ex = cfg.get("exchange", {})
    api_key = _resolve(ex.get("api_key", "")) or os.environ.get("BITFINEX_API_KEY", "")
    api_secret = _resolve(ex.get("api_secret", "")) or os.environ.get("BITFINEX_API_SECRET", "")
    if not api_key or not api_secret:
        sys.exit("ERROR: API key/secret not found.")

    rest = BfxRest(api_key, api_secret)

    # Reference prices for attributing a close price to a symbol (live tickers).
    symbol_refs = {}
    for s in cfg.get("trading", {}).get("symbols", []):
        name = s.get("name")
        if not name:
            continue
        try:
            symbol_refs[name] = float(rest.get_ticker(name).last)
        except Exception:
            pass

    db_path = str(ROOT / "instances" / args.instance / "data" / "trading.live.db")
    repo = TradingRepository(db_path, instance=args.instance, mode="live")

    print(f"Reconciling ledger -> {db_path}")
    print(f"Symbol refs: { {k: round(v,2) for k,v in symbol_refs.items()} }")
    summary = reconcile(rest, repo, currencies=["UST", "USD"],
                        symbol_refs=symbol_refs, backfill=args.backfill)

    bot = repo.total_realized_pnl()
    print("\n=== EXCHANGE TRUTH (ledger) ===")
    print(f"  realized P&L : ${summary['realized_pnl']:+.4f}  ({summary['n_closes']} closes)")
    print(f"  fees         : ${summary['fees']:+.4f}  ({summary['n_fees']} fee entries)")
    print(f"  funding      : ${summary['funding']:+.4f}")
    print(f"  NET P&L      : ${summary['net_pnl']:+.4f}")
    print(f"  wallet equity: ${summary['current_balance']}")
    print(f"  new ledger entries this run: {summary['new_entries']}")
    print(f"  backfilled trades          : {summary['backfilled']}")
    print("\n=== BOT TRADE LOG ===")
    print(f"  recorded realized P&L: ${bot:+.4f}")
    print(f"\n>>> DISCREPANCY (bot - exchange): ${bot - summary['net_pnl']:+.4f}")
    if not args.backfill:
        print("    (re-run with --backfill to insert the missed closes into the trade log)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
