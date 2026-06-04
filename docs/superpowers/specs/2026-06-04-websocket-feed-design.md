# Design: Trading Architecture Refactor — Phase 3, WebSocket Feed + State Store

Date: 2026-06-04
Status: Approved (design) — pending written-spec review
Branch: refactor/trading-architecture
Predecessors: Phase 1 persistence (implemented), Phase 2 bitfinex package (implemented, pending live gate)

## 1. Background & motivation

The bot is single-threaded with a ~60s adaptive cycle (`main.py:run`). Every cycle
it polls Bitfinex REST: one `fetch_positions` (`engine._update_position_cache`)
plus a `fetch_ticker` per analyzed symbol and per open position
(`collector.get_current_price`, `engine._current_price`) — roughly **7 REST
reads per cycle**. That per-cycle authenticated polling is the remaining source
of nonce/REST pressure (the 2026-06-03 `nonce: small` storms). Order placement,
by contrast, is infrequent.

bfxapi v4.0.0 ships a full asyncio WebSocket client (`bfx.wss`) with auto-reconnect,
push channels for ticker and the authenticated streams (positions, wallets,
orders, trade executions — the **same typed dataclasses** the REST layer returns),
and order placement over WS (`bfx.wss.inputs.submit_order`). The WS and REST
clients coexist on one `Client`.

This phase moves the data **reads** to a push-maintained in-memory state store
and moves order **placement** to WS, while keeping the Phase-2 bfxapi **REST path
as a transparent fallback**. The synchronous engine and all other callers keep
using the same `BitfinexClient` methods; all WebSocket/threading complexity lives
behind the client.

## 2. Decisions (resolved during brainstorming, 2026-06-04)

- **Scope:** WS **data feed** (ticker + auth positions/wallets/orders/trades →
  push-maintained state) **and** order **placement over WS**.
- **Fallback:** **WS-first, REST-fallback, keep trading.** When the feed is
  disconnected or a datum is stale, reads and order placement transparently use
  the Phase-2 `BfxRest` path. WS is an optimization; REST is the safety net.
- **Reconnect:** REST-reconcile the snapshot gap after every reconnect (bfxapi
  snapshots fire only once per connection).
- **Integration:** **transparent WS-or-REST routing inside `BitfinexClient`** —
  a new `state/` package + `bitfinex/ws_feed.py` (background asyncio thread); the
  engine and consumers stay essentially unchanged (they keep the cycle model).
- **Order safety preserved:** a WS submit whose confirmation does not arrive in
  time → `AckUnparseable` ("outcome unknown — do NOT auto-retry"; reconcile via
  the next position read). Never silently REST-resubmit a WS order.

**Out of scope (later phases):** engine slim-down / event-driven engine (Phase 4).
OHLCV stays ccxt/REST (public candles). Paper mode is unchanged (no feed).

## 3. Components & layout

```
state/                       (new; thread-safe, framework-free)
  __init__.py
  market_state.py   MarketState   — per-symbol latest Ticker + monotonic update ts
  account_state.py  AccountState  — positions{symbol->Position}, wallets{ccy->Wallet},
                                    recent fills{symbol->Fill}; connection flags + last_reconcile ts

bitfinex/
  ws_feed.py        WsFeed        — background daemon thread running its own asyncio loop
                                    (asyncio.run(bfx.wss.start())); registers handlers that push
                                    into MarketState/AccountState; submit_order_sync / cancel_order_sync
                                    via run_coroutine_threadsafe + cid-correlated confirmation.
  client.py         BitfinexClient (live) composes WsFeed + BfxRest + the two state objects and
                                    routes WS-first / REST-fallback. Paper mode unchanged.
```

`MarketState` and `AccountState` have one clear responsibility each, no I/O, and
are unit-testable in isolation. `WsFeed` owns all threading/asyncio. The engine,
collector, and telegram are unaware of WS — they call the same client methods.

## 4. State store (`state/`)

### MarketState (`market_state.py`)
Thread-safe (one `threading.Lock`). Holds the latest `Ticker` per display symbol
with a monotonic timestamp (`time.monotonic()`).

