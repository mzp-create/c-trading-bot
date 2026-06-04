#!/usr/bin/env python3
"""READ-ONLY live check: does bfxapi report our (margin/short) positions?

Places NO orders. Use this to decide whether to flip
`exchange.trust_exchange_positions` to true (dropping the RiskState union).

    python scripts/check_live_positions.py --config config/long.yaml
"""

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from bitfinex import BitfinexClient

_ENV_REF = re.compile(r"\$\{([^}]+)\}")


def _resolve_env(obj):
    """Expand ${VAR} references (like the bot's load_config) so api_key/secret
    come from the environment instead of being passed as literal strings."""
    if isinstance(obj, dict):
        return {k: _resolve_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env(v) for v in obj]
    if isinstance(obj, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), obj)
    return obj


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/long.yaml")
    args = p.parse_args()
    cfg = _resolve_env(yaml.safe_load(open(args.config)))
    ex = cfg.setdefault("exchange", {})
    if not ex.get("api_key") or not ex.get("api_secret"):
        sys.exit("BITFINEX_API_KEY / BITFINEX_API_SECRET not set in the "
                 "environment (or the config's ${...} refs resolved empty).")
    # Force REST-only read (no WS) for a clean snapshot.
    ex.setdefault("ws", {})["enabled"] = False
    client = BitfinexClient(cfg, mode="live", instance="check")
    positions = client.fetch_positions()        # READ-ONLY
    print(f"fetch_positions() returned {len(positions)} position(s):")
    for pos in positions:
        print(f"  {pos.symbol}  side={pos.side}  amount={pos.amount}  "
              f"entry={pos.entry_price}")
    longs = [p for p in positions if p.side == "long"]
    shorts = [p for p in positions if p.side == "short"]
    print(f"\nlongs={len(longs)} shorts={len(shorts)}")
    print("If your open SHORTS appear above, it is safe to set "
          "exchange.trust_exchange_positions: true.")
    client.close()


if __name__ == "__main__":
    main()
