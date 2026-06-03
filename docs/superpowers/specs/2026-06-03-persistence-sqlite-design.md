# Design: Trading Architecture Refactor — Phase 1, Persistence (SQLite)

Date: 2026-06-03
Status: Approved (design) — pending written-spec review
Branch: refactor/trading-architecture

## 1. Background & motivation

The bot currently polls Bitfinex REST every cycle (`fetch_positions`, `fetch_balance`,
per-order `fetch_ticker`) and journals only realized trades to a flat
`data/trades.csv`. Two problems drove this work:

- **Efficiency / nonce pressure.** Per-cycle authenticated REST polling plus two
  dual-instance processes sharing one API key produced `nonce: small` storms and
  rate-limit waits (see the 2026-06-03 incident).
- **No audit trail.** Live `trades.csv` files were empty during the incident —
  the bot had no record of orders that were submitted, filled, or rejected.

This document specifies the **overall target architecture** and details the
**first phase: a SQLite persistence layer**. Later phases (client refactor,
WebSocket feed, engine slim-down) get their own spec → plan → build cycles.

## 2. Target architecture (whole effort, for context)

```
bitfinex/ (replaces the 1067-line monolithic client)
  symbols.py   single source of truth for symbol conversion
  models.py    typed Order / Fill / Position / Ticker
  rest.py      bfxapi REST: place/cancel order, bootstrap, OHLCV
  ws_feed.py   bfxapi WebSocket in a background thread; pushes
               ticker + auth (orders/positions/wallets/fills) into state
        │ push updates                 │ place/cancel (orders over WS → ~0 REST nonces)
        ▼                              ▼
state/ (thread-safe, push-maintained)
  MarketState   latest prices
  AccountState  positions / balances / fills (authoritative from WS)
        │ engine READS (no REST polling)
        ▼
execution/engine.py (slimmed)     persistence/ (SQLite, WAL)
  SL/TP, sizing, decisions  ─────► orders, fills, trades,
  reads state, acts via rest        positions, equity, signals
                                          │
                                 dashboard/api_server.py reads the DB
```

**Decisions that frame the effort** (resolved during brainstorming):

- Efficiency approach: **WebSocket / event-driven**, integrated as a **background
  WS thread + in-memory state cache**; the synchronous engine and strategies
  read from the cache instead of polling REST.
- Database: **SQLite**, recording **orders, fills, trades, positions, equity
  snapshots, and signals**.
- Transport: **official `bfxapi` library** for both the WS data feed and order
  placement (orders over WS → near-zero REST nonces, native positions channel).
- **Per-instance API keys enforced in code** (a process refuses to start if the
  long and short instances would share a key). Lands in Phase 2.

**Phase sequencing:** 1 → 2 → 3 → 4.

1. **Persistence (SQLite)** — this document. Independent, lowest risk, immediate
   value (audit trail). No trading-behavior change.
2. **Bitfinex package refactor** — split the monolith, one symbol mapper, move
   order placement to bfxapi, fix the review bugs (negative `filled`, `fee=0`,
   phantom-success `except`), enforce per-instance keys.
3. **WebSocket feed + state store** — push-maintained `MarketState`/`AccountState`;
   engine reads state instead of polling REST. The efficiency win.
4. **Engine slim-down** — engine consumes state + persistence; remove polling,
   cache hacks, dead code.

## 3. Phase 1 scope

### In scope
- A new `persistence/` package: connection/schema management and a typed
  `TradingRepository` with all reads and writes.
- Six tables: `orders`, `fills`, `trades`, `positions`, `equity_snapshots`,
  `signals`.
- Wiring into the existing engine: record orders at submission, realized trades
  on close, position open/close, per-cycle equity snapshot, and the decision
  signal — for both paper and live modes.
- Migrate `dashboard/api_server.py` to read the DB instead of `trades.csv`.
- A one-time importer for any existing `trades.csv` rows.
- Tests.

### Out of scope (later phases)
- The WS feed, the `bitfinex/` package split, per-instance key enforcement,
  engine slim-down. The `fills` table exists now but is only richly populated
  once the WS `myTrades` channel lands in Phase 3.

## 4. Layout & data ownership

- **One SQLite file per instance**, at the instance's data dir:
  `<data_dir>/trading.db` (e.g. `instances/long/data/trading.db`,
  `instances/short/data/trading.db`; default-instance → `data/trading.db`).
  Each bot process is the sole writer of its own file → no cross-process write
  contention. The `instance` column is still recorded on every row for clarity
  and for the dashboard's combined view.
- **DB is the single source of truth.** `trades.csv` is retired; a one-time
  script imports existing rows. No dual-write.

