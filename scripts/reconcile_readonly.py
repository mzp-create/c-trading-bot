#!/usr/bin/env python3
"""READ-ONLY Bitfinex reconciliation via the bot's BfxRest. Places NO orders.

Fetches positions, balances, recent orders and trades to compare against bot
logs. Routes through `bitfinex.rest.BfxRest` (the live bot's authenticated path)
instead of an independent ccxt client — the ccxt version failed with
"nonce: small" because its millisecond nonce was below the high-water mark the
bot's bfxapi client had already set on the shared key. One bfxapi client = one
monotonic nonce source, so no collision (and the correct REST host).

Usage:  python scripts/reconcile_readonly.py [--config config/long.yaml]
"""
import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env so ${...} refs in the config (and BITFINEX_* fallbacks) resolve.
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
from bitfinex import symbols  # noqa: E402
from bitfinex.rest import BfxRest  # noqa: E402

SYMS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
_REF = re.compile(r"\$\{([^}]+)\}")


def _resolve(s: str) -> str:
    return _REF.sub(lambda m: os.environ.get(m.group(1), ""), s or "")


def section(t):
    print("\n" + "=" * 60 + "\n" + t + "\n" + "=" * 60)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/long.yaml")
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()

    ex = (yaml.safe_load(open(ROOT / args.config)) or {}).get("exchange", {})
    api_key = _resolve(ex.get("api_key", "")) or os.environ.get("BITFINEX_API_KEY", "")
    api_secret = _resolve(ex.get("api_secret", "")) or os.environ.get("BITFINEX_API_SECRET", "")
    if not api_key or not api_secret:
        sys.exit("ERROR: API key/secret not found (config ${...} refs or "
                 "BITFINEX_API_KEY/SECRET env).")

    rest = BfxRest(api_key, api_secret)

    section("OPEN POSITIONS (fetch_positions)")
    try:
        pos = rest.get_positions()
        if not pos:
            print("  (none reported open)")
        for p in pos:
            print(f"  {p.symbol}: side={p.side} amount={p.amount} "
                  f"entry={p.entry_price} uPnL={p.unrealized_pnl} lev={p.leverage}")
    except Exception as e:  # noqa: BLE001
        print("  get_positions error:", repr(e))

    section("WALLET BALANCES (non-zero only)")
    try:
        for w in rest.get_wallets():
            if w.balance:
                print(f"  {w.wallet_type:8} {w.currency:6} balance={w.balance:.8f} "
                      f"avail={w.available:.8f}")
    except Exception as e:  # noqa: BLE001
        print("  get_wallets error:", repr(e))

    for sym in SYMS:
        section(f"RECENT ORDERS (history) — {sym}")
        try:
            # Reuse the BfxRest bfxapi client (same nonce source) for the one
            # read it doesn't wrap as a typed method.
            rows = rest._client.rest.auth.get_orders_history(
                symbol=symbols.to_bitfinex(sym), limit=args.limit)
            if not rows:
                print("  (none)")
            for o in rows:
                print(f"  id={getattr(o, 'id', '?')} "
                      f"amt_orig={getattr(o, 'amount_orig', '?')} "
                      f"type={getattr(o, 'order_type', '?')} "
                      f"status={getattr(o, 'order_status', '?')} "
                      f"avg={getattr(o, 'price_avg', '?')} "
                      f"mts={getattr(o, 'mts_update', '?')}")
        except Exception as e:  # noqa: BLE001
            print("  get_orders_history error:", repr(e))

    for sym in SYMS:
        section(f"RECENT TRADES (fills) — {sym}")
        try:
            fills = rest.get_trades(sym, limit=args.limit)
            if not fills:
                print("  (none)")
            for t in fills:
                print(f"  {t.ts} {t.side} amt={t.amount} price={t.price} "
                      f"fee={t.fee} {t.fee_currency or ''}")
        except Exception as e:  # noqa: BLE001
            print("  get_trades error:", repr(e))

    print("\nDONE (read-only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
