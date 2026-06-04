# Persistence (SQLite) Implementation Plan — Phase 1

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a SQLite persistence layer that records orders, fills, trades, positions, equity snapshots, and signals for every trading cycle (paper and live), retire `trades.csv`, and serve the dashboard from the database — without changing any trading decision, sizing, or order flow.

**Architecture:** A new self-contained `persistence/` package owns all DB access behind a typed `TradingRepository`. One SQLite file per instance (`<data_dir>/trading.db`, WAL mode), with the bot process as sole writer. The engine and main loop call additive `record_*` hooks that are individually wrapped so a DB failure logs loudly but never propagates into the trading path. The dashboard reads the DB (read-only, WAL) instead of the CSV.

**Tech Stack:** Python 3 stdlib `sqlite3` (WAL), dataclasses, `pytest` for tests, FastAPI dashboard (unchanged framework). No new third-party dependency. (`bfxapi` is **not** required for this phase — it lands in Phase 2/3.)

**Source of truth:** design spec `docs/superpowers/specs/2026-06-03-persistence-sqlite-design.md`.

---

## Key facts discovered from the codebase (read before starting)

- `ExecutionEngine.__init__(self, config, mode="paper", trade_direction="both")` at `execution/engine.py:66`. It reads `self.data_config = config.get("data", {})` and builds `self._trades_csv` from `data.trades_file` at `execution/engine.py:97-100`.
- `_record_trade(self, position, close_price, pnl, reason)` at `execution/engine.py:923-953` builds a 9-field record (`timestamp,symbol,side,entry_price,close_price,amount,pnl,reason,mode`), appends to `self._trade_history` (a `deque`), then appends to CSV. Called from `close_position` (paper at `:1014`, live at `:1055`).
- `execute_order(...)` dispatch is `execution/engine.py:305-315` — returns directly from a paper/live branch.
- `close_position(symbol, reason)` at `execution/engine.py:955-1076` — paper and live branches each compute `pnl` and call `_record_trade`.
- `TradingBot.__init__(self, config_path, mode="paper", instance="default")` at `main.py:101`. `self.instance` is set at `main.py:106`; the engine is constructed at `main.py:134` **without** passing instance. `self.daily_pnl` / `self.trade_count` exist (`main.py:110-111`). `self.initial_capital` at `main.py:151`.
- Cycle summary block: `main.py:644-665`. Per-symbol loop calls `execute_trade_cycle` and collects `decision` dicts; order execution is `main.py:524-534` where `result = self.executor.execute_order(...)` and `decision` is returned at `main.py:564`. A `decision` dict has keys `signal` (`BUY|SELL|HOLD`), `confidence`, `reason`, `current_price`, `symbol`.
- Dashboard `dashboard/api_server.py`: `TRADES_PATH = BOT_DIR / "data" / "trades.csv"` (`:34-35`), `read_trades()` (`:172-184`) returns a list of dicts whose keys are the CSV headers; `/api/trades` (`:392-398`) reverses and wraps as `{"trades": [...], "count": n}`; `/api/summary` (`:434-504`) consumes `t.get("pnl")`, `t.get("symbol")`, `t.get("side")`, `t.get("timestamp")`.
- Config `data:` block is at `config/default.yaml:152-157`. Instance configs `config/long.yaml` / `config/short.yaml` set `data.trades_file: "instances/<dir>/data/trades.csv"`.
- Tests live in `tests/`, plain `pytest`-runnable scripts that `sys.path.insert(0, str(Path(__file__).parent.parent))`. No `conftest.py`. Run with `pytest tests/ -v` (or `python tests/<file>.py`).
- `.gitignore` currently ignores `data/trades.csv` but not `*.db`.
- Instance `trades.csv` files use a **different, simpler** 6-field header (`timestamp,symbol,side,amount,price,pnl`) and are currently header-only/empty — the importer must tolerate both layouts.

---

## File structure

**New package `persistence/`:**
- `persistence/__init__.py` — exports `TradingRepository` and the record dataclasses.
- `persistence/models.py` — typed dataclasses: `OrderRecord`, `FillRecord`, `PositionRecord`, `TradeRecord`, `EquitySnapshot`, `SignalRecord`. `instance`/`mode` are **not** on the dataclasses — the repository injects them.
- `persistence/schema.py` — DDL string, `SCHEMA_VERSION`, `connect(db_path)` (PRAGMAs), `init_db(conn)`.
- `persistence/repository.py` — `TradingRepository`: all writes/reads, central `_safe()` wrapper for DB-failure isolation.

**New tests / scripts:**
- `tests/test_persistence.py` — schema, round-trips, WAL concurrent read, DB-failure isolation, aggregates.
- `scripts/import_trades_csv.py` — one-time CSV → DB importer (idempotent).

**Modified:**
- `execution/engine.py` — construct repo; dual-write `_record_trade`; record/update orders; record positions + fills on close.
- `main.py` — pass `instance` to engine; per-cycle equity snapshot; per-decision signal.
- `dashboard/api_server.py` — `read_trades()` queries the DB(s).
- `config/default.yaml`, `config/long.yaml`, `config/short.yaml` — add `data.db_file`.
- `.gitignore` — ignore `*.db`, `*.db-wal`, `*.db-shm`.

---

## Task 1: Record dataclasses (`persistence/models.py`)

**Files:**
- Create: `persistence/__init__.py`
- Create: `persistence/models.py`
- Test: `tests/test_persistence.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_persistence.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from persistence.models import (
    OrderRecord, FillRecord, PositionRecord,
    TradeRecord, EquitySnapshot, SignalRecord,
)


def test_order_record_defaults():
    o = OrderRecord(ts="2026-06-03T00:00:00+00:00", symbol="BTC/USDT",
                    side="buy", order_type="market", amount=0.001)
    assert o.price is None
    assert o.reduce_only is False
    assert o.status == "unknown"
    assert o.filled == 0.0


def test_trade_record_fields():
    t = TradeRecord(ts="2026-06-03T00:00:00+00:00", symbol="BTC/USDT",
                    side="buy", entry_price=100.0, close_price=101.0,
                    amount=0.5, pnl=0.5)
    assert t.fee == 0.0
    assert t.reason is None
    assert t.position_id is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_persistence.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'persistence'`.

- [ ] **Step 3: Write minimal implementation**

Create `persistence/__init__.py` (models only for now — the repository import is
added in Task 3, once `repository.py` exists, so the package imports cleanly at
every task boundary):

```python
"""SQLite persistence layer for the trading bot.

One DB file per instance. The TradingRepository (added in Task 3) is the single
interface for all reads and writes. Writes are isolated so a DB failure never
crashes trading (see repository._safe).
"""

from persistence.models import (
    OrderRecord, FillRecord, PositionRecord,
    TradeRecord, EquitySnapshot, SignalRecord,
)

__all__ = [
    "OrderRecord", "FillRecord", "PositionRecord",
    "TradeRecord", "EquitySnapshot", "SignalRecord",
]
```

Create `persistence/models.py`:

```python
"""Typed records written to / read from the trading database.

`instance` and `mode` are NOT fields here — the repository injects them from
its construction args so callers never repeat them.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class OrderRecord:
    ts: str                              # ISO-8601 UTC
    symbol: str                          # display form, e.g. BTC/USDT
    side: str                            # buy | sell
    order_type: str                      # market | limit
    amount: float                        # requested, absolute
    price: Optional[float] = None        # limit price; None for market
    reduce_only: bool = False
    reason: Optional[str] = None         # entry | stop_loss | take_profit | manual | ...
    exchange_order_id: Optional[str] = None
    status: str = "unknown"              # accepted|rejected|filled|partial|canceled|unknown
    filled: float = 0.0
    avg_price: Optional[float] = None
    error: Optional[str] = None
    raw: Optional[str] = None            # JSON of the exchange ack/result


@dataclass
class FillRecord:
    ts: str
    symbol: str
    side: str
    amount: float                        # absolute
    price: float
    fee: float = 0.0
    fee_currency: Optional[str] = None
    order_id: Optional[int] = None       # FK -> orders.id
    exchange_trade_id: Optional[str] = None


@dataclass
class PositionRecord:
    symbol: str
    side: str                            # buy | sell (long | short)
    amount: float                        # absolute
    entry_price: float
    opened_at: str
    status: str = "open"                 # open | closed
    closed_at: Optional[str] = None
    realized_pnl: Optional[float] = None


@dataclass
class TradeRecord:
    ts: str
    symbol: str
    side: str                            # side of the position being closed
    entry_price: float
    close_price: float
    amount: float                        # absolute
    pnl: float
    fee: float = 0.0
    reason: Optional[str] = None
    position_id: Optional[int] = None    # FK -> positions.id


@dataclass
class EquitySnapshot:
    ts: str
    balance: float                       # wallet balance
    equity: float                        # balance + unrealized
    open_count: int = 0
    daily_pnl: float = 0.0


@dataclass
class SignalRecord:
    ts: str
    symbol: str
    decision: str                        # BUY | SELL | HOLD
    confidence: float
    strategy_breakdown: Optional[str] = None   # JSON / text
    acted: bool = False
    order_id: Optional[int] = None       # FK -> orders.id when acted
```