## 5. Schema

ISO-8601 UTC strings for timestamps. `mode` is `paper`|`live`. `raw`/JSON columns
store the exchange payload as TEXT (JSON).

```sql
CREATE TABLE IF NOT EXISTS orders (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                TEXT    NOT NULL,
  instance          TEXT    NOT NULL,
  symbol            TEXT    NOT NULL,        -- display form, e.g. BTC/USDT
  side              TEXT    NOT NULL,        -- buy | sell
  order_type        TEXT    NOT NULL,        -- market | limit
  amount            REAL    NOT NULL,        -- requested (absolute)
  price             REAL,                    -- limit price, NULL for market
  reduce_only       INTEGER NOT NULL DEFAULT 0,
  reason            TEXT,                    -- entry | stop_loss | take_profit | manual | ...
  exchange_order_id TEXT,
  status            TEXT    NOT NULL,        -- accepted | rejected | filled | partial | canceled | unknown
  filled            REAL    NOT NULL DEFAULT 0,
  avg_price         REAL,
  error             TEXT,
  raw               TEXT,                    -- JSON of the exchange ack/result
  mode              TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                TEXT    NOT NULL,
  instance          TEXT    NOT NULL,
  symbol            TEXT    NOT NULL,
  side              TEXT    NOT NULL,
  amount            REAL    NOT NULL,        -- absolute
  price             REAL    NOT NULL,
  fee               REAL    NOT NULL DEFAULT 0,
  fee_currency      TEXT,
  order_id          INTEGER,                 -- FK -> orders.id
  exchange_trade_id TEXT,
  mode              TEXT    NOT NULL,
  FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS trades (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT    NOT NULL,
  instance     TEXT    NOT NULL,
  symbol       TEXT    NOT NULL,
  side         TEXT    NOT NULL,             -- side of the position being closed
  entry_price  REAL    NOT NULL,
  close_price  REAL    NOT NULL,
  amount       REAL    NOT NULL,             -- absolute
  pnl          REAL    NOT NULL,
  fee          REAL    NOT NULL DEFAULT 0,
  reason       TEXT,
  position_id  INTEGER,                      -- FK -> positions.id
  mode         TEXT    NOT NULL,
  FOREIGN KEY (position_id) REFERENCES positions(id)
);

CREATE TABLE IF NOT EXISTS positions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  instance      TEXT    NOT NULL,
  symbol        TEXT    NOT NULL,
  side          TEXT    NOT NULL,            -- buy | sell  (long | short)
  amount        REAL    NOT NULL,            -- absolute
  entry_price   REAL    NOT NULL,
  opened_at     TEXT    NOT NULL,
  closed_at     TEXT,
  realized_pnl  REAL,
  status        TEXT    NOT NULL,            -- open | closed
  mode          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT    NOT NULL,
  instance     TEXT    NOT NULL,
  balance      REAL    NOT NULL,             -- wallet balance
  equity       REAL    NOT NULL,             -- balance + unrealized
  open_count   INTEGER NOT NULL DEFAULT 0,
  daily_pnl    REAL    NOT NULL DEFAULT 0,
  mode         TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                  TEXT    NOT NULL,
  instance            TEXT    NOT NULL,
  symbol              TEXT    NOT NULL,
  decision            TEXT    NOT NULL,      -- BUY | SELL | HOLD
  confidence          REAL    NOT NULL,
  strategy_breakdown  TEXT,                  -- JSON of per-strategy contributions
  acted               INTEGER NOT NULL DEFAULT 0,
  order_id            INTEGER,               -- FK -> orders.id when acted
  mode                TEXT    NOT NULL,
  FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE INDEX IF NOT EXISTS idx_orders_ts            ON orders(ts);
CREATE INDEX IF NOT EXISTS idx_trades_ts            ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_trades_symbol        ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_positions_status     ON positions(status);
CREATE INDEX IF NOT EXISTS idx_equity_ts            ON equity_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_signals_ts           ON signals(ts);

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
```

A `schema_version` row enables forward migrations later. Initial version = 1.

## 6. Repository API

`persistence/repository.py` exposes `TradingRepository`, the single interface
for all DB access. Methods accept typed `models.py` dataclasses or scalars and
return ids / typed rows. Sketch:

