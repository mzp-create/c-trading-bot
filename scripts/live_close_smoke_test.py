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
* A finally-block emergency-closes anything left open and, if it still
  can't flatten, prints LOUD manual-close instructions. It never exits
  silently with an open position.

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
    """Return (abs_amount, side) for the symbol, or (0.0, None) if flat."""
    try:
        pos = client.fetch_position(symbol)
        if pos is None:
            return 0.0, None
        return pos.abs_amount, pos.side
    except Exception as exc:  # noqa: BLE001
        print(f"  ! fetch_position error: {exc!r}")
        return 0.0, None


def emergency_flatten(client, symbol, attempts=3):
    """Guaranteed flatten via a typed opposite MARKET order on the margin position.

    Sends a reduce_only market order in the opposite direction. Returns True
    if the position is flat afterwards.
    """
    for i in range(1, attempts + 1):
        contracts, side = get_position_contracts(client, symbol)
        if contracts == 0:
            return True
        close_side = "sell" if side == "long" else "buy"
        print(f"  [safety {i}/{attempts}] {symbol} contracts={contracts} side={side}; "
              f"sending {close_side} MARKET reduce_only...")
        try:
            client.create_order(symbol, close_side, contracts,
                                order_type="market", reduce_only=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  [safety {i}] raw close raised: {exc!r}")
        time.sleep(3)
    contracts, _ = get_position_contracts(client, symbol)
    return contracts == 0


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
    ap.add_argument("--require-ws", action="store_true",
                    help="Wait for the WS feed to be healthy before proceeding, "
                         "so orders route over WebSocket (Phase-3 gate). Aborts "
                         "if the feed does not authenticate in time.")
    args = ap.parse_args()

    if not os.environ.get("BITFINEX_API_KEY") or not os.environ.get("BITFINEX_API_SECRET"):
        sys.exit("ERROR: BITFINEX_API_KEY / BITFINEX_API_SECRET not in env. `source .env` first.")

    symbol = args.symbol

    # ---- build the REAL live engine + its client (exercises the real code) ----
    cfg = load_config(args.config)
    engine = ExecutionEngine(cfg, mode="live", trade_direction="both")
    client = engine._client

    if args.require_ws:
        banner("WAIT FOR WS FEED (orders will route over WebSocket)")
        feed = getattr(client, "_feed", None)
        if feed is None:
            sys.exit("ABORT: --require-ws but no WS feed (is exchange.ws.enabled "
                     "true and mode live?).")
        deadline = time.time() + 20
        while time.time() < deadline and not feed.is_healthy():
            time.sleep(0.5)
        acct = getattr(client, "_account", None)
        print(f"  feed.is_healthy()={feed.is_healthy()} "
              f"connected={getattr(acct, 'connected', '?')} "
              f"authenticated={getattr(acct, 'authenticated', '?')}")
        if not feed.is_healthy():
            sys.exit("ABORT: WS feed did not become healthy within 20s — would "
                     "fall back to REST, not testing the WS path. Nothing placed.")
        print("  WS feed healthy — orders will route over WebSocket.")

    banner(f"PREFLIGHT (read-only) — {symbol}  side={args.side}  notional=${args.notional}")

    # Balance
    try:
        wallets = client.fetch_balance()
        totals = {w.currency: w.balance for w in wallets if w.balance}
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
    price = float(ticker.last)
    amount = args.notional / price

    print(f"  price       : {price}")
    print(f"  PLAN        : open {args.side} {amount:.8f} {symbol} (~${amount * price:.2f}), "
          f"then reduceOnly close")

    if not args.arm:
        banner("DRY RUN — no orders placed. Re-run with --arm to execute.")
        return 0

    # ---- arm gate: typed confirmation ----
    print(f"\n  WARNING: LIVE ORDERS on a real margin account.")
    phrase = f"close {symbol}"
    typed = input(f'  Type exactly  [{phrase}]  to proceed: ').strip()
    if typed != phrase:
        sys.exit("Confirmation mismatch — aborted, nothing placed.")

    opened = False
    try:
        # ---- 1. OPEN (no reduce_only: this genuinely opens the position) ----
        banner(f"OPEN: {args.side} {amount:.8f} {symbol} (market, margin)")
        # Arm the safety net BEFORE sending — a WS submit can raise AckUnparseable
        # with the order possibly already on the exchange. The finally block then
        # always verifies positions and force-flattens.
        opened = True
        open_order = client.create_order(symbol, args.side, amount,
                                         order_type="market", reduce_only=False)
        print(f"  open result: is_accepted={open_order.is_accepted} "
              f"is_filled={open_order.is_filled} id={open_order.id} "
              f"filled={open_order.filled} avg={open_order.avg_price} "
              f"status={open_order.status}")
        # A market order acked ACTIVE (real id, not rejected) WILL fill — gate on
        # is_accepted, NOT is_filled (which requires the EXECUTED status that the
        # immediate ack does not carry).
        if not open_order.is_accepted:
            sys.exit(f"OPEN rejected — nothing to close. status={open_order.status}")
        time.sleep(3)

        # ---- 2. CONFIRM the position exists ----
        banner("CONFIRM position is open")
        contracts, side = get_position_contracts(client, symbol)
        print(f"  fetch_position: side={side} contracts={contracts}")
        if contracts == 0:
            print("  ! position not visible yet; proceeding to close anyway (reduce_only is safe)")

        # ---- 3. CLOSE via the PRODUCTION path (engine -> reduceOnly) ----
        banner("CLOSE via ExecutionEngine.close_position() — reduceOnly")
        close_res = engine.close_position(symbol, reason="smoke_test")
        print(f"  engine close result: {close_res}")
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
            recent = engine._repo.recent_trades(limit=1)
            if recent:
                t = recent[0]
                print(f"  journaled: {t.symbol} {t.side} pnl={t.pnl} @ {t.ts}")
            else:
                print("  (no journaled trade found)")
        except Exception as exc:  # noqa: BLE001
            print(f"  (journal check skipped: {exc})")

        verdict = "PASS" if (ok and flat) else "FAIL"
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
                    print(f"    - client.close_position('{symbol}')  (reduce_only)")
                    print("!" * 64)


if __name__ == "__main__":
    raise SystemExit(main())