```
class MarketState:
    def update_ticker(self, ticker: Ticker) -> None        # WS writes
    def get_ticker(self, symbol: str) -> Optional[Ticker]  # reader (returns the frozen Ticker)
    def age(self, symbol: str) -> Optional[float]          # seconds since last update, or None
    def is_fresh(self, symbol: str, max_age: float) -> bool
```

### AccountState (`account_state.py`)
Thread-safe. Authoritative when the feed is authenticated.

```
class AccountState:
    # status
    connected: bool        authenticated: bool        last_reconcile: float|None
    def set_status(self, *, connected: bool, authenticated: bool) -> None

    # positions (display symbol keyed; absent key => flat)
    def apply_position_snapshot(self, positions: list[Position]) -> None   # replace all
    def apply_position(self, position: Position) -> None                   # pn/pu: upsert; pc or amount==0 => remove
    def get_positions(self) -> list[Position]
    def get_position(self, symbol: str) -> Optional[Position]

    # wallets (currency keyed; display currency, e.g. USDT)
    def apply_wallet_snapshot(self, wallets: list[Wallet]) -> None
    def apply_wallet(self, wallet: Wallet) -> None
    def get_wallets(self) -> list[Wallet]

    # fills (most-recent per symbol, for fee enrichment)
    def apply_fill(self, fill: Fill) -> None
    def last_fill(self, symbol: str) -> Optional[Fill]
```

Models are the Phase-2 frozen `Order`/`Position`/`Ticker`/`Wallet`/`Fill`. WS
dataclasses (`bfxapi.types.*`) are mapped into these by `ws_feed` (reusing the
mapping helpers already in `bitfinex/rest.py` where practical — e.g. currency
`UST→USDT`, symbol `to_display`, signed-amount → side). Positions/wallets/fills
in state are display-normalized exactly like the REST path.

## 5. WS feed (`bitfinex/ws_feed.py`)

`WsFeed(api_key, api_secret, symbols, market_state, account_state, rest,
config)`:

- **Thread/loop.** A daemon `threading.Thread` creates a fresh event loop
  (`asyncio.new_event_loop()`, `set_event_loop`, `run_until_complete(bfx.wss.start())`).
  The loop reference is captured so the sync thread can schedule coroutines via
  `asyncio.run_coroutine_threadsafe`. `bfx` is `Client(api_key, api_secret,
  wss_host=...)`. (An injected `bfx` client is accepted for tests.)
- **Handlers (registered before start):**
  - `open` → `account_state.set_status(connected=True, ...)`; subscribe a ticker
    channel per configured symbol (`to_bitfinex`).
  - `authenticated` → mark authenticated; **if this is a re-auth (not the first),
    schedule a REST reconcile** (§7).
  - `t_ticker_update(sub, ticker)` → `market_state.update_ticker(Ticker(...))`.
  - `position_snapshot` → `apply_position_snapshot`; `position_new`/`position_update`
    → `apply_position`; `position_close` → remove.
  - `wallet_snapshot` → `apply_wallet_snapshot`; `wallet_update` → `apply_wallet`.
  - `trade_execution`/`trade_execution_update` → `apply_fill` (carries fee/fee_currency).
  - `on-req-notification` (and `order_new`/`order_cancel` as fallback) → resolve a
    pending order future by `cid` (§6).
  - `disconnected` → `set_status(connected=False, authenticated=False)`.
  - Every handler body is wrapped so a handler exception is logged and never
    propagates into the loop.
- **Lifecycle:** `start()` spawns the thread; `stop()` schedules `bfx.wss.close()`
  on the loop, stops the loop, and joins the thread with a timeout. `is_healthy()
  -> bool` = connected and authenticated.
- **Periodic reconcile:** a loop task every `reconcile_interval_seconds` runs a
  REST reconcile as a backstop (§7).

### submit_order_sync / cancel_order_sync
```
def submit_order_sync(self, symbol, side, amount, *, order_type="market",
                      price=None, reduce_only=False, timeout=...) -> Order
```
1. Generate `cid` (monotonic int); register `{cid: (threading.Event, holder)}`
   under a lock.
2. Map to bfxapi inputs exactly as the REST path does (margin `MARKET`/`LIMIT`,
   signed amount string, `flags=1024` if reduce-only) and schedule
   `bfx.wss.inputs.submit_order(..., cid=cid)` on the loop.
