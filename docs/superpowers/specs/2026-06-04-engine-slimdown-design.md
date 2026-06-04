# Design: Trading Architecture Refactor — Phase 4, Engine Slim-Down

Date: 2026-06-04
Status: Approved (design) — pending written-spec review
Branch: refactor/trading-architecture
Predecessors: Phase 1 persistence (implemented), Phase 2 bitfinex package (implemented, live gate deferred), Phase 3 WebSocket feed (implemented, live gate deferred)

## 1. Background & motivation

`execution/engine.py` is 1218 lines / 24 methods and still carries structure that
the earlier phases made redundant:

- A **separate ~391-line paper simulation** (`_paper_execute_order`,
  `_create_paper_position`, `_update_paper_balance_on_close`,
  `_init_paper_balance`, plus paper branches in `execute_order`/`close_position`)
  with `self._client = None` in paper mode — a second order/position/balance
  world parallel to the live path.
- A **per-cycle position cache** (`_cached_live_positions`,
  `_update_position_cache`, `_merge_live_positions_with_meta`, the
  `open_positions` property) that duplicates what `BitfinexClient` now maintains
  push-first via `AccountState` (Phase 3).
- A **local margin-short fallback** (`_open_positions` union in
  `_merge_live_positions_with_meta`) that predates the Phase-2 bfxapi
  `get_positions()` (which reads `/auth/r/positions`, reporting margin).
- **Dead/misplaced code:** `_trades_csv` (only a DB-path anchor), `_enforce_rate_limit`
  (the client owns transport throttling now), `get_open_positions` (unused),
  `_normalize_symbol` (duplicates `bitfinex/symbols.py`), `_current_price`
  (double WS/REST layer over the client), and camelCase position-dict aliases.

This phase slims the engine to a **mode-agnostic execution core**: it always
goes through `BitfinexClient`, paper simulation moves into the client's
`PaperBroker`, bot-owned SL/TP metadata moves into a `state/risk_state.py`
`RiskState`, the position-cache hacks collapse onto the client's state, and the
dead code is removed — while preserving the engine's outward contract to
`main.py` and the Phase 1–3 wiring.

## 2. Decisions (resolved during brainstorming, 2026-06-04)

- **Scope: aggressive full restructure.** Mode-agnostic engine; paper sim →
  `PaperBroker`; SL/TP → `state/risk_state.py`; position cache collapsed onto the
  client; margin-short fallback consolidated into `RiskState`; dead code removed.
- **Sequencing: restructure now, with guardrails** — paper-parity golden tests,
  a read-only live `get_positions()` check, and a config escape-hatch. The live
  gates (2/3/4) run together afterward.
- **The engine builds and uses `BitfinexClient` in BOTH modes.** No
  `if self.mode == "paper"` branching remains in the order/close paths.
- **PnL stays in the engine, computed identically for both modes** from the
  position `entry_price` and the close `Order.avg_price`.

**Out of scope:** running the live gates (deferred); changing trading
decisions/sizing/risk thresholds; the dashboard.

## 3. Target architecture

```
bitfinex/paper.py     PaperBroker — ENRICHED: absorbs the engine's paper sim
                      (slippage, taker fee, per-currency wallet tracking, position
                      netting, realistic fill avg_price). Returns the same typed
                      Order/Position/Wallet/Fill it does today.
state/risk_state.py   RiskState — per-symbol bot-owned SL/TP + trailing metadata,
                      NOT exchange state. Also the principled home of the
                      margin-short safety net (a position the bot opened that the
                      exchange feed may not report).
execution/engine.py   ExecutionEngine (slim): mode-agnostic order dispatch via the
                      client, persistence wiring (repo — unchanged), unified PnL,
                      SL/TP lifecycle via RiskState, startup sync, close().
```

The engine becomes a thin orchestrator: decide → submit via client → compute PnL
→ persist → record trade → update RiskState. The two-world paper/live split
disappears.

## 4. Mode-agnostic engine

- `ExecutionEngine.__init__` always builds `self._client =
  BitfinexClient(config, mode, instance)` (today it is `None` in paper).
