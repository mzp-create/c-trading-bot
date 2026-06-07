# Design: WS Auto-Reconnect Supervisor + Single Auth WS Per Key

Date: 2026-06-07
Status: Approved (design) — pending written-spec review
Predecessor: 2026-06-04 WebSocket Feed (Phase 3, implemented)
Related: incident 2026-06-07 nonce storm (commit 8f41309)

## 1. Background & motivation

After the 2026-06-07 nonce-storm fix (`risk/regime_detector.py` no longer leaks
live clients), the live process was restarted. On restart **both** WS feeds hit
`HTTP 429` (residual rate-limit from the storm) and exited. They never came back:
the bot ran in REST-only fallback until investigated.

Two distinct gaps were found.

**Gap A — bfxapi's auto-reconnect is narrow.** The Phase-3 spec assumed bfxapi's
WS client "auto-reconnects." It does, but only for a small set of failures.
`bfx_websocket_client.start()` (`.venv/.../websocket/_client/bfx_websocket_client.py`)
reconnects (with exponential backoff, factor 1.618) **only** on:
- WebSocket close code **1006** (abnormal) or **1012** (server restart), and
- **408**/DNS `gaierror` *while already in a reconnection*.

For everything else — **HTTP 429** (`InvalidStatusCode`, status 429), keepalive
**1011**, and `ReconnectionTimeoutError` (offline past the timeout) — it executes
`raise error` (terminal). Our `WsFeed._run` catches the raise, logs "WS feed loop
exited", marks the feed down, and the daemon thread ends. **There is no outer
supervisor**, so the feed stays down until a full process restart.

**Gap B — two authenticated WS feeds on one API key.** Both
`MarketDataCollector` and `ExecutionEngine` build a `BitfinexClient(mode="live")`,
and each starts its own auth `WsFeed`. The collector only needs *public* data
(candles via `OhlcvFetcher`, ticker via REST). Two auth connections on one key is
the same nonce-collision surface that caused the storm — at small scale today,
but it is avoidable.

## 2. Decisions (resolved during brainstorming, 2026-06-07)

- **Scope:** (A) add a reconnect **supervisor** to `WsFeed`; (B) stop the
  collector from opening an auth WS feed. Both shipped together so we touch the
  live WS layer once.
- **Reconnect policy:** infinite retry with **capped** exponential backoff — never
  give up permanently (permanent-down is the current broken behavior). Self-heals
  whenever Bitfinex recovers.
- **No concurrent connections — the hard safety invariant.** A new bfxapi client
  is created only *after* the previous `wss.start()` has returned/raised **and**
  the old client is explicitly closed. Reconnection is strictly sequential; it can
  never run two WS connections on the shared key at once.
- **Rate-limit aware:** a 429/rate-limit terminal cause uses a higher backoff
  floor so reconnection cannot re-trigger 429.
- **REST stays the safety net:** during any gap the feed is marked down and all
  reads/orders fall back to `BfxRest` (unchanged Phase-3 behavior).

**Out of scope:** changing bfxapi internals; the latent `df["Close"]` vs lowercase
`close` correlation-gate bug (tracked separately); any strategy/risk change.

## 3. Component A — drop the collector's auth WS feed

**`bitfinex/client.py`**
- Add `enable_ws: bool = True` to `BitfinexClient.__init__`.
- In live mode, gate the existing WS startup on it:
  `if enable_ws and ws_cfg.get("enabled", True): self._start_ws(...)`.
- When `enable_ws=False`: build `BfxRest` as today (REST ticker/reads, key
  registration) but **no** `WsFeed`, **no** auth WS connection, **no** reconcile
  loop. `_ws_healthy()` stays `False`, so reads route through `BfxRest`.

**`market_data/collector.py`**
- Construct its client with `enable_ws=False`.

**Behavior:** collector `get_ohlcv` already uses the keyless `OhlcvFetcher`;
`get_current_price` → `fetch_ticker` → (no WS) → `BfxRest.get_ticker` (a public
ticker endpoint). No behavior change other than removing the redundant auth WS.

**Net:** exactly one auth WS connection on the key (engine), down from two.

## 4. Component B — reconnect supervisor in `WsFeed`

Constructor (backward compatible — existing positional calls unchanged) gains
optional, defaulted tunables and test seams:
- `reconnect_min: float = 5.0`, `reconnect_max: float = 300.0`,
  `reconnect_factor: float = 1.7`, `reconnect_jitter: float = 0.3`,
  `ratelimit_floor: float = 60.0`, `healthy_reset_after: float = 120.0`
- `bfx_factory: Optional[Callable[[], Any]] = None` — builds a fresh client per
  connection cycle (defaults to real `bfxapi.Client`; an injected `bfx=` is
  honored on the first cycle for backward compat).
- `sleep: Callable = asyncio.sleep` — injectable for deterministic backoff tests.
- `self._stopping: bool = False`.

Replace `_main` with `_supervise`:

```
def _run(self):
    loop = new asyncio loop; self._loop = loop
    try:
        loop.run_until_complete(self._supervise())
    finally:
        self._mark_down(); loop.close()

async def _supervise(self):
    delay = self._reconnect_min
    while not self._stopping:
        self._bfx = self._build_client()      # fresh client (or injected on 1st pass)
        self._register_handlers()
        recon = create_task(self._reconcile_loop())
        connected_at = monotonic-ish (loop.time())
        terminal = None
        try:
            await self._bfx.wss.start()       # blocks until disconnect/terminal
        except Exception as exc:
            terminal = exc
            log.error("WS feed loop exited: %s", exc)
        finally:
            recon.cancel(); await-suppress(recon)
            self._mark_down()                 # REST fallback during the gap
            await self._close_client(self._bfx)   # ensure fully closed: NO overlap
        if self._stopping:
            break
        # backoff reset if the last connection was healthy long enough
        if loop.time() - connected_at >= self._healthy_reset_after:
            delay = self._reconnect_min
        sleep_for = self._backoff(delay, terminal)   # +jitter; 429 -> max(delay, floor)
        log.warning("WS reconnecting in %.1fs", sleep_for)
        await self._sleep(sleep_for)
        delay = min(delay * self._reconnect_factor, self._reconnect_max)
```

Helpers:
- `_build_client()`: returns the injected `bfx` on the first call if provided,
  else `self._bfx_factory()` if set, else a real `bfxapi.Client(api_key,
  api_secret, [wss_host])`.
- `_close_client(bfx)`: best-effort `await bfx.wss.close()` guarded against
  "loop closed"/already-closed, mirroring the current `stop()` guards.
- `_backoff(delay, terminal)`: apply jitter (`delay * (1 ± jitter*rand)`); if
  `terminal` is a 429/rate-limit (`InvalidStatusCode` status 429, or message
  contains "429"), return `max(result, ratelimit_floor)`.
- `_is_ratelimit(exc)`: classify terminal cause.

`stop()` (updated): set `self._stopping = True` first, then the existing
close/loop-stop/thread-join sequence. The supervisor sees `_stopping` and breaks
instead of reconnecting.

Unchanged: all `_on_*` handlers, `_reconcile`, `is_healthy`, `submit_order_sync`
and its WS/REST routing, `_register_handlers` contents.

## 5. Data flow / state across a reconnect

1. Connection drops or raises terminally → `_mark_down()` → `is_healthy()` False.
2. `BitfinexClient` reads/orders route to `BfxRest` (existing fallback). State
   stays correct via REST during the gap.
3. Supervisor backs off (jitter; 429 floor), then builds a fresh client and
   re-registers handlers.
4. On reconnect bfxapi re-emits `open` (→ re-subscribe tickers) and, for the auth
   client, `authenticated` + position/wallet snapshots; the periodic
   `_reconcile_loop` (90s) also refreshes `AccountState` via REST.
5. `is_healthy()` returns True again → reads resume from the push state store.

## 6. Error handling & safety

- **No concurrent connections:** new client built only after prior `start()`
  returned/raised and `_close_client` ran. Strictly sequential.
- **No reconnect storm:** capped exponential backoff + jitter; 429 floor.
- **Order safety unchanged:** orders only go over WS when `_ws_healthy()`; during
  a gap they use REST. A WS submit unconfirmed within `order_confirm_timeout`
  still raises `AckUnparseable` (no silent resubmit).
- **Clean shutdown:** `_stopping` prevents reconnect-after-stop; the SIGKILL live
  restart path is unaffected (hard kill skips `stop()` entirely, by design, to
  preserve open positions — see the nonce-storm incident memory).

## 7. Testing

New `tests/test_ws_feed_reconnect.py`, driving `_supervise` with a scripted
`bfx_factory` (fake clients whose `wss.start()` raises/returns per script) and a
fake `sleep` that records delays and returns immediately:
- **reconnects after a terminal error** (e.g. 429): ≥2 clients built, handlers
  re-registered each time.
- **backoff sequence**: recorded sleeps follow min → ×factor → cap (ignoring
  jitter via tolerance).
- **429 floor**: a 429 terminal cause yields a sleep ≥ `ratelimit_floor`.
- **healthy reset**: a connection that "stayed up" past `healthy_reset_after`
  resets the next delay to `reconnect_min`.
- **stop() halts reconnect**: setting `_stopping` (or calling stop) ends the loop
  with no further client builds.
- **marks down between connections**: `is_healthy()` is False during the gap.

`tests/test_collector_cache.py` extended (or a small new test): a live-mode
collector builds its `BitfinexClient` with `enable_ws=False` (no WsFeed spun up).

All existing `test_ws_feed.py`, `test_ws_feed_orders.py`,
`test_client_ws_routing.py` must continue to pass unchanged.

## 8. Deployment

After the full suite passes, load the change with one live restart using the
**SIGKILL-preserves-positions** procedure (hard-kill the live PID so `_shutdown()`
does not close the open shorts; relaunch detached with the same command). WS then
reconnects on its own; verify a single auth WS connection, reconnect after an
induced drop, and zero nonce errors.