> Note: `__init__.py` imports `repository`, which doesn't exist until Task 3. Until then, import models directly in the test (as written above). Task 3 makes the package import cleanly.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_persistence.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add persistence/__init__.py persistence/models.py tests/test_persistence.py
git commit -m "feat(persistence): typed record dataclasses"
```

---

## Task 2: Schema + connection (`persistence/schema.py`)

**Files:**
- Create: `persistence/schema.py`
- Test: `tests/test_persistence.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_persistence.py`:

```python
from persistence.schema import connect, init_db, SCHEMA_VERSION


def test_schema_creates_and_versions(tmp_path):
    conn = connect(str(tmp_path / "trading.db"))
    init_db(conn)

    # All six tables + schema_version exist.
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    for t in ("orders", "fills", "trades", "positions",
              "equity_snapshots", "signals", "schema_version"):
        assert t in names

    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == SCHEMA_VERSION == 1
    conn.close()


def test_pragmas_enabled(tmp_path):
    conn = connect(str(tmp_path / "trading.db"))
    init_db(conn)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_init_db_is_idempotent(tmp_path):
    db = str(tmp_path / "trading.db")
    conn = connect(db)
    init_db(conn)
    init_db(conn)  # second call must not duplicate the version row
    rows = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    assert rows == 1
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_persistence.py -k schema -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'persistence.schema'`.

- [ ] **Step 3: Write minimal implementation**

Create `persistence/schema.py`:

```python
"""Database schema, connection factory, and initialization."""

import sqlite3

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS orders (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                TEXT    NOT NULL,
  instance          TEXT    NOT NULL,
  symbol            TEXT    NOT NULL,
  side              TEXT    NOT NULL,
  order_type        TEXT    NOT NULL,
  amount            REAL    NOT NULL,
  price             REAL,
  reduce_only       INTEGER NOT NULL DEFAULT 0,
  reason            TEXT,
  exchange_order_id TEXT,
  status            TEXT    NOT NULL,
  filled            REAL    NOT NULL DEFAULT 0,
  avg_price         REAL,
  error             TEXT,
  raw               TEXT,
  mode              TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                TEXT    NOT NULL,
  instance          TEXT    NOT NULL,
  symbol            TEXT    NOT NULL,
  side              TEXT    NOT NULL,
  amount            REAL    NOT NULL,
  price             REAL    NOT NULL,
  fee               REAL    NOT NULL DEFAULT 0,
  fee_currency      TEXT,
  order_id          INTEGER,
  exchange_trade_id TEXT,
  mode              TEXT    NOT NULL,
  FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS trades (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT    NOT NULL,
  instance     TEXT    NOT NULL,
  symbol       TEXT    NOT NULL,
  side         TEXT    NOT NULL,
  entry_price  REAL    NOT NULL,
  close_price  REAL    NOT NULL,
  amount       REAL    NOT NULL,
  pnl          REAL    NOT NULL,
  fee          REAL    NOT NULL DEFAULT 0,
  reason       TEXT,
  position_id  INTEGER,
  mode         TEXT    NOT NULL,
  FOREIGN KEY (position_id) REFERENCES positions(id)
);

CREATE TABLE IF NOT EXISTS positions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  instance      TEXT    NOT NULL,
  symbol        TEXT    NOT NULL,
  side          TEXT    NOT NULL,
  amount        REAL    NOT NULL,
  entry_price   REAL    NOT NULL,
  opened_at     TEXT    NOT NULL,
  closed_at     TEXT,
  realized_pnl  REAL,
  status        TEXT    NOT NULL,
  mode          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT    NOT NULL,
  instance     TEXT    NOT NULL,
  balance      REAL    NOT NULL,
  equity       REAL    NOT NULL,
  open_count   INTEGER NOT NULL DEFAULT 0,
  daily_pnl    REAL    NOT NULL DEFAULT 0,
  mode         TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                  TEXT    NOT NULL,
  instance            TEXT    NOT NULL,
  symbol              TEXT    NOT NULL,
  decision            TEXT    NOT NULL,
  confidence          REAL    NOT NULL,
  strategy_breakdown  TEXT,
  acted               INTEGER NOT NULL DEFAULT 0,
  order_id            INTEGER,
  mode                TEXT    NOT NULL,
  FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE INDEX IF NOT EXISTS idx_orders_ts        ON orders(ts);
CREATE INDEX IF NOT EXISTS idx_trades_ts        ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_trades_symbol    ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_equity_ts        ON equity_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_signals_ts       ON signals(ts);

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection with WAL + reliability pragmas. Row access by name."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables/indexes if absent and seed schema_version once."""
    conn.executescript(SCHEMA_SQL)
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)",
                     (SCHEMA_VERSION,))
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_persistence.py -k "schema or pragmas or idempotent" -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add persistence/schema.py tests/test_persistence.py
git commit -m "feat(persistence): schema, WAL connection, idempotent init"
```

---

## Task 3: Repository writes + DB-failure isolation (`persistence/repository.py`)

**Files:**
- Create: `persistence/repository.py`
- Test: `tests/test_persistence.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_persistence.py`:

```python
from persistence.repository import TradingRepository
from persistence.models import (
    OrderRecord as OR, FillRecord as FR, PositionRecord as PR,
    TradeRecord as TR, EquitySnapshot as ES, SignalRecord as SR,
)


def _repo(tmp_path):
    return TradingRepository(str(tmp_path / "trading.db"),
                             instance="long", mode="paper")


def test_record_order_roundtrip(tmp_path):
    repo = _repo(tmp_path)
    oid = repo.record_order(OR(ts="2026-06-03T00:00:00+00:00", symbol="BTC/USDT",
                               side="buy", order_type="market", amount=0.001,
                               reason="entry"))
    assert oid > 0
    row = repo._conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    assert row["instance"] == "long"
    assert row["mode"] == "paper"
    assert row["status"] == "unknown"
    assert row["reduce_only"] == 0


def test_update_order(tmp_path):
    repo = _repo(tmp_path)
    oid = repo.record_order(OR(ts="t", symbol="BTC/USDT", side="buy",
                               order_type="market", amount=0.001))
    repo.update_order(oid, status="filled", filled=0.001, avg_price=100.0)
    row = repo._conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    assert row["status"] == "filled"
    assert row["filled"] == 0.001
    assert row["avg_price"] == 100.0


def test_position_open_close(tmp_path):
    repo = _repo(tmp_path)
    pid = repo.open_position(PR(symbol="BTC/USDT", side="buy", amount=0.5,
                                entry_price=100.0, opened_at="t0"))
    assert pid > 0
    repo.close_position(pid, closed_at="t1", close_price=110.0, realized_pnl=5.0)
    row = repo._conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
    assert row["status"] == "closed"
    assert row["closed_at"] == "t1"
    assert row["realized_pnl"] == 5.0


