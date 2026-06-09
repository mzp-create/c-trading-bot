# Exchange-native catastrophe-floor stop — design

**Date:** 2026-06-09
**Status:** approved (design), live-test pending
**Author:** Zeya Phyo (with Claude)

## Problem

Stop-losses are "soft" (bot-monitored): `TradingBot._check_positions` checks each
open position once per main-loop cycle (`sleep` 30–300 s) against the live ticker,
and on breach sends a **market** close. Two slippage sources, plus a third failure
mode:

1. **Monitoring latency** — up to 5 min between checks; price can run far past the
   stop before the bot looks.
2. **Market fill past the level** — the close fills at wherever the market is by
   then; on a fast move that is well past the stop.
3. **Bot-down exposure** — a bot-side stop cannot fire at all while the bot is not
   running.

Real impact (2026-06-07): two shorts inherited from a prior bot instance (opened
06:00, bot down until ~19:02) were stopped out on a vertical 22:00 spike, filling
~2× past their stop level. Net realized −$20.67 of the −$21.28 lifetime loss.

## Goal

Cap worst-case loss near the intended stop level even when the bot is down, slow,
or price gaps — with the **minimum** new surface on the (fragile) order path.

## Approach (chosen)

When a position opens, place **one reduce-only exchange-native STOP** (market-stop)
order resting at the **initial** stop level. Cancel it whenever the position closes
by any path. Keep ALL existing bot-side exit logic unchanged — trailing stop,
take-profit, signal exits, and the bot-side stop check — as the primary exit path
and a backstop. The exchange stop only bites when the bot-side path fails (down,
slow, or gap).

Rejected alternatives: stop+trailing on exchange (order churn → nonce-pressure
risk) and full OCO stop+TP (largest change to the order path). Both deferred.

## Critical safety invariant

The resting stop is **`reduce_only`**. A reduce-only order with no position to
reduce is rejected/ignored by the exchange, so a stale or orphaned stop can **never**
open a new position in the wrong direction. This is what makes a server-side resting
stop safe to leave in place across restarts and bot-side closes.

## Components

### 1. Order layer — add STOP type + cancel
Files: `bitfinex/rest.py`, `bitfinex/ws_feed.py`, `bitfinex/paper.py`, `bitfinex/client.py`

- Today `rest.py:34` and `ws_feed.py:350` hardcode `bfx_type = "MARKET" if ... else "LIMIT"`.
  Extend the mapping so `order_type="stop"` → Bitfinex margin **`STOP`**, with the
  `price` field carrying the **trigger** price and `reduce_only=True` honored.
- Add `cancel_order(order_id)` to the client + REST/WS layers (only `close_position`
  exists today).
- Paper engine: simulate a resting stop — record it on submit, trigger a reduce-only
  market close when a later cycle's price crosses the trigger, and honor cancel.
  Keeps paper/live parity for the lifecycle logic.

### 2. Stop lifecycle (`execution/engine.py`)
- **On entry fill:** after the position is recorded, place a reduce-only STOP at the
  position's `stop_loss` price; store the returned exchange id as `stop_order_id` on
  the position dict. If the ack does not yield an id (see Risks), resolve it by
  fetching open orders and matching (symbol, side, reduce_only, trigger≈stop_loss).
- **On any close path** (bot-side SL/TP, signal exit, daily-loss breaker,
  `close_all_positions`): cancel `stop_order_id` if present, tolerating
  "already filled/triggered/unknown" without raising.

### 3. Reconciliation on restart (`ExecutionEngine` position sync)
- Fetch open orders. For each reloaded position: adopt an existing matching
  reduce-only stop (store its id, do not duplicate); place one if missing.
- Cancel orphaned reduce-only stops that match no open position (defensive cleanup;
  reduce_only already makes them harmless).

## Data flow

```
entry fills ──▶ record position ──▶ place reduce_only STOP @ stop_loss ──▶ store stop_order_id
                                                                                │
open position rests; exchange watches trigger server-side ◀─────────────────────┘
                                                                                │
close (any path) ──▶ cancel stop_order_id ──▶ send reduce_only market close
restart ──▶ position sync ──▶ adopt/place stop per position ; cancel orphans
```

## Backstop / interaction
`_check_positions` is unchanged and still does bot-side SL/TP market closes. If the
exchange stop has already triggered, the position is gone: the bot-side close finds
no position (no-op) and the cancel is a no-op. `reduce_only` on both sides prevents
any double-close from opening exposure.

## Risks / unknowns (resolve via live test before deploy)
- **bfxapi STOP semantics on margin pairs (tBTCUST/tETHUST/tSOLUST):** exact
  order-type string, which field carries the trigger price, and whether `reduce_only`
  is honored for STOP. *(LIVE-TEST RESULT: TBD)*
- **Ack parseability:** prior notes (`bitfinex-onreq-unparsed`, `bfxapi-rest-market-ack-active`)
  show market acks don't always parse to an id. If STOP acks are the same, the
  lifecycle must resolve the id via open-orders fetch. *(LIVE-TEST RESULT: TBD)*
- **WS vs REST submission:** `create_order` uses the WS path when healthy, else REST.
  Both must map STOP identically. *(LIVE-TEST RESULT: TBD — REST path probed)*
- **cancel_order ack shape.** *(LIVE-TEST RESULT: TBD)*

## Live test (gated, manual, one-off)
Probe the real LONG key/account against an existing position, during a brief bot
pause (positions rest on the exchange; same exposure window as a normal restart):

1. SIGKILL the running bot (preserve positions).
2. Standalone probe (LONG key, REST, single client → no nonce competition):
   place a reduce-only SELL STOP on the SOL long, trigger **far below** market (~50
   when market ~66 → cannot trigger), amount = position size.
3. Confirm: order accepted, rests (fetch open orders), `reduce_only` echoed, id
   obtainable. Record the ack/order shape and exact type string.
4. **Cancel** the test order (try/finally) and verify it is gone.
5. Restart the bot; confirm clean position sync, 0 nonce errors.

Record findings in memory and fold the confirmed semantics back into this spec
before implementation.

## Testing (implementation)
- Unit: order layer maps `stop` → correct bfx type + trigger price + reduce_only;
  paper simulates a resting stop (rests, triggers on cross, cancels); lifecycle
  places-on-open / cancels-on-close / reconciles (adopt vs place vs orphan-cleanup)
  with a mocked client.
- Gated live smoke test (above) before the live deploy.

## Phasing
- **A:** order-layer STOP + cancel_order + unit tests.
- **B:** lifecycle manager (place/cancel/reconcile) + unit tests.
- **C:** gated live verification, then SIGKILL-safe deploy.

## Out of scope
Exchange-side trailing stop, exchange-side take-profit / OCO, changing the bot-side
exit logic or the main-loop cadence.
