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

from market_data.bitfinex_client import BitfinexClient

config = {
    'exchange': {
        'api_key': os.getenv('BITFINEX_API_KEY'),
        'api_secret': os.getenv('BITFINEX_API_SECRET'),
        'testnet': False,
        'default_type': 'margin',
        'nonce_file': 'data/.bfx_nonce_shared'
    },
    'data': {
        'ohlcv_dir': 'data/ohlcv'
    },
    'trading': {
        'instance': 'close'
    }
}

client = BitfinexClient(config, mode='live')

print("=" * 50)
print("CLOSING OPEN POSITIONS")
print("=" * 50)

# Fetch positions
positions = client.fetch_positions()
print(f"\nFound {len(positions)} open position(s)")

for pos in positions:
    symbol = pos.get('symbol')
    side = pos.get('side')
    amount = pos.get('amount', 0)
    
    print(f"\n{symbol}: {side} {abs(amount)}")
    
    # Determine close side (opposite of position)
    close_side = "buy" if side == "short" else "sell"
    
    # Get current price
    try:
        ticker = client._exchange.fetch_ticker(symbol)
        current_price = ticker['last']
        print(f"  Current price: ${current_price}")
        
        # Close position — reduceOnly so Bitfinex closes rather than opening
        # an opposing position. Symbol from fetch_positions() is already the
        # unified CCXT form (e.g. "BTC/USDT") the client expects.
        print(f"  Closing with {close_side} order...")
        result = client.create_order(
            symbol,
            "market",
            close_side,
            abs(amount),
            None,
            {"reduceOnly": True},
        )

        if result["success"]:
            print(f"  ✅ CLOSED - Order ID: {result['id']}")
        else:
            print(f"  ❌ FAILED: {result['error']}")
            
    except Exception as e:
        print(f"  ❌ ERROR: {e}")

print("\n" + "=" * 50)
print("Done!")
print("=" * 50)
