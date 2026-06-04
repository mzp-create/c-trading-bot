#!/usr/bin/env python3
"""Close all open positions on Bitfinex."""

import os
import sys
sys.path.insert(0, '/mnt/hermes-data/.hermes/hermes-agent/trading-bot')

# Load env vars manually
env_vars = {}
with open('.env', 'r') as f:
    for line in f:
        if '=' in line and not line.startswith('#'):
            # Remove 'export ' prefix if present
            line = line.strip()
            if line.startswith('export '):
                line = line[7:]
            key, val = line.split('=', 1)
            val = val.strip('"').strip("'")
            env_vars[key] = val
            os.environ[key] = val

from bitfinex import BitfinexClient

config = {
    'exchange': {
        'api_key': os.getenv('BITFINEX_API_KEY'),
        'api_secret': os.getenv('BITFINEX_API_SECRET'),
        'testnet': False,
        'default_type': 'margin',
    },
    'data': {
        'ohlcv_dir': 'data/ohlcv'
    },
    'trading': {
        'instance': 'close'
    }
}

client = BitfinexClient(config, mode='live', instance='close')

print("=" * 50)
print("CLOSING OPEN POSITIONS")
print("=" * 50)

# Fetch positions
positions = client.fetch_positions()
print(f"\nFound {len(positions)} open position(s)")

for p in positions:
    print(f"\n{p.symbol}: {p.side} {p.abs_amount}")

    # Determine close side (opposite of position)
    close_side = "sell" if p.side == "long" else "buy"

    # Get current price for informational display
    try:
        current_price = client.fetch_ticker(p.symbol).last
        print(f"  Current price: ${current_price}")
    except Exception as e:
        print(f"  ! Could not fetch ticker: {e}")

    # Close position — reduce_only so Bitfinex closes rather than opening
    # an opposing position.
    try:
        print(f"  Closing with {close_side} order...")
        order = client.create_order(
            p.symbol,
            close_side,
            p.abs_amount,
            order_type="market",
            reduce_only=True,
        )

        if order.is_filled:
            print(f"  CLOSED - Order ID: {order.id}")
        else:
            print(f"  FAILED: {order.status}")

    except Exception as e:
        print(f"  ERROR: {e}")

print("\n" + "=" * 50)
print("Done!")
print("=" * 50)
