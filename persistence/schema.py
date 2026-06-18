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

-- Authoritative exchange ledger (source of truth for realized P&L + fees on
-- Bitfinex margin, where the per-trade fee is 0 and exchange-side closes never
-- reach the bot's own close path). `id` is the exchange ledger id so
-- reconciliation is idempotent via INSERT OR IGNORE.
CREATE TABLE IF NOT EXISTS ledger_entries (
  id           INTEGER PRIMARY KEY,
  mts          INTEGER NOT NULL,
  ts           TEXT    NOT NULL,
  instance     TEXT    NOT NULL,
  currency     TEXT,
  kind         TEXT    NOT NULL,
  amount       REAL    NOT NULL,
  balance      REAL,
  price        REAL,
  symbol       TEXT,
  description  TEXT,
  mode         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_ts        ON orders(ts);
CREATE INDEX IF NOT EXISTS idx_trades_ts        ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_trades_symbol    ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_equity_ts        ON equity_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_signals_ts       ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_ledger_mts        ON ledger_entries(mts);
CREATE INDEX IF NOT EXISTS idx_ledger_kind       ON ledger_entries(kind);

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
