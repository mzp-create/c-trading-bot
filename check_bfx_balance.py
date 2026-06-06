#!/usr/bin/env python3
"""Simple Bitfinex balance check using REST API directly."""
import os
import sys
_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

# Load env
env_path = os.path.join(_ROOT, '.env')
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:]
        if '=' in line:
            key, val = line.split('=', 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val

from bfxapi import Client

REST_HOST = "https://api.bitfinex.com"

# Use LONG API key
api_key = os.environ.get('BITFINEX_LONG_API_KEY', '')
api_secret = os.environ.get('BITFINEX_LONG_API_SECRET', '')

if not api_key or not api_secret:
    print("API keys not found in environment")
    sys.exit(1)

# Create client
client = Client(api_key=api_key, api_secret=api_secret, rest_host=REST_HOST)

print("=== Bitfinex Live Balance ===\n")

# Get wallets
wallets = client.rest.auth.get_wallets()
total_usd = 0
for w in wallets:
    currency = w.currency.upper()
    balance = float(w.balance)
    available = float(getattr(w, 'available_balance', w.balance))
    wallet_type = w.wallet_type
    
    print(f"{currency}: {balance:.6f} (avail: {available:.6f}) [{wallet_type}]")
    
    if currency == 'USD' or currency == 'USDT':
        total_usd += balance

print(f"\nTotal USD/USDT: ${total_usd:.2f}")

# Get positions
print("\n=== Open Positions ===")
positions = client.rest.auth.get_positions()
if positions:
    for p in positions:
        symbol = p.symbol
        amount = float(p.amount)
        entry = float(getattr(p, 'base_price', 0))
        pnl = float(getattr(p, 'pl', 0))
        print(f"{symbol}: {amount:.6f} @ ${entry:.2f} | PnL: ${pnl:.2f}")
else:
    print("No open positions")
