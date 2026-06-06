#!/usr/bin/env python3
"""READ-ONLY live Bitfinex balance + positions, via the bot's BfxRest.

Goes through `bitfinex.rest.BfxRest` (the same authenticated path the live bot
uses) rather than a bare bfxapi client. This fixes two bugs the old version had:
  * it hardcoded REST_HOST="https://api.bitfinex.com" (missing the "/v2"), so
    auth/r/wallets hit the wrong path and returned an empty body (JSONDecodeError);
  * independent clients on the shared key collided on nonces ("nonce: small").
Using the bot's BfxRest gives the correct host and a single nonce source.

Places NO orders.  Usage:  python check_bfx_balance.py [--config config/long.yaml]
"""
import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
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
from bitfinex.rest import BfxRest  # noqa: E402

_REF = re.compile(r"\$\{([^}]+)\}")


def _resolve(s: str) -> str:
    return _REF.sub(lambda m: os.environ.get(m.group(1), ""), s or "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/long.yaml",
                    help="Config whose exchange.api_key/secret (${...} refs) to use.")
    args = ap.parse_args()

    ex = (yaml.safe_load(open(ROOT / args.config)) or {}).get("exchange", {})
    api_key = _resolve(ex.get("api_key", "")) or os.environ.get("BITFINEX_API_KEY", "")
    api_secret = _resolve(ex.get("api_secret", "")) or os.environ.get("BITFINEX_API_SECRET", "")
    if not api_key or not api_secret:
        sys.exit("ERROR: API key/secret not found (config ${...} refs or "
                 "BITFINEX_API_KEY/SECRET env).")

    rest = BfxRest(api_key, api_secret)

    print("=== Bitfinex Live Balance (BfxRest) ===\n")
    total_usd = 0.0
    for w in rest.get_wallets():
        print(f"  {w.wallet_type:8} {w.currency:6} balance={w.balance:.6f} "
              f"(avail: {w.available:.6f})")
        if w.currency.upper() in ("USD", "USDT", "UST"):
            total_usd += w.balance
    print(f"\nTotal USD/USDT: ${total_usd:.2f}")

    print("\n=== Open Positions ===")
    positions = rest.get_positions()
    if positions:
        for p in positions:
            print(f"  {p.symbol}: {p.amount:.8f} @ ${p.entry_price:.2f} "
                  f"| side={p.side} | uPnL: ${p.unrealized_pnl:.2f}")
    else:
        print("  No open positions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
