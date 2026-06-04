#!/usr/bin/env python3
"""READ-ONLY live check: does bfxapi report our (margin/short) positions?

Places NO orders. Use this to decide whether to flip
`exchange.trust_exchange_positions` to true (dropping the RiskState union).

    python scripts/check_live_positions.py --config config/long.yaml
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from bitfinex import BitfinexClient


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/long.yaml")
    args = p.parse_args()
    cfg = yaml.safe_load(open(args.config))
    # Force REST-only read (no WS) for a clean snapshot.
    cfg.setdefault("exchange", {}).setdefault("ws", {})["enabled"] = False
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
