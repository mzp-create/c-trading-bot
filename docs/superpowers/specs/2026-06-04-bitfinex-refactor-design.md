# Design: Trading Architecture Refactor — Phase 2, Bitfinex Package (bfxapi)

Date: 2026-06-04
Status: Approved (design) — pending written-spec review
Branch: refactor/trading-architecture
Predecessor: Phase 1 persistence (docs/superpowers/specs/2026-06-03-persistence-sqlite-design.md, implemented)

## 1. Background & motivation

The Bitfinex integration is a single 1067-line `market_data/bitfinex_client.py`
(`BitfinexClient`) plus two dead untracked copies (`bitfinex_client_ccxt.py`,
`bitfinex_client_legacy.py`). Today's live order path is a workaround: ccxt
4.5.54 cannot parse Bitfinex's `on-req` order acknowledgement, so the client
submits orders through raw ccxt `private_post_auth_w_order_submit` and parses the
ack array by index (`_parse_onreq`). That path is **live-verified** (opens,
reduce-only closes, leaves the account flat) but the code carries real debt:

- **Monolith.** One file mixes symbol conversion, paper simulation, REST calls,
  on-req parsing, position fetching, and normalization.
- **Four scattered symbol converters** (`_symbol_to_ccxt`, `_symbol_to_bitfinex`,
  `_bitfinex_to_display` in the client; `_normalize_symbol` + a `symbol_map`
  builder in the engine) with fragile string surgery.
- **Phantom-success bug.** `_normalize_order_result`'s defensive `except` returns
  `{"success": True, "id": None, "error": None}` — a parse failure looks like a
  successful order.
- **Missing fee.** Normalized order results carry no fee.
- **Success-contract mismatch.** Callers historically checked `.get("success")`
  on raw ccxt dicts.