3. Wait on the Event up to `timeout`.
4. The `on-req-notification` handler matches `cid`: `ERROR` → holder gets
   `OrderRejected(text)`; `SUCCESS` → holder gets the mapped typed `Order`; set
   the Event.
5. Return the `Order`, or raise `OrderRejected`, or — on timeout — raise
   `AckUnparseable`. Always reap the pending entry.

`cancel_order_sync(order_id, timeout)` uses the same machinery with
`oc-req-notification`.

## 6. Client routing (`BitfinexClient`, live)

Construction (live) builds `BfxRest` (fallback), `MarketState`, `AccountState`,
and `WsFeed(...)`, then `feed.start()`. Methods route:

- `fetch_ticker(symbol)`: if `feed.is_healthy()` and `market_state.is_fresh(symbol,
  ticker_staleness)` → return the state `Ticker`; else `BfxRest.get_ticker` (which
  also updates `market_state`).
- `fetch_positions()` / `fetch_position(symbol)`: if `account_state.authenticated`
  → return state positions; else `BfxRest.get_positions`.
- `fetch_balance()`: if authenticated → state wallets; else `BfxRest.get_wallets`.
- `create_order(...)` / `close_position(...)`: if `feed.is_healthy()` →
  `feed.submit_order_sync(...)` (raises `OrderRejected`/`AckUnparseable` as
  specified — the engine already catches both); else `BfxRest.submit_order`.
  **A WS submit that times out is NOT retried over REST** (unknown outcome).
- `fetch_my_trades(...)`: state fills if available, else `BfxRest.get_trades`.
- `last_fill(symbol)`: expose `account_state.last_fill` for fee enrichment (§8).
- `close()`: `feed.stop()`; wired to bot shutdown + `atexit`.

Paper mode: unchanged — `PaperBroker` + `_PublicTicker`, no feed, `is_healthy`
absent/false. The engine and consumers call identical methods in both modes.

## 7. Reconnect & reconciliation

- **The gotcha (verified in the installed bfxapi v4 source):** the WS event
  emitter gates `open`, `authenticated`, `position_snapshot`, `wallet_snapshot`
  (and the other snapshots) in a `_ONCE_PER_CONNECTION` set, and the emitter's
  seen-events list is **reused across reconnects and never reset**. bfxapi
  auto-reconnects and re-subscribes/re-auths transparently, but it does **not**
  re-emit `open`/`authenticated`/snapshots on a reconnect — and it emits
  `disconnected` **only on a terminal give-up**, not on transient drops. So there
  is **no reliable event signal of a reconnect**, and an event-driven reconnect
  trigger cannot work.
- **Recovery mechanism — an unconditional periodic REST reconcile.** The feed
  runs a background task every `reconcile_interval_seconds` (default **90**) that
  overwrites `AccountState` positions+wallets from `BfxRest.get_positions()` +
  `get_wallets()` (snapshot semantics) and sets `last_reconcile`. This is the
  primary correctness guarantee: it bounds account-state staleness to the
  interval regardless of any reconnect, missed event, or checksum gap. Between
  reconciles, incremental push updates keep state current for changes that occur
  while connected. Cost is ~2 REST calls / 90 s vs the old ~7 reads / 60 s cycle
  — still a large reduction, and the high-frequency ticker reads are fully
  push-based.
- **Terminal disconnect:** when bfxapi gives up (emits `disconnected`), the feed
  marks `connected=authenticated=False` → `is_healthy()` is False → reads and
  orders fall back to REST until the feed recovers/restarts.
- **Startup window:** before the first snapshot/auth, reads fall back to REST, so
  the bot is never blind while connecting.

## 8. Fee enrichment (closes the Phase-2 gap)

WS `trade_execution`/`_update` deliver `fee`/`fee_currency`. `AccountState`
retains the most recent `Fill` per symbol. When the engine writes a `FillRecord`
on close (Phase-1 persistence), it reads `client.last_fill(symbol)` and uses its
`fee`/`fee_currency` if the fill matches (same symbol, recent). Best-effort: if no
WS fill arrived yet (or feed unhealthy), fee stays 0 — never blocks the close.
`bitfinex/` stays independent of `persistence/`; the engine owns the mapping it
already does.

