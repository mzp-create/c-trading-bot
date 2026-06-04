"""READ-ONLY Bitfinex reconciliation. Places NO orders. Fetches positions,
balances, recent closed orders and trades to compare against bot logs."""
import os, sys, json
import ccxt

key = os.environ.get("BITFINEX_API_KEY")
secret = os.environ.get("BITFINEX_API_SECRET")
if not key or not secret:
    print("ERROR: API key/secret not in env"); sys.exit(1)

ex = ccxt.bitfinex({"apiKey": key, "secret": secret, "enableRateLimit": True,
                    "options": {"defaultType": "margin"}})

SYMS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]

def section(t): print("\n" + "=" * 60 + "\n" + t + "\n" + "=" * 60)

section("OPEN DERIVATIVE POSITIONS (fetch_positions)")
try:
    pos = ex.fetch_positions()
    live = [p for p in pos if p.get("contracts")]
    if not live:
        print("  (none reported open)")
    for p in live:
        print(f"  {p.get('symbol')}: side={p.get('side')} contracts={p.get('contracts')} "
              f"entry={p.get('entryPrice')} uPnL={p.get('unrealizedPnl')} lev={p.get('leverage')}")
except Exception as e:
    print("  fetch_positions error:", repr(e))

section("MARGIN / DERIVATIVES BALANCE (non-zero only)")
try:
    bal = ex.fetch_balance()
    tot = {k: v for k, v in bal.get("total", {}).items() if v}
    print("  totals:", json.dumps(tot))
except Exception as e:
    print("  fetch_balance error:", repr(e))

for sym in SYMS:
    section(f"RECENT CLOSED ORDERS — {sym}")
    try:
        orders = ex.fetch_closed_orders(sym, limit=10)
        if not orders:
            print("  (none)")
        for o in orders[-10:]:
            print(f"  {o.get('datetime')} {o.get('side')} {o.get('type')} "
                  f"amt={o.get('amount')} filled={o.get('filled')} "
                  f"avg={o.get('average')} status={o.get('status')} id={o.get('id')}")
    except Exception as e:
        print("  fetch_closed_orders error:", repr(e))

for sym in SYMS:
    section(f"RECENT TRADES (fills) — {sym}")
    try:
        trades = ex.fetch_my_trades(sym, limit=10)
        if not trades:
            print("  (none)")
        for t in trades[-10:]:
            print(f"  {t.get('datetime')} {t.get('side')} amt={t.get('amount')} "
                  f"price={t.get('price')} cost={t.get('cost')} fee={t.get('fee')}")
    except Exception as e:
        print("  fetch_my_trades error:", repr(e))

print("\nDONE (read-only).")