```
class TradingRepository:
    def __init__(self, db_path: str, instance: str, mode: str): ...

    # writes (return row id)
    def record_order(self, order: OrderRecord) -> int
    def update_order(self, order_id: int, *, status, filled, avg_price, error=None) -> None
    def record_fill(self, fill: FillRecord) -> int
    def open_position(self, pos: PositionRecord) -> int
    def close_position(self, position_id: int, *, closed_at, close_price, realized_pnl) -> None
    def record_trade(self, trade: TradeRecord) -> int
    def snapshot_equity(self, snap: EquitySnapshot) -> int
    def record_signal(self, sig: SignalRecord) -> int

    # reads (dashboard / reporting)
    def recent_trades(self, limit: int = 100) -> list[TradeRecord]
    def trades_between(self, start: str, end: str) -> list[TradeRecord]
    def open_positions(self) -> list[PositionRecord]
    def equity_curve(self, start: str, end: str) -> list[EquitySnapshot]
    def daily_pnl(self, day: str) -> float
```

`instance` and `mode` are injected at construction so callers don't repeat them.

## 7. Reliability

- On open: `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL`,
  `PRAGMA busy_timeout=5000`, `PRAGMA foreign_keys=ON`. WAL lets the dashboard
  read while the bot writes.
- **Persistence must never crash trading.** Every write is wrapped; on any
  `sqlite3.Error` it logs at ERROR and returns a sentinel (e.g. `-1`/`None`) so
  the trading path continues. A failed `record_*` never propagates.
- One connection per process, used from the engine's (single) thread in Phase 1.
  When the WS thread is added (Phase 3) the repository gets a lock or a
  per-thread connection; noted there, not built now.

## 8. Integration points (current engine)

- `ExecutionEngine.__init__` builds `self._repo = TradingRepository(db_path,
  instance, mode)` (path from config `data.db_file`, default `<data_dir>/trading.db`).
- `_record_trade(...)` → also calls `self._repo.record_trade(...)` (replaces the
  CSV append).
- Order submission (paper and live, in `create_order`'s callers / engine open &
  close paths): `record_order(...)` before/at submit, `update_order(...)` with the
  result; on a realized close also `record_fill`/`record_trade`.
- Position open → `open_position(...)`; close → `close_position(...)`.
- `main.py` cycle summary → `snapshot_equity(...)` once per cycle.
- `main.py` decision loop → `record_signal(...)` (with `acted`/`order_id` when a
  trade results).

These hooks are additive; they do not change decisions, sizing, or order flow.

## 9. Dashboard migration

`dashboard/api_server.py` currently reads `data/trades.csv` (`TRADES_PATH`,
`_read_trades()`, the trade-history endpoint). Change `_read_trades()` to query
the repository (`recent_trades`/`trades_between`) and return the same JSON shape
the frontend already consumes. For the dual-instance view, the dashboard opens
both instance DBs (read-only) and unions the results, sorted by `ts`.

## 10. CSV import

`scripts/import_trades_csv.py`: read an existing `trades.csv`, map its columns
(`timestamp,symbol,side,entry_price,close_price,amount,pnl,reason,mode`) into
`TradeRecord`s, and insert via `record_trade`. Idempotent (skip if a row with the
same ts+symbol+amount already exists). One-time, run manually per instance.

## 11. Config

- `data.db_file` (string): path to the SQLite file. Default: alongside the
  instance data dir as `trading.db`.
- `.gitignore`: add `*.db`, `*.db-wal`, `*.db-shm` (runtime artifacts).

## 12. Testing (`tests/test_persistence.py`)

- Schema creates cleanly on a temp DB; `schema_version` = 1.
- Each `record_*`/`update_*`/`close_position` round-trips (write then read back).
- `record_trade` reproduces the old CSV semantics (same fields/values for a
  known trade).
- Concurrent read under WAL: a second read-only connection sees committed rows
  while a write connection is open.
- **DB-failure isolation:** with a deliberately broken connection (e.g. closed /
  read-only path), `record_*` logs and returns the sentinel without raising — a
  test asserts the trading path would continue.
- `daily_pnl`/`equity_curve`/`recent_trades` return correct aggregates.

## 13. Risks & mitigations

- *Two instances, one DB file.* Avoided by per-instance files.
- *Dashboard reading mid-write.* WAL + read-only connections.
- *A DB bug silently dropping records.* Writes are wrapped + logged, and tests
  assert round-trips; the audit value depends on writes succeeding, so failures
  are logged loudly (ERROR) even though they don't crash trading.
- *Schema churn in later phases.* `schema_version` + additive migrations; the
  `fills` table already exists so Phase 3 adds data, not columns.

## 14. Definition of done (Phase 1)

- `persistence/` package + `tests/test_persistence.py` green.
- Engine records orders/trades/positions/equity/signals in paper and live modes.
- Dashboard reads the DB; `trades.csv` retired; importer available.
- No change to trading decisions, sizing, or order flow; full existing suite
  still green.