def test_record_trade_fill_signal_equity(tmp_path):
    repo = _repo(tmp_path)
    tid = repo.record_trade(TR(ts="t", symbol="BTC/USDT", side="buy",
                               entry_price=100.0, close_price=101.0,
                               amount=0.5, pnl=0.5, reason="take_profit"))
    fid = repo.record_fill(FR(ts="t", symbol="BTC/USDT", side="sell",
                              amount=0.5, price=101.0, fee=0.1))
    sid = repo.record_signal(SR(ts="t", symbol="BTC/USDT", decision="BUY",
                                confidence=0.8, acted=True, order_id=1))
    eid = repo.snapshot_equity(ES(ts="t", balance=500.0, equity=505.0,
                                  open_count=1, daily_pnl=5.0))
    assert all(x > 0 for x in (tid, fid, sid, eid))
    assert repo._conn.execute("SELECT acted FROM signals WHERE id=?",
                              (sid,)).fetchone()[0] == 1


def test_db_failure_is_isolated(tmp_path):
    """A broken connection must make writes return sentinels, not raise."""
    repo = _repo(tmp_path)
    repo._conn.close()  # subsequent writes will raise sqlite3.ProgrammingError
    assert repo.record_order(OR(ts="t", symbol="X", side="buy",
                                order_type="market", amount=1.0)) == -1
    assert repo.record_trade(TR(ts="t", symbol="X", side="buy",
                                entry_price=1, close_price=1,
                                amount=1, pnl=0)) == -1
    # update/close return None without raising
    assert repo.update_order(1, status="filled", filled=1.0,
                             avg_price=1.0) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_persistence.py -k "roundtrip or update_order or position_open or trade_fill or failure" -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'persistence.repository'`.

- [ ] **Step 3: Write minimal implementation**

Create `persistence/repository.py`:

```python
"""TradingRepository — the single interface for all database access.

Every write is wrapped by `_safe`: on any sqlite3.Error it logs at ERROR and
returns a sentinel (-1 for id-returning writes, None for updates) so the
trading path never crashes because of persistence.
"""

import logging
import sqlite3
from typing import Callable, List, Optional

from persistence.schema import connect, init_db
from persistence.models import (
    OrderRecord, FillRecord, PositionRecord,
    TradeRecord, EquitySnapshot, SignalRecord,
)

log = logging.getLogger(__name__)


class TradingRepository:
    def __init__(self, db_path: str, instance: str, mode: str):
        self.db_path = db_path
        self.instance = instance
        self.mode = mode
        self._conn: Optional[sqlite3.Connection] = None
        try:
            self._conn = connect(db_path)
            init_db(self._conn)
            log.info("TradingRepository ready at %s (instance=%s, mode=%s)",
                     db_path, instance, mode)
        except sqlite3.Error as exc:
            log.error("Persistence init FAILED at %s: %s — continuing without DB",
                      db_path, exc)
            self._conn = None

    # ── internal ─────────────────────────────────────────────────────────
    def _safe(self, fn: Callable, default):
        # Catch ALL exceptions, not just sqlite3.Error: the guarantee is that
        # persistence never crashes trading, even on a non-DB programming error.
        if self._conn is None:
            return default
        try:
            return fn()
        except Exception as exc:
            log.error("Persistence write failed: %s", exc)
            return default

    # ── writes ───────────────────────────────────────────────────────────
    def record_order(self, order: OrderRecord) -> int:
        def _do():
            cur = self._conn.execute(
                "INSERT INTO orders (ts,instance,symbol,side,order_type,amount,"
                "price,reduce_only,reason,exchange_order_id,status,filled,"
                "avg_price,error,raw,mode) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (order.ts, self.instance, order.symbol, order.side,
                 order.order_type, order.amount, order.price,
                 int(order.reduce_only), order.reason, order.exchange_order_id,
                 order.status, order.filled, order.avg_price, order.error,
                 order.raw, self.mode))
            self._conn.commit()
            return cur.lastrowid
        return self._safe(_do, -1)

    def update_order(self, order_id: int, *, status: str, filled: float,
                     avg_price: Optional[float], error: Optional[str] = None) -> None:
        def _do():
            self._conn.execute(
                "UPDATE orders SET status=?, filled=?, avg_price=?, error=? "
                "WHERE id=?",
                (status, filled, avg_price, error, order_id))
            self._conn.commit()
            return None
        return self._safe(_do, None)

    def record_fill(self, fill: FillRecord) -> int:
        def _do():
            cur = self._conn.execute(
                "INSERT INTO fills (ts,instance,symbol,side,amount,price,fee,"
                "fee_currency,order_id,exchange_trade_id,mode) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (fill.ts, self.instance, fill.symbol, fill.side, fill.amount,
                 fill.price, fill.fee, fill.fee_currency, fill.order_id,
                 fill.exchange_trade_id, self.mode))
            self._conn.commit()
            return cur.lastrowid
        return self._safe(_do, -1)

    def open_position(self, pos: PositionRecord) -> int:
        def _do():
            cur = self._conn.execute(
                "INSERT INTO positions (instance,symbol,side,amount,entry_price,"
                "opened_at,closed_at,realized_pnl,status,mode) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (self.instance, pos.symbol, pos.side, pos.amount, pos.entry_price,
                 pos.opened_at, pos.closed_at, pos.realized_pnl, pos.status,
                 self.mode))
            self._conn.commit()
            return cur.lastrowid
        return self._safe(_do, -1)

    def close_position(self, position_id: int, *, closed_at: str,
                       close_price: float, realized_pnl: float) -> None:
        def _do():
            self._conn.execute(
                "UPDATE positions SET status='closed', closed_at=?, "
                "realized_pnl=? WHERE id=?",
                (closed_at, realized_pnl, position_id))
            self._conn.commit()
            return None
        return self._safe(_do, None)

    def record_trade(self, trade: TradeRecord) -> int:
        def _do():
            cur = self._conn.execute(
                "INSERT INTO trades (ts,instance,symbol,side,entry_price,"
                "close_price,amount,pnl,fee,reason,position_id,mode) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (trade.ts, self.instance, trade.symbol, trade.side,
                 trade.entry_price, trade.close_price, trade.amount, trade.pnl,
                 trade.fee, trade.reason, trade.position_id, self.mode))
            self._conn.commit()
            return cur.lastrowid
        return self._safe(_do, -1)

    def snapshot_equity(self, snap: EquitySnapshot) -> int:
        def _do():
            cur = self._conn.execute(
                "INSERT INTO equity_snapshots (ts,instance,balance,equity,"
                "open_count,daily_pnl,mode) VALUES (?,?,?,?,?,?,?)",
                (snap.ts, self.instance, snap.balance, snap.equity,
                 snap.open_count, snap.daily_pnl, self.mode))
            self._conn.commit()
            return cur.lastrowid
        return self._safe(_do, -1)

    def record_signal(self, sig: SignalRecord) -> int:
        def _do():
            cur = self._conn.execute(
                "INSERT INTO signals (ts,instance,symbol,decision,confidence,"
                "strategy_breakdown,acted,order_id,mode) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (sig.ts, self.instance, sig.symbol, sig.decision, sig.confidence,
                 sig.strategy_breakdown, int(sig.acted), sig.order_id, self.mode))
            self._conn.commit()
            return cur.lastrowid
        return self._safe(_do, -1)
```

Then extend `persistence/__init__.py` (created in Task 1) so the repository is
exported at package level now that it exists. Replace its contents with:

```python
"""SQLite persistence layer for the trading bot.

One DB file per instance. The TradingRepository is the single interface for
all reads and writes. Writes are isolated so a DB failure never crashes
trading (see repository._safe).
"""

from persistence.models import (
    OrderRecord, FillRecord, PositionRecord,
    TradeRecord, EquitySnapshot, SignalRecord,
)
from persistence.repository import TradingRepository

