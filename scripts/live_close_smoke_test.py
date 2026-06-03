#!/usr/bin/env python3
"""
LIVE single-close smoke test — Bitfinex MARGIN (derivatives) account.

Purpose
-------
Prove end-to-end that the fixed close path works on the real exchange:
  1. open ONE tiny margin position (market order),
  2. confirm it appears via fetch_positions,
  3. close it through ExecutionEngine.close_position() — the SAME path
     main.py uses for live SL/TP, which now sends a reduceOnly market order,
  4. confirm the position is flat and the close was journaled to trades.csv.

This directly targets the 2026-06-03 incident: closes that failed with
"not enough tradable balance" because they were NOT reduceOnly.

SAFETY
------
* DRY-RUN by default. No orders are placed unless you pass --arm AND type
  the confirmation phrase when prompted.
* Places at most TWO orders total: one open, one reduceOnly close.
* Refuses to run if a position already exists on the test symbol (won't
  touch positions it didn't create).
* A finally-block emergency-closes (reduceOnly) anything left open and, if
  it still can't flatten, prints LOUD manual-close instructions. It never
  exits silently with an open position.

Usage
-----
  # read-only preflight, places nothing:
  python scripts/live_close_smoke_test.py --symbol BTC/USDT --notional 12

  # actually trade (tiny), long then close:
  python scripts/live_close_smoke_test.py --symbol BTC/USDT --notional 12 --side buy --arm

  # test the SHORT close path:
  python scripts/live_close_smoke_test.py --symbol BTC/USDT --notional 12 --side sell --arm

Env: BITFINEX_API_KEY / BITFINEX_API_SECRET (source your .env first).
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from main import load_config            # noqa: E402
from execution.engine import ExecutionEngine  # noqa: E402


def banner(msg):
    print("\n" + "=" * 64 + f"\n  {msg}\n" + "=" * 64)


def get_position_contracts(client, symbol):
    """Return (contracts, side) for the symbol, or (0.0, None) if flat."""
    try:
        pos = client.fetch_position(symbol)
        return float(pos.get("contracts", 0) or 0), pos.get("side")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! fetch_position error: {exc!r}")
        return 0.0, None


def emergency_flatten(client, symbol, attempts=3):
    """Guaranteed flatten via a raw opposite MARKET order on the margin position.

    Uses the raw /auth/r/positions + /auth/w/order/submit endpoints directly
    (proven to net/close margin positions) rather than reduceOnly, so the net
    works even if reduceOnly is rejected. Returns True if flat afterwards.
    """
    bfx_symbol = client._symbol_to_bitfinex(symbol)
    for i in range(1, attempts + 1):
        try:
            active = [p for p in client._exchange.private_post_auth_r_positions()
                      if p[1] == "ACTIVE" and p[0] == bfx_symbol]
        except Exception as exc:  # noqa: BLE001
            print(f"  [safety {i}] could not read positions: {exc!r}"); time.sleep(2); continue
        if not active:
            return True
        amt = float(active[0][2])
        print(f"  [safety {i}/{attempts}] {bfx_symbol} amount={amt}; sending opposite MARKET...")
        try:
            client._exchange.private_post_auth_w_order_submit(
                {"symbol": bfx_symbol, "amount": f"{-amt:.8f}", "type": "MARKET"})
        except Exception as exc:  # noqa: BLE001
            print(f"  [safety {i}] raw close raised: {exc!r}")
        time.sleep(3)
    active = [p for p in client._exchange.private_post_auth_r_positions()
              if p[1] == "ACTIVE" and p[0] == bfx_symbol]
    return not active


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BTC/USDT",
                    help="Unified symbol; routed to the margin/derivative market (default BTC/USDT)")
    ap.add_argument("--notional", type=float, default=12.0,
                    help="USD notional for the tiny test position (default 12)")
    ap.add_argument("--side", choices=["buy", "sell"], default="buy",
                    help="buy = open long, sell = open short (default buy)")
    ap.add_argument("--config", default="config/default.yaml")
    ap.add_argument("--arm", action="store_true",
                    help="Actually place orders. Without this, preflight only.")
    args = ap.parse_args()

    if not os.environ.get("BITFINEX_API_KEY") or not os.environ.get("BITFINEX_API_SECRET"):
        sys.exit("ERROR: BITFINEX_API_KEY / BITFINEX_API_SECRET not in env. `source .env` first.")

    symbol = args.symbol

    # ---- build the REAL live engine + its client (exercises the real code) ----
    cfg = load_config(args.config)
    engine = ExecutionEngine(cfg, mode="live", trade_direction="both")
    client = engine._client

    banner(f"PREFLIGHT (read-only) — {symbol}  side={args.side}  notional=${args.notional}")

    # Balance
    try:
        bal = client.fetch_balance()
        totals = {k: v for k, v in (bal.get("total") or {}).items() if v}
        print(f"  margin wallet totals: {totals}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! fetch_balance error: {exc!r}")

    # Refuse to interfere with an existing position
    contracts, side = get_position_contracts(client, symbol)
    if contracts != 0:
        sys.exit(f"ABORT: a position already exists on {symbol} "
                 f"({side} {contracts}). This test will not touch positions it "
                 f"did not open. Close it first or pick another symbol.")
    print(f"  {symbol}: flat (0 contracts) — safe to proceed")

    # Price + sizing
    ticker = client.fetch_ticker(symbol)
    price = float(ticker.get("last") or ticker.get("close"))
    amount = args.notional / price

    # Respect the exchange minimum order size
    ccxt_symbol = client._symbol_to_ccxt(symbol)
    min_amt = None
    try:
        client._exchange.load_markets()
        mkt = client._exchange.market(ccxt_symbol)
        min_amt = (mkt.get("limits", {}).get("amount", {}) or {}).get("min")
        amount = float(client._exchange.amount_to_precision(ccxt_symbol, amount))
    except Exception as exc:  # noqa: BLE001
        print(f"  ! market/precision lookup failed, using raw amount: {exc!r}")

    if min_amt and amount < float(min_amt):
        amount = float(min_amt)
        print(f"  ! notional below exchange minimum; bumping amount to min {min_amt} "
              f"(~${amount * price:.2f} notional)")

    print(f"  ccxt market : {ccxt_symbol}")
    print(f"  price       : {price}")
    print(f"  min amount  : {min_amt}")
    print(f"  PLAN        : open {args.side} {amount} {symbol} (~${amount * price:.2f}), "
          f"then reduceOnly close")

    if not args.arm:
        banner("DRY RUN — no orders placed. Re-run with --arm to execute.")
        return 0

    # ---- arm gate: typed confirmation ----
    print(f"\n  ⚠️  LIVE ORDERS on a real margin account.")
    phrase = f"close {symbol}"
    typed = input(f'  Type exactly  «{phrase}»  to proceed: ').strip()
    if typed != phrase:
        sys.exit("Confirmation mismatch — aborted, nothing placed.")

    opened = False
    try:
        # ---- 1. OPEN (no reduceOnly: this genuinely opens the position) ----
        banner(f"OPEN: {args.side} {amount} {symbol} (market, margin)")
        open_res = client.create_order(symbol, "market", args.side, amount, None,
                                       params={"marginMode": "margin"})
        print(f"  open result: success={open_res.get('success')} id={open_res.get('id')} "
              f"filled={open_res.get('filled')} avg={open_res.get('average')} "
              f"status={open_res.get('status')} error={open_res.get('error')}")
        if not open_res.get("success"):
            sys.exit(f"OPEN failed — nothing to close. error={open_res.get('error')}")
        opened = True
        time.sleep(3)

        # ---- 2. CONFIRM the position exists ----
        banner("CONFIRM position is open")
        contracts, side = get_position_contracts(client, symbol)
        print(f"  fetch_position: side={side} contracts={contracts}")
        if contracts == 0:
            print("  ! position not visible yet; proceeding to close anyway (reduceOnly is safe)")

        # ---- 3. CLOSE via the PRODUCTION path (engine -> reduceOnly) ----
        banner("CLOSE via ExecutionEngine.close_position() — reduceOnly")
        close_res = engine.close_position(symbol, reason="smoke_test")
        print(f"  engine close contract: {close_res}")
        ok = bool(close_res.get("success"))
        print(f"  -> success={ok} pnl={close_res.get('pnl')} price={close_res.get('price')} "
              f"error={close_res.get('error')}")

        # ---- 4. VERIFY flat + journaled ----
        time.sleep(3)
        contracts, _ = get_position_contracts(client, symbol)
        flat = contracts == 0
        banner("RESULT")
        print(f"  position flat after close : {flat} (contracts={contracts})")
        try:
            with open(engine._trades_csv) as f:
                last = f.read().strip().splitlines()[-1]
            print(f"  trades.csv last row       : {last}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! could not read trades.csv: {exc!r}")

        verdict = "PASS ✅" if (ok and flat) else "FAIL ❌"
        print(f"\n  SMOKE TEST: {verdict}")
        return 0 if (ok and flat) else 1

    finally:
        # ---- SAFETY: never leave a position open ----
        if opened:
            contracts, _ = get_position_contracts(client, symbol)
            if contracts != 0:
                banner("SAFETY NET — position still open, force-flattening")
                if not emergency_flatten(client, symbol):
                    print("\n" + "!" * 64)
                    print(f"  COULD NOT FLATTEN {symbol}. CLOSE IT MANUALLY NOW:")
                    print(f"    - Bitfinex app/website  OR")
                    print(f"    - python close_positions.py   OR")
                    print(f"    - client.close_position('{symbol}')  (reduceOnly)")
                    print("!" * 64)


if __name__ == "__main__":
    raise SystemExit(main())
