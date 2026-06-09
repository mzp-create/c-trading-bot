# Exchange-native catastrophe-floor stop — design

**Date:** 2026-06-09
**Status:** DEPLOYED LIVE 2026-06-09 (master @ a792f97). Implemented via plan `docs/superpowers/plans/2026-06-09-exchange-catastrophe-stop.md`; 195 tests pass; live-verified — reconcile placed reduce-only stops for all 3 positions (ETH/BTC shorts above entry, SOL long below), and a second restart adopted them with no duplication, 0 nonce errors.
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

## Risks / unknowns — RESOLVED by live test (2026-06-09, LONG key, bot paused)
- **bfxapi STOP semantics on margin pairs:** CONFIRMED. `auth.submit_order(type="STOP",
  symbol="tSOLUST", amount="-1.92045709", price="<trigger>", flags=1024)` →
  `order_status=ACTIVE`, `order_type="STOP"`, trigger price echoed in the **`price`**
  field. So: type string is **`"STOP"`**, trigger goes in **`price`**, signed amount
  sets direction.
- **reduce_only honored:** CONFIRMED. `flags=1024` (REDUCE_ONLY) echoed on the resting
  order; `get_orders()` shows `reduce_only=True`.
- **Ack parseability:** BETTER THAN MARKET. The STOP submit ack parsed cleanly —
  `notif.status="SUCCESS"`, `notif.data.id=238683931018`, `order_status="ACTIVE"`. We
  get the id **inline** (no open-orders fetch needed on the happy path). Keep the
  open-orders match as a fallback for robustness/reconciliation.
- **cancel_order:** CONFIRMED. `auth.cancel_order(id=...)` → `status="SUCCESS"`; order
  gone from `get_orders()`.
- **WS vs REST:** REST path probed and confirmed. WS path (`ws_feed._send_submit`) must
  map STOP identically in implementation; covered by Phase A unit tests + the existing
  WS-healthy routing.
- **Reads for reconciliation:** `auth.get_orders()` returns active orders with `.id`,
  `.symbol`, `.order_type`, `.amount_orig`, `.price`, `.flags`, `.order_status` — enough
  to adopt/match/clean up resting stops on restart.

**Bonus finding:** the exchange-reported SOL entry was **65.614**, vs our DB's stale
**66.857** — independent confirmation of the stale-anchor bug fixed earlier today
(commit 3616c43). Validates that fix.

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