- **Shared API key across instances.** The dual long/short instances can run on
  one API key, producing `nonce: small` storms (root cause of the 2026-06-03
  incident's secondary symptom). No code enforcement of per-instance keys.

This phase replaces the monolith with a focused `bitfinex/` package, moves the
authenticated REST surface to the **official `bfxapi` library** (which parses the
on-req ack natively into typed objects), confines ccxt to public OHLCV candles,
enforces per-instance API keys in code, and fixes the three bugs — while
preserving the exact, verified order semantics.

## 2. Decisions (resolved during brainstorming, 2026-06-04)

- **Transport:** move order placement to **bfxapi** now (not deferred to
  Phase 3). bfxapi's REST auth interface returns a typed `Notification[Order]`
  that natively parses the on-req ack, eliminating the custom array-index parser.
- **REST boundary:** bfxapi owns **all authenticated** calls — submit/cancel
  order, positions, wallets/balance, my-trades — plus public ticker. **ccxt is
  confined to public OHLCV candles only.**
- **Market:** **spot-margin `tBTCUST`** (unchanged — matches the live-verified
  path and where the ~\$538 UST collateral sits). The mapper standardizes on
  `BTC/USDT ⇄ tBTCUST`. Closes use an opposite-side margin order with the
  reduce-only flag (1024), which is verified to work for spot-margin.
- **Key enforcement:** a **shared key-fingerprint registry/lock** — a live
  instance refuses to start if another live instance already registered the same
  API-key hash (stores the hash, never the key).
- **Migration:** **clean break** — an idiomatic typed client API; update all
  call sites (engine, collector, scripts, tests). No legacy facade.
- **Bug fixes in scope:** phantom-success, missing fee, success-contract — all
  resolved by the typed design. Delete the two dead untracked client files and
  the old monolith.

**Out of scope (later phases):** the WebSocket feed and push-maintained
state (Phase 3 — where orders-over-WS removes nonce pressure and a native
positions channel + rich `myTrades` fills land); engine slim-down (Phase 4).

## 3. Target package layout

```
bitfinex/                 (replaces market_data/bitfinex_client*.py)
  __init__.py    re-exports BitfinexClient + typed models
  symbols.py     single source of truth: display ⇄ bitfinex (BTC/USDT ⇄ tBTCUST)
  models.py      typed: Order, Position, Ticker, Wallet, Fill
  errors.py      BitfinexError, OrderRejected, AckUnparseable, KeyConflictError
  keyguard.py    per-instance API-key fingerprint registry/lock (live only)
  rest.py        bfxapi REST: submit/cancel order, positions, wallets, my-trades, ticker
  ohlcv.py       ccxt-only public candle fetch (the one thing ccxt keeps)
  paper.py       paper-mode simulation (extracted from today's inline sim)
  client.py      BitfinexClient — composes the above; paper + live; the public API
```

Each module has one responsibility and is independently testable. `market_data/
bitfinex_client.py`, `bitfinex_client_ccxt.py`, and `bitfinex_client_legacy.py`
are deleted. (`market_data/collector.py` and other `market_data/` files remain.)

## 4. Symbol mapper (`symbols.py`)

One module replaces all four scattered converters. Spot-margin only:

- `to_bitfinex(display: str) -> str` — `"BTC/USDT" -> "tBTCUST"`.
- `to_display(bfx: str) -> str` — `"tBTCUST" -> "BTC/USDT"`; also tolerates the
  derivative form `"tBTCF0:USTF0" -> "BTC/USDT"` so stray derivative positions
  read cleanly.
- An explicit quote map (`{"USDT": "UST", "USD": "USD"}` and inverse) instead of
  `.replace()` string surgery. Unknown symbols raise `ValueError` (fail loud, not
  silently pass through).

The engine's `_normalize_symbol` and `symbol_map` builder collapse to calls into
this module. `_symbol_to_ccxt` is dropped — ccxt now only fetches OHLCV, which
takes display symbols.

## 5. Typed models (`models.py`)

Frozen dataclasses; callers read attributes, not dict keys. `amount` fields are
floats; symbols are display form unless named `raw_symbol`.

- `Order`: `id: int|None, symbol, side ("buy"|"sell"), order_type ("market"|
  "limit"), amount (abs requested), filled, avg_price, status, reduce_only, fee,
  fee_currency, raw`. Properties: `is_filled` (status indicates fully executed),
  `is_rejected`. **`status` is always explicit** — there is no success sentinel
  that can default to True.
- `Position`: `symbol, side ("long"|"short"), amount (signed), abs_amount,
  entry_price, unrealized_pnl, leverage, raw_symbol`.
- `Ticker`: `symbol, bid, ask, last`.
- `Wallet`: `currency, wallet_type, balance, available`.
- `Fill`: `symbol, side, amount, price, fee, fee_currency, order_id, trade_id,
  ts`.

## 6. REST transport (`rest.py`) — bfxapi

A thin typed wrapper over the official `bfxapi` REST auth client. (Exact bfxapi
method names/enums are pinned against the installed version during planning; this
spec fixes the behavior and the mapping to our models.)

- **Construction:** `BfxRest(api_key, api_secret)` builds the bfxapi client.
- **submit_order(symbol, side, amount, order_type, price, reduce_only) -> Order:**
  - symbol → `to_bitfinex` (`tBTCUST`); amount **signed** by side; `order_type`
    maps to bfxapi margin `MARKET`/`LIMIT` (margin, not `EXCHANGE *`);
    `reduce_only=True` sets flag 1024.
  - bfxapi returns `Notification[Order]`. On notification status `ERROR` → raise
    `OrderRejected(text)`. On success → map the typed order to our `Order`
    (id, status, avg price, filled, fee if present).
  - **If the call raises after sending** (network/transport, or a result we
    cannot interpret) → raise `AckUnparseable`. Callers must treat this as
    "outcome unknown — do NOT auto-retry"; reconciliation happens on the next
    positions read.
- **cancel_order(order_id) -> Order.**
- **get_positions() -> list[Position]:** bfxapi positions → `Position` (display
  symbol via `to_display`, signed amount, entry price, unrealized PnL, leverage).
  Spot-margin (`tBTCUST`) positions are included.
- **get_wallets() -> list[Wallet].**
- **get_trades(symbol, since, limit) -> list[Fill].**
- **get_ticker(symbol) -> Ticker** (bfxapi public).

No raw array indexing remains; bfxapi owns ack parsing.

## 7. OHLCV (`ohlcv.py`) — ccxt

The only retained ccxt use. `fetch_ohlcv(symbol, timeframe, limit, since) ->
pandas.DataFrame` using ccxt with a public (keyless) ccxt instance — historical
candles need no auth, so this never contends for the API-key nonce. Display
symbols in; ccxt's symbol form handled internally here (not leaked elsewhere).

## 8. Key-fingerprint registry guard (`keyguard.py`) — live only

Fixes the dual-instance shared-key nonce storm in code.

- On live client construction, compute `fp = sha256(api_key.encode()).hexdigest()
  [:16]`. **Only the hash is stored — never the key.**
- Registry file `data/.bfx_key_registry.json`, accessed under an OS file lock
  (`fcntl.flock`). Shape: `{ fp: {"instance": str, "pid": int, "ts": str} }`.
- **Reap** entries whose pid is no longer alive (`os.kill(pid, 0)` → `ProcessLookupError`).
- If `fp` is held by a **different live instance** → raise `KeyConflictError`
  with a clear message ("instance '<x>' is already running with this API key;
  long and short need separate keys"). Same-instance/same-pid re-entry is allowed
  (restart-safe after reaping).
- Register own `{instance, pid, ts}`; deregister via `atexit`. Crash recovery is
  handled by reap-on-startup.
- Paper mode skips the guard entirely (no key).

## 9. Public client API (`client.py`)

`BitfinexClient(config, mode, instance)` composes `symbols` + `models` +
(`rest` + `keyguard` for live | `paper` for paper) + `ohlcv`. Public methods,
all returning typed models (paper and live return the *same* types):

```
create_order(symbol, side, amount, *, order_type="market", price=None,
             reduce_only=False) -> Order
close_position(symbol) -> Order          # opposite side, reduce_only=True
cancel_order(order_id) -> Order
fetch_positions() -> list[Position]
fetch_position(symbol) -> Position | None
fetch_balance() -> list[Wallet]
fetch_ticker(symbol) -> Ticker
fetch_my_trades(symbol=None, since=None, limit=None) -> list[Fill]
get_ohlcv(symbol, timeframe="1h", limit=200, since=None) -> DataFrame
```

A short-TTL cache for `fetch_positions` (≈5 s) and `fetch_balance` is retained
(it exists today) to avoid hammering REST within a cycle; Phase 3's WS feed
replaces it. `instance` is passed so the keyguard and logs are instance-aware.

## 10. Order & close semantics (preserve the verified behavior)

These are carried over **unchanged** from the live-verified path; only the
transport underneath changes:

- **Open:** margin `MARKET`/`LIMIT` to `tBTCUST`, amount signed by side.
- **Close:** opposite-side margin order with reduce-only flag 1024.
- **No blind retry:** an ambiguous submit result (`AckUnparseable`) is never
  retried automatically; the engine reconciles via the next positions read.
- **Confirmation:** bfxapi returns an explicit order id + status, so success is
  read directly from the typed `Order`, not inferred.

## 11. Caller migration (clean break)

- **`execution/engine.py`:** `_live_execute_order`, `close_position`, the
  position cache (`_update_position_cache`/`_get_live_positions`),
  `_current_price`, and the order-result handling read `Order`/`Position`/`Ticker`
  attributes instead of dict keys. The symbol helpers call `bitfinex.symbols`.
  **The engine's *outward* contract to `main.py` is preserved** — `execute_order`
  still returns the same result dict (`success`, `db_order_id`, `error`, …) and
  the Phase-1 persistence mapping (`OrderRecord`/`FillRecord`/`PositionRecord`)
  is unchanged; the engine maps the typed `Order`/`Position` into those records
  internally. So Phase-1 wiring (signals, equity, `_persist_close`) needs no
  changes beyond reading typed fields where it previously read dict keys.
- **`market_data/collector.py`:** uses `get_ohlcv` (ccxt) — minimal change.
- **Scripts:** `close_positions.py` and the live smoke test move to the typed
  API; unmaintained scripts referencing the old client are updated or removed.
- **Paper mode:** `paper.py` returns the same typed models, so the engine is
  mode-agnostic and no `if mode == "paper"` branches leak into callers.

## 12. Error handling & the three bug fixes

- **Phantom success — removed.** No code path returns success on a parse/transport
  failure. Failures raise `OrderRejected` (exchange said no) or `AckUnparseable`
  (outcome unknown — do not retry). The engine catches these and returns its
  existing `{"success": False, "error": ...}` outward dict.
- **Missing fee — wired.** `Order.fee`/`fee_currency` flow from bfxapi when
  present (market fills may report fee on execution; otherwise 0 with a noted
  Phase-3 enrichment via WS `myTrades`). The persistence `FillRecord`/`fee`
  columns (already present from Phase 1) now receive real values when available.
- **Success-contract — typed.** Callers read `order.is_filled`/`order.status`;
  the `.get("success")`-on-raw-dict pattern is gone.

## 13. Reliability

- bfxapi REST failures (network, 5xx, rate limit) raise typed errors; the engine
  treats a failed read as "no data this cycle" (it already does) and a failed
  order as `{"success": False}` — never a phantom success.
- The keyguard prevents the dual-instance nonce storm at startup.
- Persistence (Phase 1) remains the audit trail; order/fill rows now carry the
  real bfxapi order id (`exchange_order_id`) and fee.

## 14. Testing

- **Unit:**
  - `symbols`: round-trip `BTC/USDT ⇄ tBTCUST`; derivative `tBTCF0:USTF0` →
    `BTC/USDT`; unknown raises.
  - `models`: construction, `is_filled`/`is_rejected`.
  - `keyguard`: two instances, same key → `KeyConflictError`; stale pid reaped;
    same-instance restart allowed; registry stores only the hash.
  - `rest`: against a **stubbed bfxapi client** — filled ack → `Order`; `ERROR`
    notification → `OrderRejected`; transport raise → `AckUnparseable`; positions
    → typed `Position`s; signed-amount + reduce-only-flag construction asserted.
  - `paper`: paper client returns the same typed models; open/close parity.
- **Integration:** engine open/close path against a stubbed `BitfinexClient`
  returning typed models — replaces/retools `tests/test_close_path.py` and
  `tests/test_onreq_recovery.py` (the latter's array-parse concern is now
  bfxapi's responsibility; it becomes a bfxapi-stub submit/confirm test).
- **Live gate (before declaring Phase 2 done):** re-run the single-close smoke
  test on the real account — open → confirm id+status → reduce-only close →
  account flat. Mirrors how the ccxt path was verified.
- The full existing suite stays green (persistence tests untouched).

## 15. Dependency & config

- Add a pinned `bfxapi` dependency. The repo has **no `requirements.txt`**
  today (deps live in the `.venv`); planning should create/declare a pinned
  `requirements.txt` (or equivalent) capturing at least `bfxapi`, `ccxt`,
  `pandas`, `fastapi`, so the bfxapi addition is reproducible — and `pip install`
  bfxapi into the venv.
- Config: `exchange.api_key`/`api_secret` unchanged in shape; instance configs
  (`config/long.yaml`, `config/short.yaml`) should reference instance-specific
  env vars (`BITFINEX_LONG_API_KEY` / `BITFINEX_SHORT_API_KEY`). The keyguard
  enforces distinctness at runtime regardless of naming.
- `.gitignore`: add `data/.bfx_key_registry.json` (runtime artifact).

## 16. Definition of done (Phase 2)

- New `bitfinex/` package (symbols, models, errors, keyguard, rest, ohlcv, paper,
  client) with unit tests green; the three dead/old client files deleted.
- All authenticated REST goes through bfxapi; ccxt only fetches OHLCV.
- Single symbol mapper; the four old converters and the engine `symbol_map`
  builder removed.
- Per-instance key registry guard active in live mode (fail-fast on shared key).
- Phantom-success, missing-fee, and success-contract bugs fixed; order/fill rows
  carry the real bfxapi order id and fee.
- Engine and other callers migrated to the typed API; the engine's outward
  contract to `main.py` and the Phase-1 persistence wiring are preserved.
- Full test suite green; live single-close smoke test passes on the real account.
