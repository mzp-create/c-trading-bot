"""READ-ONLY deep probe. No orders placed."""
import os, sys, json, time
import ccxt

ex = ccxt.bitfinex({"apiKey": os.environ["BITFINEX_API_KEY"],
                    "secret": os.environ["BITFINEX_API_SECRET"],
                    "enableRateLimit": True,
                    "options": {"defaultType": "margin"}})

def section(t): print("\n" + "=" * 60 + "\n" + t + "\n" + "=" * 60)

section("ALL RECENT TRADES (no symbol filter, last ~20)")
try:
    tr = ex.fetch_my_trades(None, limit=20)
    if not tr: print("  (none)")
    for t in tr[-20:]:
        print(f"  {t.get('datetime')} {t.get('symbol')} {t.get('side')} "
              f"amt={t.get('amount')} px={t.get('price')} cost={t.get('cost')}")
except Exception as e:
    print("  error:", repr(e))

section("OPEN / ACTIVE ORDERS (any dangling from retry storm)")
try:
    oo = ex.fetch_open_orders()
    if not oo: print("  (none)")
    for o in oo:
        print(f"  {o.get('datetime')} {o.get('symbol')} {o.get('side')} "
              f"{o.get('type')} amt={o.get('amount')} status={o.get('status')} id={o.get('id')}")
except Exception as e:
    print("  error:", repr(e))

section("POSITIONS HISTORY (closed positions, if supported)")
try:
    if ex.has.get("fetchPositionsHistory"):
        ph = ex.fetch_positions_history(None, None, 20)
        if not ph: print("  (none)")
        for p in ph[-20:]:
            print(f"  {p.get('datetime')} {p.get('symbol')} side={p.get('side')} "
                  f"contracts={p.get('contracts')} entry={p.get('entryPrice')} "
                  f"realizedPnl={p.get('realizedPnl') or (p.get('info') or {})}")
    else:
        print("  fetchPositionsHistory not supported by ccxt for bitfinex")
except Exception as e:
    print("  error:", repr(e))

section("LEDGER (recent margin wallet movements, last ~15)")
try:
    if ex.has.get("fetchLedger"):
        led = ex.fetch_ledger(None, None, 15)
        for l in led[-15:]:
            print(f"  {l.get('datetime')} {l.get('type')} {l.get('currency')} "
                  f"amt={l.get('amount')} after={l.get('after')} {l.get('info','')[:0]}")
    else:
        print("  fetchLedger not supported")
except Exception as e:
    print("  error:", repr(e))

print("\nDONE (read-only).")