__all__ = [
    "TradingRepository",
    "OrderRecord", "FillRecord", "PositionRecord",
    "TradeRecord", "EquitySnapshot", "SignalRecord",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_persistence.py -v`
Expected: PASS (all tests so far green). `from persistence import TradingRepository` now resolves.

- [ ] **Step 5: Commit**

```bash
git add persistence/repository.py tests/test_persistence.py
git commit -m "feat(persistence): repository writes with DB-failure isolation"
```

---

## Task 4: Repository reads + aggregates

**Files:**
- Modify: `persistence/repository.py` (add read methods + `trade_exists`)
- Test: `tests/test_persistence.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_persistence.py`:

```python
def test_reads_and_aggregates(tmp_path):
    repo = _repo(tmp_path)
    repo.record_trade(TR(ts="2026-06-01T10:00:00+00:00", symbol="BTC/USDT",
                         side="buy", entry_price=100, close_price=110,
                         amount=1, pnl=10.0))
    repo.record_trade(TR(ts="2026-06-01T12:00:00+00:00", symbol="ETH/USDT",
                         side="sell", entry_price=50, close_price=45,
                         amount=2, pnl=-10.0))
    repo.record_trade(TR(ts="2026-06-02T09:00:00+00:00", symbol="BTC/USDT",
                         side="buy", entry_price=100, close_price=105,
                         amount=1, pnl=5.0))

    recent = repo.recent_trades(limit=2)
    assert len(recent) == 2
    assert recent[0].ts == "2026-06-02T09:00:00+00:00"   # newest first

    day1 = repo.trades_between("2026-06-01T00:00:00+00:00",
                               "2026-06-01T23:59:59+00:00")
    assert len(day1) == 2
    assert pytest.approx(repo.daily_pnl("2026-06-01")) == 0.0
    assert pytest.approx(repo.daily_pnl("2026-06-02")) == 5.0


def test_open_positions_and_equity_curve(tmp_path):
    repo = _repo(tmp_path)
    pid = repo.open_position(PR(symbol="BTC/USDT", side="buy", amount=1,
                                entry_price=100, opened_at="2026-06-01T00:00:00+00:00"))
    repo.open_position(PR(symbol="ETH/USDT", side="sell", amount=1,
                          entry_price=50, opened_at="2026-06-01T01:00:00+00:00"))
    repo.close_position(pid, closed_at="2026-06-01T02:00:00+00:00",
                        close_price=110, realized_pnl=10.0)
    opens = repo.open_positions()
    assert len(opens) == 1
    assert opens[0].symbol == "ETH/USDT"

    repo.snapshot_equity(ES(ts="2026-06-01T00:00:00+00:00", balance=500,
                            equity=500))
    repo.snapshot_equity(ES(ts="2026-06-02T00:00:00+00:00", balance=510,
                            equity=515))
    curve = repo.equity_curve("2026-06-01T00:00:00+00:00",
                              "2026-06-02T23:59:59+00:00")
    assert len(curve) == 2
    assert curve[-1].equity == 515


def test_trade_exists(tmp_path):
    repo = _repo(tmp_path)
    repo.record_trade(TR(ts="2026-06-01T10:00:00+00:00", symbol="BTC/USDT",
                         side="buy", entry_price=100, close_price=110,
                         amount=0.5, pnl=5.0))
    assert repo.trade_exists("2026-06-01T10:00:00+00:00", "BTC/USDT", 0.5) is True
    assert repo.trade_exists("2026-06-01T10:00:00+00:00", "BTC/USDT", 0.9) is False
```

Add `import pytest` to the top of the test file (after the `sys.path.insert` line).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_persistence.py -k "aggregates or equity_curve or trade_exists" -v`
Expected: FAIL with `AttributeError: 'TradingRepository' object has no attribute 'recent_trades'`.

- [ ] **Step 3: Write minimal implementation**

Append these methods inside `TradingRepository` in `persistence/repository.py` (after `record_signal`):

```python
    # ── reads ────────────────────────────────────────────────────────────
    def _trade_from_row(self, r) -> TradeRecord:
        return TradeRecord(
            ts=r["ts"], symbol=r["symbol"], side=r["side"],
            entry_price=r["entry_price"], close_price=r["close_price"],
            amount=r["amount"], pnl=r["pnl"], fee=r["fee"],
            reason=r["reason"], position_id=r["position_id"])

    def recent_trades(self, limit: int = 100) -> List[TradeRecord]:
        def _do():
            rows = self._conn.execute(
                "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._trade_from_row(r) for r in rows]
        return self._safe(_do, [])

    def trades_between(self, start: str, end: str) -> List[TradeRecord]:
        def _do():
            rows = self._conn.execute(
                "SELECT * FROM trades WHERE ts >= ? AND ts <= ? ORDER BY ts",
                (start, end)).fetchall()
            return [self._trade_from_row(r) for r in rows]
        return self._safe(_do, [])

    def open_positions(self) -> List[PositionRecord]:
        def _do():
            rows = self._conn.execute(
                "SELECT * FROM positions WHERE status='open' ORDER BY opened_at"
            ).fetchall()
            return [PositionRecord(
                symbol=r["symbol"], side=r["side"], amount=r["amount"],
                entry_price=r["entry_price"], opened_at=r["opened_at"],
                status=r["status"], closed_at=r["closed_at"],
                realized_pnl=r["realized_pnl"]) for r in rows]
        return self._safe(_do, [])

    def equity_curve(self, start: str, end: str) -> List[EquitySnapshot]:
        def _do():
            rows = self._conn.execute(
                "SELECT * FROM equity_snapshots WHERE ts >= ? AND ts <= ? "
                "ORDER BY ts", (start, end)).fetchall()
            return [EquitySnapshot(
                ts=r["ts"], balance=r["balance"], equity=r["equity"],
                open_count=r["open_count"], daily_pnl=r["daily_pnl"])
                for r in rows]
        return self._safe(_do, [])

    def daily_pnl(self, day: str) -> float:
        """Sum of trade pnl whose ts date == `day` (YYYY-MM-DD)."""
        def _do():
            row = self._conn.execute(
                "SELECT COALESCE(SUM(pnl), 0.0) FROM trades WHERE ts LIKE ?",
                (day + "%",)).fetchone()
            return float(row[0])
        return self._safe(_do, 0.0)

    def trade_exists(self, ts: str, symbol: str, amount: float) -> bool:
        """Idempotency check for the CSV importer."""
        def _do():
            row = self._conn.execute(
                "SELECT 1 FROM trades WHERE ts=? AND symbol=? "
                "AND ABS(amount-?) < 1e-12 LIMIT 1",
                (ts, symbol, amount)).fetchone()
            return row is not None
        return self._safe(_do, False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_persistence.py -v`
Expected: PASS (all green).

- [ ] **Step 5: Commit**

```bash
git add persistence/repository.py tests/test_persistence.py
git commit -m "feat(persistence): repository reads, aggregates, trade_exists"
```

---

## Task 5: WAL concurrent-read reliability test

**Files:**
- Test: `tests/test_persistence.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_persistence.py`:

```python
import sqlite3 as _sqlite3


def test_concurrent_read_under_wal(tmp_path):
    """A read-only connection sees committed rows while the writer is open."""
    db = str(tmp_path / "trading.db")
    repo = TradingRepository(db, instance="long", mode="paper")
    repo.record_trade(TR(ts="2026-06-01T10:00:00+00:00", symbol="BTC/USDT",
                         side="buy", entry_price=100, close_price=110,
                         amount=1, pnl=10.0))
    # Writer connection stays open (repo._conn). Open a second read-only conn.
    reader = _sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    n = reader.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    reader.close()
    assert n == 1
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `pytest tests/test_persistence.py -k concurrent -v`
Expected: PASS (WAL already enabled in Task 2). If it FAILS with a lock error, WAL/commit is misconfigured — fix `connect()`/commit before proceeding.

- [ ] **Step 3: (no implementation needed — this test pins existing behavior)**

- [ ] **Step 4: Run full suite**

Run: `pytest tests/test_persistence.py -v`
Expected: PASS (all green).

- [ ] **Step 5: Commit**

```bash
git add tests/test_persistence.py
git commit -m "test(persistence): WAL concurrent-read reliability"
```

---

## Task 6: Config + .gitignore

**Files:**
- Modify: `config/default.yaml:152-157`
- Modify: `config/long.yaml` (the `data:` block)
- Modify: `config/short.yaml` (the `data:` block)
- Modify: `.gitignore`

- [ ] **Step 1: Add `db_file` to `config/default.yaml`**

Replace the `data:` block at `config/default.yaml:152-157`:

```yaml
data:
  ohlcv_dir: "data/ohlcv/"
  models_dir: "data/models/"
  trades_file: "data/trades.csv"
  db_file: "data/trading.db"
  equity_file: "data/equity.csv"
  log_file: "logs/bot.log"
```

- [ ] **Step 2: Add `db_file` to the instance configs**

In `config/long.yaml`, in its `data:` block, add directly under `trades_file`:

```yaml
  db_file: "instances/long/data/trading.db"
```

In `config/short.yaml`, in its `data:` block, add directly under `trades_file`:

```yaml
  db_file: "instances/short/data/trading.db"
```

- [ ] **Step 3: Ignore DB runtime artifacts**

In `.gitignore`, under the `# Data` section (after `data/trades.csv`), add:

```
# SQLite database (runtime artifact, one per instance)
*.db
*.db-wal
*.db-shm
```

- [ ] **Step 4: Verify YAML still loads**

Run:
```bash
python -c "import yaml; [print(yaml.safe_load(open(f'config/{n}.yaml'))['data']['db_file']) for n in ('default','long','short')]"
```
Expected output:
```
data/trading.db
instances/long/data/trading.db
instances/short/data/trading.db
```

- [ ] **Step 5: Commit**

```bash
git add config/default.yaml config/long.yaml config/short.yaml .gitignore
git commit -m "feat(persistence): config db_file + gitignore db artifacts"
```

---

## Task 7: Engine — construct repo + dual-write `_record_trade`

**Files:**
- Modify: `execution/engine.py:66-109` (`__init__`)
- Modify: `execution/engine.py:923-953` (`_record_trade`)
- Test: `tests/test_engine_persistence.py` (create)

**Note on signature change:** `ExecutionEngine.__init__` gains an `instance` parameter (default `"default"`), so existing positional callers and tests keep working. The repo's DB path comes from `data.db_file`, defaulting to `trading.db` beside `trades_file`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_engine_persistence.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.engine import ExecutionEngine


def _engine(tmp_path):
    config = {
        "data": {
            "trades_file": str(tmp_path / "trades.csv"),
            "db_file": str(tmp_path / "trading.db"),
        },
        "exchange": {"rate_limit": 0.0},
    }
    return ExecutionEngine(config, mode="paper", trade_direction="both",
                           instance="long")


def test_engine_builds_repo(tmp_path):
    eng = _engine(tmp_path)
    assert eng._repo is not None
    assert eng._repo.instance == "long"
    assert (tmp_path / "trading.db").exists()


def test_record_trade_writes_to_db(tmp_path):
    eng = _engine(tmp_path)
    position = {"symbol": "BTC/USDT", "side": "buy",
                "entry_price": 100.0, "amount": 0.5}
    eng._record_trade(position, close_price=110.0, pnl=5.0, reason="take_profit")

    rows = eng._repo.recent_trades(limit=5)
    assert len(rows) == 1
    assert rows[0].symbol == "BTC/USDT"
    assert rows[0].pnl == 5.0
    assert rows[0].reason == "take_profit"
    # In-memory history still populated (back-compat).
    assert len(eng._trade_history) == 1


def test_db_path_defaults_beside_trades_file(tmp_path):
    config = {"data": {"trades_file": str(tmp_path / "sub" / "trades.csv")},
              "exchange": {"rate_limit": 0.0}}
    eng = ExecutionEngine(config, mode="paper", trade_direction="both")
    assert (tmp_path / "sub" / "trading.db").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_engine_persistence.py -v`
Expected: FAIL — `__init__()` has no `instance` kwarg / `eng._repo` does not exist.

- [ ] **Step 3: Implement — repo construction in `__init__`**

In `execution/engine.py`, change the signature at `:66`:

```python
    def __init__(self, config: dict, mode: str = "paper",
                 trade_direction: str = "both", instance: str = "default"):
        self.config = config
        self.mode = mode
        self.instance = instance
        self.trade_direction = trade_direction.lower()
```

Then, replace the CSV-path block at `:97-100` with CSV path **plus** repo construction:

```python
        # Trade history CSV path (retained for the one-time importer source)
        trades_path = self.data_config.get("trades_file", "data/trades.csv")
        self._trades_csv = Path(trades_path)
        self._trades_csv.parent.mkdir(parents=True, exist_ok=True)

        # SQLite persistence (single source of truth). Default the DB beside
        # the trades file. Construction never raises — see TradingRepository.
        from persistence import TradingRepository
        db_path = self.data_config.get("db_file") or str(
            self._trades_csv.parent / "trading.db")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._repo = TradingRepository(db_path, instance=self.instance,
                                       mode=self.mode)
```

- [ ] **Step 4: Implement — dual-write in `_record_trade`**

In `execution/engine.py`, replace the body of `_record_trade` (`:930-953`) so it writes the DB and keeps the in-memory deque, dropping the CSV append:

```python
        """Record a completed trade to in-memory history and the database."""
        ts = datetime.now(timezone.utc).isoformat()
        record = {
            "timestamp": ts,
            "symbol": position.get("symbol", ""),
            "side": position.get("side", ""),
            "entry_price": position.get("entry_price", 0),
            "close_price": close_price,
            "amount": position.get("amount", 0),
            "pnl": round(pnl, 2),
            "reason": reason,
            "mode": self.mode,
        }
        self._trade_history.append(record)

        # Persist to the database (single source of truth). Never raises.
        from persistence import TradeRecord
        self._repo.record_trade(TradeRecord(
            ts=ts,
            symbol=position.get("symbol", ""),
            side=position.get("side", ""),
            entry_price=float(position.get("entry_price", 0) or 0),
            close_price=float(close_price),
            amount=abs(float(position.get("amount", 0) or 0)),
            pnl=round(pnl, 2),
            reason=reason,
        ))
```

> This removes the `with open(self._trades_csv, ...)` block. The `csv` import elsewhere in the module is still used by other code — leave it.

- [ ] **Step 5: Pass `instance` from main.py**

In `main.py:134`, change the engine construction:

```python
        self.executor = ExecutionEngine(self.config, mode=mode,
                                        trade_direction=self.trade_direction,
                                        instance=self.instance)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_engine_persistence.py -v`
Expected: PASS (3 passed).

- [ ] **Step 7: Run the existing engine tests for regressions**

Run: `pytest tests/test_close_path.py tests/test_direction_filter.py -v`
Expected: PASS (no regression from the signature/`_record_trade` change).

- [ ] **Step 8: Commit**

```bash
git add execution/engine.py main.py tests/test_engine_persistence.py
git commit -m "feat(persistence): engine builds repo, trades persist to DB"
```

---

## Task 8: Engine — record orders, positions, and fills

**Files:**
- Modify: `execution/engine.py:305-315` (`execute_order` dispatch)
- Modify: `execution/engine.py:955-1076` (`close_position`)
- Test: `tests/test_engine_persistence.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_engine_persistence.py`:

```python
def test_execute_order_records_order(tmp_path):
    eng = _engine(tmp_path)
    result = eng.execute_order(symbol="BTC/USDT", side="buy", amount=0.001,
                               price=100.0, order_type="market")
    assert result.get("success") is True
    assert result.get("db_order_id", -1) > 0
    row = eng._repo._conn.execute(
        "SELECT * FROM orders WHERE id=?", (result["db_order_id"],)).fetchone()
    assert row["symbol"] == "BTC/USDT"
    assert row["side"] == "buy"
    assert row["reduce_only"] == 0
    assert row["status"] == "filled"


def test_close_position_records_position_and_fill(tmp_path):
    eng = _engine(tmp_path)
    eng.execute_order(symbol="BTC/USDT", side="buy", amount=0.001,
                      price=100.0, order_type="market")
    close = eng.close_position("BTC/USDT", reason="take_profit")
    assert close.get("success") is True

    # A position row exists and is closed; a closing order + fill recorded.
    pos = eng._repo._conn.execute(
        "SELECT * FROM positions ORDER BY id DESC LIMIT 1").fetchone()
    assert pos["status"] == "closed"
    assert pos["realized_pnl"] is not None
    close_orders = eng._repo._conn.execute(
        "SELECT COUNT(*) FROM orders WHERE reduce_only=1").fetchone()[0]
    assert close_orders == 1
    fills = eng._repo._conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    assert fills == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_engine_persistence.py -k "records_order or position_and_fill" -v`
Expected: FAIL — `db_order_id` not in result; no position/fill rows.

- [ ] **Step 3: Implement — record the entry order in `execute_order`**

In `execution/engine.py`, replace the dispatch block at `:305-315`:

```python
        # Persist the order (additive; never blocks trading). OrderRecord is
        # imported at module level (Task 7 cleanup).
        db_order_id = self._repo.record_order(OrderRecord(
            ts=datetime.now(timezone.utc).isoformat(),
            symbol=symbol, side=side, order_type=order_type,
            amount=abs(amount), price=price, reduce_only=False,
            reason="entry"))

        try:
            if self.mode == "paper":
                result = self._paper_execute_order(
                    symbol, side, amount, price, order_type,
                    stop_loss_pct, take_profit_pct,
                )
            else:
                result = self._live_execute_order(
                    symbol, side, amount, price, order_type,
                    stop_loss_pct, take_profit_pct,
                )
        except Exception as exc:
            self._log.error("Order execution failed: %s", exc, exc_info=True)
            self._repo.update_order(db_order_id, status="rejected", filled=0.0,
                                    avg_price=None, error=str(exc))
            return {"success": False, "error": str(exc),
                    "db_order_id": db_order_id}

        # Reflect the outcome on the order row and surface the id to callers.
        if result.get("success"):
            self._repo.update_order(
                db_order_id, status="filled", filled=abs(amount),
                avg_price=result.get("filled_price") or result.get("average")
                or price)
        else:
            self._repo.update_order(
                db_order_id, status="rejected", filled=0.0, avg_price=None,
                error=result.get("error"))
        result["db_order_id"] = db_order_id
        return result
```

> This replaces the original `try/except` that returned directly. The function's outer behavior is unchanged except the additive `db_order_id` key.

- [ ] **Step 4: Implement — record position + fill on close**

In `close_position`, both branches already compute `pnl` and call `self._record_trade(...)`. Add an `open_position` + `close_position` + `record_fill` + a reduce-only order around each `_record_trade` call.

For the **paper** branch, replace the lines at `:1013-1014`:

```python
                # Persist the position lifecycle, a reduce-only close order,
                # and the resulting fill (all additive; never raise).
                # OrderRecord/FillRecord/PositionRecord imported at module level.
                now = datetime.now(timezone.utc).isoformat()
                pos_id = self._repo.open_position(PositionRecord(
                    symbol=symbol, side=side, amount=abs(amount),
                    entry_price=entry_price,
                    opened_at=position.get("timestamp", now)))
                self._repo.close_position(pos_id, closed_at=now,
                                          close_price=current_price,
                                          realized_pnl=round(pnl, 2))
                close_oid = self._repo.record_order(OrderRecord(
                    ts=now, symbol=symbol, side=close_side, order_type="market",
                    amount=abs(amount), price=current_price, reduce_only=True,
                    reason=reason, status="filled", filled=abs(amount),
                    avg_price=current_price))
                self._repo.record_fill(FillRecord(
                    ts=now, symbol=symbol, side=close_side, amount=abs(amount),
                    price=current_price, order_id=close_oid))

                # Record trade
                self._record_trade(position, current_price, pnl, reason)
```

For the **live** branch, replace the line at `:1055` (`self._record_trade(position, close_price, pnl, reason)`):

```python
                    # OrderRecord/FillRecord/PositionRecord imported at module level.
                    now = datetime.now(timezone.utc).isoformat()
                    pos_id = self._repo.open_position(PositionRecord(
                        symbol=symbol, side=side, amount=abs(amount),
                        entry_price=entry_price,
                        opened_at=position.get("timestamp", now)))
                    self._repo.close_position(pos_id, closed_at=now,
                                              close_price=close_price,
                                              realized_pnl=round(pnl, 2))
                    close_oid = self._repo.record_order(OrderRecord(
                        ts=now, symbol=symbol, side=close_side,
                        order_type="market", amount=abs(amount),
                        price=close_price, reduce_only=True, reason=reason,
                        status="filled", filled=abs(amount),
                        avg_price=close_price,
                        exchange_order_id=str(result.get("order_id") or "")
                        or None))
                    self._repo.record_fill(FillRecord(
                        ts=now, symbol=symbol, side=close_side,
                        amount=abs(amount), price=close_price,
                        order_id=close_oid))

                    # Journal the trade and clear local SL/TP metadata so the
                    # closed position is no longer tracked.
                    self._record_trade(position, close_price, pnl, reason)
```

> `side`, `amount`, `entry_price`, `close_side` are already locals in `close_position` (`:997-1002`). `current_price` (paper) and `close_price` (live) are already computed above each insertion point.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_engine_persistence.py -v`
Expected: PASS (all green).

- [ ] **Step 6: Regression check**

Run: `pytest tests/test_close_path.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add execution/engine.py tests/test_engine_persistence.py
git commit -m "feat(persistence): record orders, positions, fills on open/close"
```

---

## Task 9: main.py — per-cycle equity snapshot + per-decision signal

**Files:**
- Modify: `main.py` — add an `_equity_snapshot_values` helper + a `_persist_cycle` call in the cycle-summary block (`:644-665`), and a signal write in `execute_trade_cycle` near `:533-564`.
- Test: `tests/test_main_persistence.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_main_persistence.py`:

```python
import sys
import types
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.engine import ExecutionEngine
from main import TradingBot


def _engine(tmp_path):
    config = {"data": {"trades_file": str(tmp_path / "trades.csv"),
                       "db_file": str(tmp_path / "trading.db")},
              "exchange": {"rate_limit": 0.0}}
    return ExecutionEngine(config, mode="paper", trade_direction="both",
                           instance="long")


def _stub_bot(eng, *, initial_capital=500.0, daily_pnl=5.0):
    """A minimal stand-in exposing only what the two helpers touch, so we can
    call the unbound TradingBot methods without a full (network-touching) init."""
    return types.SimpleNamespace(
        executor=eng,
        initial_capital=initial_capital,
        daily_pnl=daily_pnl,
        log=logging.getLogger("stub"))


def test_record_decision_signal_for_hold_and_acted(tmp_path):
    eng = _engine(tmp_path)
    bot = _stub_bot(eng)

    # HOLD decision (no order attempted) → acted=False, order_id=None.
    TradingBot._record_decision_signal(bot, "BTC/USDT",
        {"signal": "HOLD", "confidence": 0.0, "reason": "flat"})
    # Acted BUY decision carrying the order outcome.
    TradingBot._record_decision_signal(bot, "ETH/USDT",
        {"signal": "BUY", "confidence": 0.8, "reason": "trend",
         "_acted": True, "_db_order_id": 7})

    rows = eng._repo._conn.execute(
        "SELECT symbol, decision, acted, order_id FROM signals ORDER BY id"
    ).fetchall()
    assert (rows[0]["symbol"], rows[0]["decision"], rows[0]["acted"],
            rows[0]["order_id"]) == ("BTC/USDT", "HOLD", 0, None)
    assert (rows[1]["symbol"], rows[1]["decision"], rows[1]["acted"],
            rows[1]["order_id"]) == ("ETH/USDT", "BUY", 1, 7)


def test_equity_snapshot_values(tmp_path):
    eng = _engine(tmp_path)
    bot = _stub_bot(eng, initial_capital=500.0, daily_pnl=5.0)
    # No open positions → equity == balance == capital + daily_pnl.
    bal, eq = TradingBot._equity_snapshot_values(bot)
    assert bal == 505.0
    assert eq == 505.0
```

> `TradingBot.__init__` builds many network-touching components, so we call the
> two new helpers as **unbound methods** against a tiny `SimpleNamespace` stub
> exposing only the attributes they read (`executor`, `initial_capital`,
> `daily_pnl`, `log`). This gives real coverage of the helper logic without a
> live bot. The run-loop wiring is verified manually in Step 6.

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `pytest tests/test_main_persistence.py -v`
Expected: PASS (it exercises the repo, already built). If it fails, fix the repo before wiring main.py.

- [ ] **Step 3: Add the equity-snapshot helper to `TradingBot`**

In `main.py`, add this method to the `TradingBot` class (e.g. just below `__init__`, after `:163`):

```python
    def _equity_snapshot_values(self):
        """Best-effort (balance, equity) for the per-cycle snapshot.

        Balance = starting capital + realized daily PnL. Equity adds any
        unrealized PnL exposed on open positions (0 when unavailable). This is
        an audit snapshot, refined when the WS account feed lands (Phase 3).
        """
        balance = self.initial_capital + self.daily_pnl
        unrealized = 0.0
        try:
            for pos in self.executor.open_positions:
                unrealized += float(pos.get("unrealized_pnl", 0.0) or 0.0)
        except Exception:
            pass
        return round(balance, 2), round(balance + unrealized, 2)
```

- [ ] **Step 4: Snapshot equity once per cycle**

In `main.py`, in the cycle-summary block, after the `self.telegram.send_cycle_summary(...)` call ends at `:665` (and still inside the `if cycle_results:` body or right after it), add:

```python
                # Persist a per-cycle equity snapshot (additive; never raises).
                try:
                    from datetime import datetime, timezone
                    from persistence import EquitySnapshot
                    bal, eq = self._equity_snapshot_values()
                    self.executor._repo.snapshot_equity(EquitySnapshot(
                        ts=datetime.now(timezone.utc).isoformat(),
                        balance=bal, equity=eq,
                        open_count=len(self.executor.open_positions),
                        daily_pnl=round(self.daily_pnl, 2)))
                except Exception as exc:
                    self.log.error("Equity snapshot failed: %s", exc)
```

- [ ] **Step 5: Record the decision signal — from the run loop (covers ALL decisions)**

`execute_trade_cycle` has **six** return points (risk-fail `:408`, HOLD `:415`, correlation-blocked `:472`, invalid-price `:479`, too-small `:499`, final `:566`, plus the exception path). Instrumenting one return would miss the others. Instead: (a) attach the order outcome to the `decision` dict where the order is executed, and (b) record the signal once in the caller's run loop, which receives every returned decision.

**(a)** In `main.py` `execute_trade_cycle`, right AFTER `result = self.executor.execute_order(...)` returns (`:533`) and BEFORE the `if result.get('success'):` block (`:535`), insert:

```python
            # Surface the order outcome on the decision so the run loop can
            # link the persisted signal to its order row.
            decision["_acted"] = bool(result.get("success"))
            decision["_db_order_id"] = result.get("db_order_id")
```

**(b)** Add this helper to `TradingBot` (next to `_equity_snapshot_values`):

```python
    def _record_decision_signal(self, symbol: str, decision: dict) -> None:
        """Persist one decision signal per symbol per cycle (additive; never
        raises). `_acted`/`_db_order_id` are present only when an order was
        attempted; HOLD/blocked decisions record with acted=False."""
        try:
            from datetime import datetime, timezone
            from persistence import SignalRecord
            self.executor._repo.record_signal(SignalRecord(
                ts=datetime.now(timezone.utc).isoformat(),
                symbol=symbol,
                decision=str(decision.get("signal", "HOLD")),
                confidence=float(decision.get("confidence", 0.0) or 0.0),
                strategy_breakdown=str(decision.get("reason", "")),
                acted=bool(decision.get("_acted", False)),
                order_id=decision.get("_db_order_id")))
        except Exception as exc:
            self.log.error("Signal record failed: %s", exc)
```

Then in the run loop (`main.py:637-642`), call it for each returned decision:

```python
                cycle_results = []
                for s in self.symbols:
                    result = self.execute_trade_cycle(symbol_config=s)
                    if result:
                        # Add display name for Telegram
                        result["symbol_name"] = s["name"]
                        cycle_results.append(result)
                        self._record_decision_signal(s["name"], result)
```

- [ ] **Step 6: Manual wiring verification**

Run a single paper cycle and confirm rows land. Run:
```bash
python -c "
from main import TradingBot
import sqlite3
bot = TradingBot(config_path='config/default.yaml', mode='paper', instance='default')
bot.execute_trade_cycle(symbol_config=bot.symbols[0])
conn = sqlite3.connect('data/trading.db')
print('signals:', conn.execute('SELECT COUNT(*) FROM signals').fetchone()[0])
"
```
Expected: prints `signals: 1` (or more). If the bot needs network and errors before the signal write, confirm at least that no exception comes from the persistence calls.

- [ ] **Step 7: Commit**

```bash
git add main.py tests/test_main_persistence.py
git commit -m "feat(persistence): per-cycle equity snapshot and decision signals"
```

---

## Task 10: Dashboard reads the DB

**Files:**
- Modify: `dashboard/api_server.py:34-35` (paths) and `:172-184` (`read_trades`)
- Test: `tests/test_dashboard_db.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_dashboard_db.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from persistence import TradingRepository, TradeRecord


def test_read_trades_from_db(tmp_path, monkeypatch):
    db = tmp_path / "trading.db"
    repo = TradingRepository(str(db), instance="long", mode="paper")
    repo.record_trade(TradeRecord(ts="2026-06-01T10:00:00+00:00",
                                  symbol="BTC/USDT", side="buy",
                                  entry_price=100, close_price=110,
                                  amount=0.5, pnl=5.0, reason="take_profit"))

    import dashboard.api_server as api
    # Point the dashboard at our temp DB only.
    monkeypatch.setattr(api, "_db_paths", lambda: [db])
    rows = api.read_trades()

    assert len(rows) == 1
    r = rows[0]
    # Frontend-facing keys preserved.
    assert r["timestamp"] == "2026-06-01T10:00:00+00:00"
    assert r["symbol"] == "BTC/USDT"
    assert r["side"] == "buy"
    assert float(r["pnl"]) == 5.0
    assert float(r["entry_price"]) == 100.0
    assert float(r["close_price"]) == 110.0
    assert r["mode"] == "paper"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_dashboard_db.py -v`
Expected: FAIL — `api._db_paths` does not exist / `read_trades` still reads CSV.

- [ ] **Step 3: Implement — replace CSV reads with DB reads**

In `dashboard/api_server.py`, replace the `TRADES_PATH` definition at `:34-35`:

```python
CONFIG_PATH = BOT_DIR / "config" / "default.yaml"
# Trade history now lives in SQLite (one DB per instance). Union all that exist.
DB_PATHS = [
    BOT_DIR / "data" / "trading.db",
    BOT_DIR / "instances" / "long" / "data" / "trading.db",
    BOT_DIR / "instances" / "short" / "data" / "trading.db",
]
```

Replace the `read_trades()` function at `:172-184`:

```python
def _db_paths():
    """Existing per-instance DB files to read (overridable in tests)."""
    return [p for p in DB_PATHS if Path(p).exists()]


def read_trades() -> list[dict]:
    """Trade history from the SQLite DB(s), newest-first sort done by callers.

    Returns dicts using the same field names the frontend already consumes
    (`timestamp`, `symbol`, `side`, `entry_price`, `close_price`, `amount`,
    `pnl`, `reason`, `mode`), plus `instance`. Read-only WAL connections so a
    running bot is never blocked.
    """
    import sqlite3
    rows: list[dict] = []
    for db in _db_paths():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT ts, instance, symbol, side, entry_price, close_price, "
                "amount, pnl, fee, reason, mode FROM trades ORDER BY ts")
            for r in cur.fetchall():
                d = dict(r)
                d["timestamp"] = d.pop("ts")   # frontend expects 'timestamp'
                rows.append(d)
            conn.close()
        except sqlite3.Error as exc:
            logger.error("Error reading trades DB %s: %s", db, exc)
    rows.sort(key=lambda x: x.get("timestamp", ""))
    return rows
```

> `Path` is already imported in `api_server.py` (it builds `BOT_DIR`). `logger` already exists. The `/api/trades` and `/api/summary` endpoints are unchanged — they call `read_trades()` and use the same keys.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_dashboard_db.py -v`
Expected: PASS.

- [ ] **Step 5: Smoke-check the endpoints still shape correctly**

Run:
```bash
python -c "
import dashboard.api_server as api
from pathlib import Path
api._db_paths = lambda: [p for p in api.DB_PATHS if Path(p).exists()]
print('rows:', len(api.read_trades()))
"
```
Expected: prints `rows: N` with no traceback (N may be 0 if no DB yet).

- [ ] **Step 6: Commit**

```bash
git add dashboard/api_server.py tests/test_dashboard_db.py
git commit -m "feat(persistence): dashboard reads trades from SQLite (WAL, multi-instance)"
```

---

## Task 11: One-time CSV importer

**Files:**
- Create: `scripts/import_trades_csv.py`
- Test: `tests/test_import_trades_csv.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_import_trades_csv.py`:

```python
import sys
import csv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.import_trades_csv import import_csv
from persistence import TradingRepository


def _write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def test_import_full_format(tmp_path):
    csv_path = tmp_path / "trades.csv"
    _write_csv(csv_path,
               ["timestamp", "symbol", "side", "entry_price", "close_price",
                "amount", "pnl", "reason", "mode"],
               [["2026-06-01T10:00:00+00:00", "BTC/USDT", "buy", "100", "110",
                 "0.5", "5.0", "manual_close", "paper"]])
    repo = TradingRepository(str(tmp_path / "trading.db"),
                             instance="default", mode="paper")
    n = import_csv(str(csv_path), repo)
    assert n == 1
    assert len(repo.recent_trades()) == 1
    # Idempotent: re-import inserts nothing.
    assert import_csv(str(csv_path), repo) == 0
    assert len(repo.recent_trades()) == 1


def test_import_six_field_format(tmp_path):
    csv_path = tmp_path / "trades.csv"
    _write_csv(csv_path,
               ["timestamp", "symbol", "side", "amount", "price", "pnl"],
               [["2026-06-01T10:00:00+00:00", "ETH/USDT", "sell", "2", "50",
                 "-3.0"]])
    repo = TradingRepository(str(tmp_path / "trading.db"),
                             instance="short", mode="live")
    n = import_csv(str(csv_path), repo)
    assert n == 1
    t = repo.recent_trades()[0]
    assert t.symbol == "ETH/USDT"
    assert t.entry_price == 50.0 and t.close_price == 50.0   # price → both
    assert t.pnl == -3.0
    assert t.reason == "imported"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_import_trades_csv.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.import_trades_csv'`.

- [ ] **Step 3: Implement the importer**

Create `scripts/import_trades_csv.py`:

```python
#!/usr/bin/env python3
"""One-time importer: legacy trades.csv -> SQLite trades table.

Handles both CSV layouts found in this repo:
  full (9 cols):  timestamp,symbol,side,entry_price,close_price,amount,pnl,reason,mode
  slim (6 cols):  timestamp,symbol,side,amount,price,pnl   (entry==close==price)

Idempotent: a row already present (same ts+symbol+amount) is skipped. Run once
per instance:

    python scripts/import_trades_csv.py --csv data/trades.csv --db data/trading.db --instance default
"""

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from persistence import TradingRepository, TradeRecord


def _row_to_trade(row: dict) -> TradeRecord:
    ts = row.get("timestamp", "")
    symbol = row.get("symbol", "")
    side = row.get("side", "")
    pnl = float(row.get("pnl", 0) or 0)
    if "entry_price" in row and "close_price" in row:
        entry = float(row.get("entry_price", 0) or 0)
        close = float(row.get("close_price", 0) or 0)
        reason = row.get("reason") or "imported"
    else:  # slim 6-field layout: single price for entry & close
        price = float(row.get("price", 0) or 0)
        entry = close = price
        reason = "imported"
    return TradeRecord(ts=ts, symbol=symbol, side=side, entry_price=entry,
                       close_price=close, amount=abs(float(row.get("amount", 0)
                       or 0)), pnl=pnl, reason=reason)


def import_csv(csv_path: str, repo: TradingRepository) -> int:
    """Import rows into the repo. Returns the number of NEW rows inserted."""
    path = Path(csv_path)
    if not path.exists():
        return 0
    inserted = 0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if not row.get("timestamp"):
                continue
            trade = _row_to_trade(row)
            if repo.trade_exists(trade.ts, trade.symbol, trade.amount):
                continue
            if repo.record_trade(trade) != -1:
                inserted += 1
    return inserted


def main():
    p = argparse.ArgumentParser(description="Import legacy trades.csv into SQLite")
    p.add_argument("--csv", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--instance", default="default")
    p.add_argument("--mode", default="paper", choices=["paper", "live"])
    args = p.parse_args()

    repo = TradingRepository(args.db, instance=args.instance, mode=args.mode)
    n = import_csv(args.csv, repo)
    print(f"Imported {n} new trade(s) from {args.csv} into {args.db}")


if __name__ == "__main__":
    main()
```

Create `scripts/__init__.py` if it does not exist (so `from scripts.import_trades_csv import ...` resolves):

```bash
test -f scripts/__init__.py || : > scripts/__init__.py
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_import_trades_csv.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add scripts/import_trades_csv.py scripts/__init__.py tests/test_import_trades_csv.py
git commit -m "feat(persistence): idempotent one-time trades.csv importer"
```

---

## Task 12: Full-suite verification + docs

**Files:**
- Modify: `docs/superpowers/specs/2026-06-03-persistence-sqlite-design.md` (status line)

- [ ] **Step 1: Run the entire test suite**

Run: `pytest tests/ -v`
Expected: PASS — all persistence tests plus the pre-existing `test_close_path.py`, `test_direction_filter.py`, `test_position_sizing.py`, `test_nonce_atomic.py`, `test_onreq_recovery.py`. If any pre-existing test regressed, fix before continuing.

- [ ] **Step 2: Confirm no `trades.csv` write remains in the engine**

Run: `grep -n "self._trades_csv" execution/engine.py`
Expected: only the `__init__` definition lines remain (the importer source path); no `open(self._trades_csv, "a"...)` write.

- [ ] **Step 3: Update the spec status**

In `docs/superpowers/specs/2026-06-03-persistence-sqlite-design.md`, change the `Status:` line near the top to:

```
Status: Implemented (Phase 1) — see docs/superpowers/plans/2026-06-03-persistence-sqlite.md
```

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-06-03-persistence-sqlite-design.md
git commit -m "docs(persistence): mark Phase 1 implemented"
```

---

## Self-review (completed by plan author)

**Spec coverage:**
- §3 `persistence/` package → Tasks 1–4. ✔
- §5 six tables + indexes + schema_version → Task 2. ✔
- §6 repository API (all listed methods) → Tasks 3–4 (record_order, update_order, record_fill, open_position, close_position, record_trade, snapshot_equity, record_signal, recent_trades, trades_between, open_positions, equity_curve, daily_pnl). ✔
- §7 reliability (PRAGMAs, never-crash wrapping, single connection) → Task 2 (`connect`) + Task 3 (`_safe`). ✔
- §8 integration points (record orders at submit + update; record_trade on close; position open/close; per-cycle equity; signal) → Tasks 7–9. ✔
- §9 dashboard migration (DB read, same JSON shape, multi-instance union, WAL read-only) → Task 10. ✔
- §10 CSV importer (idempotent, both layouts) → Task 11. ✔
- §11 config `data.db_file` + gitignore → Task 6. ✔
- §12 tests (schema, round-trips, CSV semantics, WAL concurrent read, DB-failure isolation, aggregates) → Tasks 2–5. ✔
- §14 DoD (suite green, paper+live records, dashboard reads DB, csv retired, importer) → Task 12. ✔

**Deviations from spec (intentional, noted):**
- `bfxapi` is **not** installed and is **not** needed for Phase 1 — the design's "transport" decisions are Phase 2/3. This plan adds zero third-party deps.
- Instance CSVs use a 6-field layout; the importer (Task 11) handles both, mapping `price`→`entry_price`/`close_price`.
- `ExecutionEngine.__init__` gains an `instance` kwarg (default `"default"`) so existing positional callers/tests are unaffected.
- Order↔signal linkage is carried via an additive `db_order_id` key on `execute_order`'s result dict (avoids threading ids through internal return values).
- Equity `balance`/`equity` are best-effort (capital + realized; + unrealized when exposed) — a documented Phase-1 approximation refined by the WS account feed in Phase 3.

**Placeholder scan:** none — every code step contains complete code; every command has expected output.

**Type consistency:** dataclass field names match the SQL columns and the INSERT/SELECT bindings; `record_order`→`db_order_id`→`record_signal(order_id=...)` chain is consistent; `_trade_from_row` mirrors `TradeRecord` fields.