- `execute_order(...)`: validates direction, computes size already done upstream,
  calls `self._client.create_order(symbol, side, amount, order_type=, price=,
  reduce_only=False)`, maps the returned typed `Order` into the existing outward
  result dict (`success`, `id`/`order_id`, `average`/`filled_price`, `filled`,
  `fee`, `error`), records the order/position via the repo (Phase-1 wiring; the
  decision *signal* is still recorded in `main.py`'s run loop, unchanged), and
  writes SL/TP into `RiskState`. One path, no mode branch.
- `close_position(symbol, reason)`: fetch the position
  (`self._client.fetch_position`), submit the opposite-side reduce-only order via
  `self._client.create_order(..., reduce_only=True)`, compute PnL from
  `entry_price` + `order.avg_price` (fallback `_current_price`), `_persist_close`
  (with WS fee enrichment from Phase 3), `_record_trade`, clear `RiskState`. One
  path.
- The engine catches `OrderRejected`/`AckUnparseable` (Phase 2/3) and returns its
  existing failure dict — unchanged.

## 5. Enriched PaperBroker (`bitfinex/paper.py`)

The engine's paper logic moves here so the client is the single simulator in
paper mode. `PaperBroker` gains:

- **Wallets** seeded from `initial_capital` (quote currency) with per-currency
  `balance`/`available`, updated on fills (quote spent/received, fee deducted).
- **Slippage** on market fills: `avg_price = price * (1 ± slippage_pct)`
  (default 0.05%, buy up / sell down).
- **Fee**: taker `fee = avg_price * amount * fee_pct` (default 0.1%), recorded on
  the returned `Order` and the simulated `Fill`.
- **Position netting**: a reduce-only or opposing order nets against the existing
  same-symbol position; full close removes it, partial reduces it, a flip closes
  then opens the remainder. (Ports the current `_paper_execute_order` netting.)
- `submit_order` returns a typed `Order` with `avg_price`/`fee`/`is_filled`;
  `get_positions`/`get_wallets` reflect the simulated state; `get_trades`/
  `last_fill` return the simulated fills.

The engine computes PnL from the position entry + `order.avg_price` (same as
live), so `PaperBroker` does **not** compute PnL — it only produces realistic
fills + tracks balances/positions. This keeps PnL unified.

**Guardrail:** golden tests (§8) pin the current paper outputs before the move
and assert the enriched `PaperBroker` reproduces them exactly.

## 6. RiskState (`state/risk_state.py`)

Per-symbol, bot-owned, thread-safe (consistent with `state/`):

```
class RiskState:
    def set(self, symbol, *, stop_loss, take_profit, trailing_stop,
            trailing_activation, trailing_distance, entry_price, side) -> None
    def get(self, symbol) -> Optional[dict]
    def update_trailing(self, symbol, *, stop_loss=None, highest_price=None,
                        lowest_price=None) -> None
    def clear(self, symbol) -> None
    def symbols(self) -> list[str]              # bot-known open symbols
```

- Set on entry (replaces writing `_live_position_meta`/paper position SL/TP).
- `main._check_positions` reads `engine.open_positions` (positions from the
  client) and merges the matching `RiskState.get(symbol)` SL/TP fields for the
  threshold check; trailing-stop updates write back via `update_trailing`
  (replacing the in-place position-dict mutation).
- Cleared on close.

## 7. Position consolidation & margin-short safety net

- `engine.open_positions` returns `self._client.fetch_positions()` (push-first
  `AccountState` when WS-healthy, REST otherwise — the client already does this),
  **unioned** with any `RiskState`-known symbol the client did not report. The
  union is the principled replacement for the old `_open_positions` margin-short
  hack: the bot always knows what it opened.
- The per-cycle `_update_position_cache()` call and the engine's own cache are
  removed; the client's state is the cache.
- **Config `exchange.trust_exchange_positions` (default `false`)**: when `false`,
  the `RiskState` union is applied (safety net retained). When `true` (after the
  read-only live check / live gate confirms the exchange reports margin shorts),
  the union is skipped and positions come purely from the client.
- camelCase aliases in the engine's position dicts are removed after the readers
  (`scripts/reconcile_*.py`, `monitoring/telegram_alerts.py`) are migrated to
  snake_case.

## 8. Guardrails

1. **Paper-parity golden tests.** Before changing paper code, capture the current
   engine paper outputs (fill `avg_price`, `fee`, per-currency balance deltas,
   netting result, realized PnL) for a fixed scenario set — open long, open short,
   partial net, full close, reverse, insufficient balance — as JSON fixtures.
   After the `PaperBroker` move, a test asserts the new outputs match the fixtures
   exactly. Paper results cannot drift.
2. **Read-only live position check.** `scripts/check_live_positions.py` does a
   **read-only** `client.fetch_positions()` against the live account (NO orders —
   safe without the money-moving gate) and prints whether margin/short positions
   appear. Gates flipping `trust_exchange_positions` to `true`.
3. **Config escape-hatch.** `trust_exchange_positions: false` keeps the union
   until validated; one-line revert.
4. **Outward contract preserved.** `main.py`'s engine surface keeps the same
   method signatures and return shapes.

## 9. Dead-code removal

- `_trades_csv` / `_last_api_call` / `_enforce_rate_limit` — removed; `db_path`
  computed from `data.db_file` (fallback `<data_dir>/trading.db`).
- `get_open_positions()` — removed (unused wrapper).
- `_normalize_symbol` — removed; callers use `bitfinex.symbols.to_display`.
- `_current_price` — simplified to `self._client.fetch_ticker(symbol).last` (or
  bid/ask mid) with the entry-price fallback; the client owns WS/REST.
- camelCase position-dict aliases — removed after reader migration (§7).

## 10. main.py changes (minimal)

- Remove the per-cycle `self.executor._update_position_cache()` call.
- `_check_positions`: read `engine.open_positions`; for each, merge
  `RiskState.get(symbol)` SL/TP for the threshold check; on a trailing update call
  `RiskState.update_trailing(...)` instead of mutating a position dict.
- `sync_positions_at_startup` continues to seed `RiskState` from the exchange
  positions at startup (it already seeds SL/TP meta today).
- No change to decision/sizing/risk logic.

## 11. Error handling

- The engine's outward result dicts and typed-error handling
  (`OrderRejected`/`AckUnparseable`) are unchanged in semantics.
- A failed read = no data this cycle (unchanged); a failed order =
  `success=False` (unchanged). Persistence remains non-crashing (Phase-1 `_safe`).

## 12. Testing

- **Paper-parity golden tests** (§8.1) — the gate on the `PaperBroker` move.
- **`RiskState` unit tests:** set/get/update_trailing/clear; `symbols()`; the
  union behavior (a RiskState-known symbol absent from client positions surfaces;
  with `trust_exchange_positions=true` it does not).
- **Enriched `PaperBroker` unit tests:** slippage direction, fee, netting (full
  close, partial reduce, flip), wallet balance deltas, insufficient balance.
- **Engine tests:** existing `tests/test_close_path.py`, `test_direction_filter.py`,
  and the persistence tests stay green against the mode-agnostic path (update
  stubs to the client interface as needed). A test that paper and live close
  paths share one code path (no `mode==paper` branch in `close_position`).
- **Regression:** full suite green; the engine's outward contract and Phase 1–3
  wiring unchanged. (The 2 pre-existing unrelated `tests/test_nonce_atomic.py`
  failures are out of scope.)

## 13. Decomposition (for the plan)

Sequenced smallest-blast-radius first; each step independently testable:

- **4a — Paper unification:** capture golden tests → enrich `PaperBroker` →
  engine builds/uses the client in paper mode → delete the engine paper-sim
  methods. Gate: golden tests pass.
- **4b — RiskState + position consolidation:** add `RiskState`; move SL/TP off
  `_live_position_meta`; `open_positions` = client ∪ RiskState; remove the
  per-cycle cache + `_update_position_cache`; wire `main._check_positions` /
  `trust_exchange_positions`.
- **4c — Dead-code cleanup + reader migration:** migrate `scripts/reconcile_*`,
  `telegram_alerts` to snake_case; drop aliases; remove `_trades_csv`,
  `_enforce_rate_limit`, `get_open_positions`, `_normalize_symbol`; simplify
  `_current_price`.

## 14. Definition of done (Phase 4)

- Engine is mode-agnostic — no `mode == "paper"` branch in the order/close paths;
  paper simulation lives in `PaperBroker` with golden-test parity.
- SL/TP metadata in `RiskState`; the position-cache hacks
  (`_cached_live_positions`/`_update_position_cache`/`_merge_live_positions_with_meta`)
  removed; positions come from the client, unioned with `RiskState` under the
  `trust_exchange_positions` guard.
- Dead code removed; readers migrated off camelCase aliases.
- `read-only check_live_positions.py` available to validate margin-short
  visibility before flipping `trust_exchange_positions`.
- Full test suite green; the engine's outward contract to `main.py` and the
  Phase 1–3 wiring (persistence, typed errors, WS routing) preserved.
- Engine line count materially reduced (target: well under ~800 lines).
