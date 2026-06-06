#!/usr/bin/env python3
"""Check Bitfinex balance and positions."""
from bitfinex.client import BitfinexClient
import yaml

def main():
    # Load config
    with open('config/short.yaml') as f:
        config = yaml.safe_load(f)

    # Create client in live mode
    client = BitfinexClient(config, mode='live', instance='short')

    # Get wallets
    wallets = client.fetch_balance()
    print('=== Bitfinex Wallet Balances ===')
    total_usd = 0
    for w in wallets:
        print(f'{w.currency}: {w.balance:.6f} (available: {w.available:.6f}) [type: {w.wallet_type}]')
        if w.currency == 'USDT':
            total_usd = w.balance

    # Get positions
    positions = client.fetch_positions()
    print(f'\n=== Open Positions ({len(positions)}) ===')
    for p in positions:
        entry = p.entry_price if p.entry_price is not None else 0.0
        print(f'{p.symbol}: {p.amount:.6f} @ entry ${entry:.2f} | PnL: ${p.unrealized_pnl:.2f}')

    print(f'\nTotal USDT: ${total_usd:.2f}')

if __name__ == "__main__":
    main()