## 9. Threading & safety

- `MarketState`/`AccountState` guard all mutations and reads with a
  `threading.Lock` and return immutable frozen dataclasses (or fresh lists), so
  the engine never observes a torn update.
- The WS thread is the sole state writer (plus REST-fallback writes from the
  engine thread, also under the lock). The order bridge uses a per-`cid`
  `threading.Event` + holder, set in the asyncio thread, awaited in the engine
  thread with a timeout.
- The WS thread is a **daemon**; a handler exception is caught+logged and never
  kills the loop. If the thread dies or never authenticates, **everything falls
  back to REST** — worst-case behavior equals Phase 2, never worse.
- State reads are non-blocking; the only place the engine can block on WS is the
  order-confirm timeout (bounded, default 10s).
- Keyguard (Phase 2) is unchanged; WS auth and REST share the per-instance key.

## 10. Config (`exchange.ws`)

```yaml
exchange:
  ws:
    enabled: true                      # false => pure Phase-2 REST behavior (kill-switch)
    wss_host: "wss://api.bitfinex.com/ws/2"
    ticker_staleness_seconds: 15
    order_confirm_timeout_seconds: 10
    reconcile_interval_seconds: 90      # periodic REST reconcile (reconnect recovery)
```

`enabled: false` is a clean kill-switch that reverts to Phase-2 REST-only.

## 11. Error handling

- A rejected WS order (`on-req-notification` status ERROR) → `OrderRejected`
  (exchange said no). A missing confirmation → `AckUnparseable` (unknown — do not
  retry). The engine already catches both and returns its outward failure dict.
- REST-fallback failures behave exactly as Phase 2 (a failed read = no data this
  cycle; a failed order = `success=False`).
- WS connection/auth failures degrade silently to REST with ERROR-level logs; the
  feed keeps trying to reconnect in the background.

## 12. Testing

- **state/**: `MarketState`/`AccountState` apply+get; freshness windows;
  snapshot-replace vs incremental-upsert vs close-removes; a concurrent
  reader/writer thread test asserting no torn reads and correct final state.
- **ws_feed** (inject a **fake `bfx.wss`** — no network): driving each handler
  updates the right state; `submit_order_sync` returns a typed `Order` when a
  matching-`cid` `on-req-notification(SUCCESS)` is delivered; ERROR → `OrderRejected`;
  no confirmation within timeout → `AckUnparseable`; a re-`authenticated` event
  triggers a REST reconcile (fake `BfxRest`); a handler exception does not kill
  the loop.
- **client routing**: fresh ticker → state, stale → REST; authenticated → state
  positions/wallets, not → REST; healthy WS → WS submit, unhealthy → REST submit;
  WS submit timeout is not REST-resubmitted.
- **Regression**: the full existing suite (bfx, persistence, engine) stays green
  with `ws.enabled: false` and with a stubbed/unhealthy feed (REST path).
- **Live gate** (user-approved, real money): extend the smoke test to assert the
  feed connects + authenticates, a ticker and a position update arrive over WS,
  and an order placed over WS confirms with a real id; then reduce-only close,
  account flat.

## 13. Definition of done (Phase 3)

- New `state/` package (`MarketState`, `AccountState`) + `bitfinex/ws_feed.py`,
  unit-tested (incl. threading + reconnect-reconcile + order bridge).
- `BitfinexClient` (live) routes WS-first / REST-fallback for ticker, positions,
  balance, and order placement; paper mode unchanged.
- Per-cycle authenticated REST reads eliminated in the healthy-WS path (replaced
  by state reads); REST reconcile on reconnect + periodic backstop.
- WS order placement with cid-correlated confirmation; timeout → `AckUnparseable`
  (no auto-retry); REST fallback when WS unhealthy.
- Fill fee enrichment wired into persistence on close.
- `exchange.ws.enabled: false` kill-switch reverts to Phase-2 behavior.
- Full test suite green (with the feed stubbed/disabled); the engine's outward
  contract and the Phase-1 persistence wiring unchanged.
- Live WS smoke test passes on the real account (user-gated).
