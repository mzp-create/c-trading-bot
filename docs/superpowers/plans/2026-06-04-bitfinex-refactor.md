# Bitfinex Package (bfxapi) Refactor — Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 1067-line `market_data/bitfinex_client.py` monolith with a focused, typed `bitfinex/` package that places orders / reads positions+balance+trades through the official `bfxapi` library (ccxt confined to public OHLCV), enforces per-instance API keys in code, fixes the phantom-success/missing-fee/success-contract bugs, and migrates every caller — preserving the live-verified order semantics and the engine's outward contract to `main.py`.

**Architecture:** A new `bitfinex/` package decomposed into single-responsibility modules (`symbols`, `models`, `errors`, `keyguard`, `rest`, `ohlcv`, `paper`, `client`). `client.BitfinexClient` composes them and exposes a typed public API returning frozen dataclasses (`Order`, `Position`, `Ticker`, `Wallet`, `Fill`). Live auth calls go through bfxapi (`bfx.rest.auth.*`); public candles through a keyless ccxt instance. Paper mode simulates the auth methods, returning the *same* typed models. The synchronous engine and other callers read typed attributes; the engine maps them into its existing outward result dict and Phase-1 persistence records.

**Tech Stack:** Python 3.11, `bitfinex-api-py` (import `bfxapi`, pin v4.0.0, synchronous REST), `ccxt` 4.5.54 (OHLCV only), `pandas`, `pytest`. No network in tests — a fake bfx client is injected.

**Source of truth:** spec `docs/superpowers/specs/2026-06-04-bitfinex-refactor-design.md`.

---

## Verified bfxapi v4 facts (read before starting)

- Install: `pip install "bitfinex-api-py==4.0.0"`; import module is `bfxapi`. **Synchronous** REST (no `await`).
- Client: `from bfxapi import Client, REST_HOST` → `Client(rest_host=REST_HOST, api_key=..., api_secret=...)`. Auth methods at `bfx.rest.auth.*`, public at `bfx.rest.public.*`.
- `bfx.rest.auth.submit_order(type, symbol, amount, price, *, flags=None, lev=None, ...) -> Notification[Order]`.
  - `type` for **margin** = `"MARKET"` / `"LIMIT"` (NOT `"EXCHANGE ..."`). `price` is a **required positional** even for market (pass `0`).
  - `amount` is **signed** (`+` buy, `−` sell). Pass a **signed string** to avoid float drift.
  - **Reduce-only** = `flags=1024` (raw int — there is NO `reduce_only=` kwarg and NO `Flag` enum).
- `Notification` fields: `.status` (`"SUCCESS"`/`"ERROR"`), `.text` (reason), `.data` (the `Order`). **Order rejection is NOT an exception** — it is `status=="ERROR"`.
- `Order` attributes: `id`, `symbol`, `amount`, `amount_orig`, `order_type`, **`order_status`** (NOT `.status`), `price`, **`price_avg`**, `flags`, `mts_create`. (`.status`/`.type` belong to the *Notification*, not the Order.)
- `bfx.rest.auth.cancel_order(*, id=...) -> Notification[Order]`.
- `bfx.rest.auth.get_positions() -> List[Position]`. `Position`: `symbol`, `status`, `amount` (signed), `base_price`, `pl`, `leverage`, `position_id`, `mts_update`. Includes spot-margin positions.
- `bfx.rest.auth.get_wallets() -> List[Wallet]`. `Wallet`: `wallet_type` (`"exchange"|"margin"|"funding"`), `currency`, `balance`, `available_balance`.
- `bfx.rest.auth.get_trades_history(*, symbol=None, start=None, end=None, limit=None, sort=None) -> List[Trade]`. `Trade`: `id`, `symbol`, `order_id`, `exec_amount` (signed), `exec_price`, `fee`, `fee_currency`, `mts_create`.
- `bfx.rest.public.get_t_ticker(symbol) -> TradingPairTicker`: `bid`, `ask`, `last_price`.
- Exceptions: base `bfxapi.exceptions.BfxBaseException`; REST transport `bfxapi.rest.exceptions.GenericError`, `RequestParameterError`. Transport failures raise; order rejection does not.
- Symbols: bfxapi expects Bitfinex `t`-prefixed (`tBTCUST`; USDT is `UST`).

## Verified current call sites to migrate (clean break)

The active client returns **dicts**; consumers read keys. Each consumer below moves to the typed API. Engine constructs the client **only in live mode**; paper order simulation stays inside the engine.

- `execution/engine.py`: construct (`BitfinexClient(config, mode="live")`); `_live_execute_order` (reads `success,id,average,filled,raw.fee.cost,error`); `close_position` live branch (`success,average,order_id,error`); `_get_live_positions` (`symbol,contracts,side,entryPrice,unrealizedPnl`); `_current_price` (`last,close,bid,ask`); a balance read (`free`); `cancel_order` (`status`,`error`).
- `market_data/collector.py`: `fetch_ohlcv` → DataFrame; `fetch_ticker` (`bid,ask,last`); `fetch_orderbook` (`bids,asks`). All **public data** — mode-agnostic.
- `close_positions.py`: `fetch_positions`, `create_order(...reduceOnly)`, `client._exchange.fetch_ticker` (anti-pattern → use typed ticker).
- `monitoring/telegram_alerts.py`: `bot.collector.client.fetch_balance()` (`free`/`total` by currency).
- `scripts/live_close_smoke_test.py`: `fetch_position`, `fetch_balance`, `fetch_ticker`, `create_order`.
- Tests: `tests/test_close_path.py`, `tests/test_onreq_recovery.py`, `test_nonce_fix.py`, `test_imports.py`.

---

## File structure

**New package `bitfinex/`:**
- `bitfinex/__init__.py` — re-export `BitfinexClient` + models.
- `bitfinex/errors.py` — `BitfinexError`, `OrderRejected`, `AckUnparseable`, `KeyConflictError`.
- `bitfinex/symbols.py` — `to_bitfinex`, `to_display`.
- `bitfinex/models.py` — `Order`, `Position`, `Ticker`, `Wallet`, `Fill`.
- `bitfinex/keyguard.py` — `register_key`/`release_key` fingerprint registry (live only).
- `bitfinex/ohlcv.py` — `OhlcvFetcher` (keyless ccxt candles).
- `bitfinex/rest.py` — `BfxRest` (bfxapi auth+public wrapper → typed models).
- `bitfinex/paper.py` — `PaperBroker` (simulated auth methods → typed models).
- `bitfinex/client.py` — `BitfinexClient` public API.

**Modified:** `execution/engine.py`, `market_data/collector.py`, `close_positions.py`, `monitoring/telegram_alerts.py`, `scripts/live_close_smoke_test.py`, `tests/test_close_path.py`, `tests/test_onreq_recovery.py`, `test_nonce_fix.py`, `test_imports.py`, `.gitignore`, new `requirements.txt`.

**Deleted:** `market_data/bitfinex_client.py`, `market_data/bitfinex_client_ccxt.py`, `market_data/bitfinex_client_legacy.py`.

**Tests:** `tests/test_bfx_symbols.py`, `tests/test_bfx_models.py`, `tests/test_bfx_keyguard.py`, `tests/test_bfx_ohlcv.py`, `tests/test_bfx_rest.py`, `tests/test_bfx_paper.py`, `tests/test_bfx_client.py`.

---

## Task 1: Dependency, requirements, package skeleton + errors

**Files:**
- Create: `requirements.txt`, `bitfinex/__init__.py`, `bitfinex/errors.py`
- Test: `tests/test_bfx_models.py` (start the package import test here)

- [ ] **Step 1: Install bfxapi and record dependencies**

Run:
```bash
.venv/bin/pip install "bitfinex-api-py==4.0.0"
.venv/bin/python -c "import bfxapi; from bfxapi import Client, REST_HOST; print('bfxapi ok')"
```
Expected: prints `bfxapi ok`.

Create `requirements.txt` (pins the deps that live only in the venv today):
```
bitfinex-api-py==4.0.0
ccxt==4.5.54
pandas
numpy
scipy
scikit-learn
xgboost
joblib
PyYAML
fastapi
uvicorn
requests
pytest
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_bfx_models.py`:
```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_errors_importable():
    from bitfinex.errors import (
        BitfinexError, OrderRejected, AckUnparseable, KeyConflictError,
    )
    assert issubclass(OrderRejected, BitfinexError)
    assert issubclass(AckUnparseable, BitfinexError)
    assert issubclass(KeyConflictError, BitfinexError)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bfx_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bitfinex'`.

- [ ] **Step 4: Create the package + errors**

Create `bitfinex/__init__.py`:
```python
"""Typed Bitfinex client package (bfxapi for auth REST, ccxt for OHLCV).

BitfinexClient is the public entry point; it returns typed models (Order,
Position, Ticker, Wallet, Fill). Live auth goes through bfxapi; paper mode
simulates. See docs/superpowers/specs/2026-06-04-bitfinex-refactor-design.md.
"""
```
(The model/client re-exports are added in later tasks as those modules land.)

Create `bitfinex/errors.py`:
```python
"""Exception hierarchy for the Bitfinex package."""


class BitfinexError(Exception):
    """Base for all Bitfinex client errors."""


class OrderRejected(BitfinexError):
    """The exchange explicitly rejected the order (ack status == ERROR)."""


class AckUnparseable(BitfinexError):
    """Submit returned/raised something we cannot interpret as success or
    rejection. The order's outcome is UNKNOWN — callers must NOT auto-retry;
    reconcile via the next positions/trades read."""


class KeyConflictError(BitfinexError):
    """Another live instance is already running with this API key."""
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_bfx_models.py -v`
Expected: PASS (1 passed).

- [ ] **Step 6: Commit**

```bash
git add requirements.txt bitfinex/__init__.py bitfinex/errors.py tests/test_bfx_models.py
git commit -m "feat(bitfinex): package skeleton, errors, bfxapi dependency"
```

---

## Task 2: Symbol mapper (`symbols.py`)

**Files:**
- Create: `bitfinex/symbols.py`
- Test: `tests/test_bfx_symbols.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_bfx_symbols.py`:
```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from bitfinex.symbols import to_bitfinex, to_display


def test_to_bitfinex():
    assert to_bitfinex("BTC/USDT") == "tBTCUST"
    assert to_bitfinex("ETH/USD") == "tETHUSD"
    assert to_bitfinex("tBTCUST") == "tBTCUST"          # already bitfinex


def test_to_display():
    assert to_display("tBTCUST") == "BTC/USDT"
    assert to_display("tETHUSD") == "ETH/USD"
    assert to_display("BTC/USDT") == "BTC/USDT"          # already display


def test_to_display_tolerates_derivative_form():
    assert to_display("tBTCF0:USTF0") == "BTC/USDT"


def test_round_trip():
    for d in ("BTC/USDT", "ETH/USD"):
        assert to_display(to_bitfinex(d)) == d


def test_unknown_raises():
    with pytest.raises(ValueError):
        to_bitfinex("not-a-symbol")
    with pytest.raises(ValueError):
        to_display("garbage")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bfx_symbols.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bitfinex.symbols'`.

- [ ] **Step 3: Implement**

Create `bitfinex/symbols.py`:
```python
"""Single source of truth for symbol conversion (spot-margin).

display form:  BTC/USDT   (base/quote, what the bot and dashboard use)
bitfinex form: tBTCUST    (t-prefixed; USDT is "UST" on Bitfinex)
"""

# display quote -> bitfinex quote, and the inverse.
_DISPLAY_TO_BFX_QUOTE = {"USDT": "UST", "USD": "USD"}
_BFX_TO_DISPLAY_QUOTE = {"UST": "USDT", "USD": "USD"}


def to_bitfinex(symbol: str) -> str:
    """'BTC/USDT' -> 'tBTCUST'. Pass-through if already bitfinex form."""
    if symbol.startswith("t") and "/" not in symbol:
        return symbol
    if "/" not in symbol:
        raise ValueError(f"not a display symbol: {symbol!r}")
    base, quote = symbol.split("/", 1)
    quote = quote.split(":", 1)[0]            # drop any :USDT margin suffix
    if quote not in _DISPLAY_TO_BFX_QUOTE:
        raise ValueError(f"unsupported quote in {symbol!r}")
    return f"t{base}{_DISPLAY_TO_BFX_QUOTE[quote]}"


def to_display(symbol: str) -> str:
    """'tBTCUST' -> 'BTC/USDT'. Tolerates the 'tBTCF0:USTF0' derivative form.
    Pass-through if already display form."""
    if "/" in symbol:
        return symbol.split(":", 1)[0]
    if not symbol.startswith("t"):
        raise ValueError(f"not a bitfinex symbol: {symbol!r}")
    body = symbol[1:]
    # Derivative form, e.g. BTCF0:USTF0 -> base BTC, quote UST.
    if ":" in body:
        left = body.split(":", 1)[0]          # 'BTCF0'
        base = left.replace("F0", "")
        right = body.split(":", 1)[1]         # 'USTF0'
        bfx_quote = right.replace("F0", "")
    else:
        bfx_quote = None
        for q in _BFX_TO_DISPLAY_QUOTE:
            if body.endswith(q):
                bfx_quote = q
                base = body[: -len(q)]
                break
        if bfx_quote is None:
            raise ValueError(f"cannot parse bitfinex symbol {symbol!r}")
    if bfx_quote not in _BFX_TO_DISPLAY_QUOTE:
        raise ValueError(f"unsupported quote in {symbol!r}")
    return f"{base}/{_BFX_TO_DISPLAY_QUOTE[bfx_quote]}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_bfx_symbols.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add bitfinex/symbols.py tests/test_bfx_symbols.py
git commit -m "feat(bitfinex): single symbol mapper (display <-> tBTCUST)"
```

---

## Task 3: Typed models (`models.py`)

**Files:**
- Create: `bitfinex/models.py`
- Modify: `bitfinex/__init__.py` (re-export models)
- Test: `tests/test_bfx_models.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_bfx_models.py`:
```python
from bitfinex.models import Order, Position, Ticker, Wallet, Fill


def test_order_status_helpers():
    filled = Order(id=1, symbol="BTC/USDT", side="buy", order_type="market",
                   amount=0.5, filled=0.5, avg_price=100.0, status="EXECUTED",
                   reduce_only=False, fee=0.1, fee_currency="USDT", raw=None)
    assert filled.is_filled is True
    assert filled.is_rejected is False

    rejected = Order(id=None, symbol="BTC/USDT", side="buy", order_type="market",
                     amount=0.5, filled=0.0, avg_price=None, status="REJECTED",
                     reduce_only=False, fee=0.0, fee_currency=None, raw=None)
    assert rejected.is_filled is False
    assert rejected.is_rejected is True


def test_position_abs_amount():
    short = Position(symbol="BTC/USDT", side="short", amount=-0.3,
                     entry_price=100.0, unrealized_pnl=1.0, leverage=2.0,
                     raw_symbol="tBTCUST")
    assert short.abs_amount == 0.3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bfx_models.py -k "status_helpers or abs_amount" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bitfinex.models'`.

- [ ] **Step 3: Implement**

Create `bitfinex/models.py`:
```python
"""Typed, frozen models returned by BitfinexClient. Callers read attributes.

`status` strings are the bitfinex order-status text (e.g. 'EXECUTED',
'ACTIVE', 'CANCELED', 'REJECTED') or our paper equivalents. There is NO
success sentinel that can default to True.
"""

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class Order:
    id: Optional[int]
    symbol: str                      # display form, e.g. BTC/USDT
    side: str                        # buy | sell
    order_type: str                  # market | limit
    amount: float                    # absolute requested
    filled: float
    avg_price: Optional[float]
    status: str
    reduce_only: bool
    fee: float
    fee_currency: Optional[str]
    raw: Any = None

    @property
    def is_filled(self) -> bool:
        return self.id is not None and "EXECUTED" in (self.status or "").upper()

    @property
    def is_rejected(self) -> bool:
        s = (self.status or "").upper()
        return self.id is None or "REJECT" in s or "ERROR" in s


@dataclass(frozen=True)
class Position:
    symbol: str                      # display form
    side: str                        # long | short
    amount: float                    # signed
    entry_price: float
    unrealized_pnl: float
    leverage: float
    raw_symbol: str

    @property
    def abs_amount(self) -> float:
        return abs(self.amount)


@dataclass(frozen=True)
class Ticker:
    symbol: str
    bid: float
    ask: float
    last: float


@dataclass(frozen=True)
class Wallet:
    currency: str
    wallet_type: str                 # exchange | margin | funding
    balance: float
    available: float


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: str                        # buy | sell
    amount: float                    # absolute
    price: float
    fee: float
    fee_currency: Optional[str]
    order_id: Optional[int]
    trade_id: Optional[int]
    ts: str                          # ISO-8601 UTC
```

Update `bitfinex/__init__.py` to append:
```python
from bitfinex.models import Order, Position, Ticker, Wallet, Fill

__all__ = ["Order", "Position", "Ticker", "Wallet", "Fill"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_bfx_models.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add bitfinex/models.py bitfinex/__init__.py tests/test_bfx_models.py
git commit -m "feat(bitfinex): typed models (Order/Position/Ticker/Wallet/Fill)"
```

---

## Task 4: Key-fingerprint registry guard (`keyguard.py`)

**Files:**
- Create: `bitfinex/keyguard.py`
- Modify: `.gitignore`
- Test: `tests/test_bfx_keyguard.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_bfx_keyguard.py`:
```python
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from bitfinex.keyguard import register_key, release_key
from bitfinex.errors import KeyConflictError


def test_same_key_different_instance_conflicts(tmp_path):
    reg = str(tmp_path / "reg.json")
    register_key(reg, instance="long", api_key="KEYAAA", pid=os.getpid())
    # A different instance, same key, with a LIVE pid (this process) -> conflict.
    with pytest.raises(KeyConflictError):
        register_key(reg, instance="short", api_key="KEYAAA", pid=os.getpid())


def test_distinct_keys_ok(tmp_path):
    reg = str(tmp_path / "reg.json")
    register_key(reg, instance="long", api_key="KEYAAA", pid=os.getpid())
    register_key(reg, instance="short", api_key="KEYBBB", pid=os.getpid())  # no raise


def test_stale_pid_is_reaped(tmp_path):
    reg = str(tmp_path / "reg.json")
    dead_pid = 2_000_000_000  # not a live process
    register_key(reg, instance="long", api_key="KEYAAA", pid=dead_pid)
    # Same key, new instance, but the prior holder's pid is dead -> reaped, OK.
    register_key(reg, instance="short", api_key="KEYAAA", pid=os.getpid())


def test_registry_stores_hash_not_key(tmp_path):
    reg = str(tmp_path / "reg.json")
    register_key(reg, instance="long", api_key="SECRETKEY", pid=os.getpid())
    assert "SECRETKEY" not in Path(reg).read_text()


def test_release(tmp_path):
    reg = str(tmp_path / "reg.json")
    register_key(reg, instance="long", api_key="KEYAAA", pid=os.getpid())
    release_key(reg, api_key="KEYAAA")
    # After release, another instance may take the same key.
    register_key(reg, instance="short", api_key="KEYAAA", pid=os.getpid())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bfx_keyguard.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bitfinex.keyguard'`.

- [ ] **Step 3: Implement**

Create `bitfinex/keyguard.py`:
```python
"""Per-instance API-key registry. Prevents two LIVE instances from sharing one
API key (the dual-instance 'nonce: small' root cause). Stores only a hash."""

import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timezone

from bitfinex.errors import KeyConflictError

try:
    import fcntl  # POSIX file locking
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


def _fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text() or "{}")
    except (ValueError, OSError):
        return {}


def register_key(registry_path: str, *, instance: str, api_key: str,
                 pid: int) -> None:
    """Claim `api_key` for `instance`/`pid`. Raise KeyConflictError if another
    instance with a LIVE pid already holds the same key. Reaps dead pids."""
    path = Path(registry_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fp = _fingerprint(api_key)
    with open(path, "a+") as lockf:
        if fcntl is not None:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            reg = _load(path)
            # Reap dead holders.
            reg = {k: v for k, v in reg.items() if _pid_alive(v.get("pid", -1))}
            held = reg.get(fp)
            if held and not (held["instance"] == instance and held["pid"] == pid):
                raise KeyConflictError(
                    f"instance {held['instance']!r} (pid {held['pid']}) is "
                    f"already running with this API key; {instance!r} needs a "
                    f"separate key")
            reg[fp] = {
                "instance": instance, "pid": pid,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            path.write_text(json.dumps(reg))
        finally:
            if fcntl is not None:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


def release_key(registry_path: str, *, api_key: str) -> None:
    """Drop this key's registry entry (best-effort; called on shutdown)."""
    path = Path(registry_path)
    if not path.exists():
        return
    fp = _fingerprint(api_key)
    with open(path, "a+") as lockf:
        if fcntl is not None:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            reg = _load(path)
            reg.pop(fp, None)
            path.write_text(json.dumps(reg))
        finally:
            if fcntl is not None:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
```

Add to `.gitignore` under the SQLite block:
```
# Bitfinex per-instance key registry (runtime artifact)
data/.bfx_key_registry.json
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_bfx_keyguard.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add bitfinex/keyguard.py tests/test_bfx_keyguard.py .gitignore
git commit -m "feat(bitfinex): per-instance API-key fingerprint registry guard"
```

---

## Task 5: OHLCV fetcher (`ohlcv.py`, ccxt public)

**Files:**
- Create: `bitfinex/ohlcv.py`
- Test: `tests/test_bfx_ohlcv.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_bfx_ohlcv.py`:
```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from bitfinex.ohlcv import OhlcvFetcher


class _FakeCcxt:
    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=200):
        # ccxt returns [ts, open, high, low, close, volume] rows
        assert symbol == "BTC/USDT"          # display symbol passed straight through
        return [[1_700_000_000_000, 100.0, 110.0, 90.0, 105.0, 12.0]]


def test_fetch_ohlcv_returns_dataframe():
    f = OhlcvFetcher(exchange=_FakeCcxt())
    df = f.fetch_ohlcv("BTC/USDT", timeframe="1h", limit=1)
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df["close"].iloc[-1] == 105.0


def test_fetch_ohlcv_error_returns_none():
    class _Boom:
        def fetch_ohlcv(self, *a, **k):
            raise RuntimeError("network")
    assert OhlcvFetcher(exchange=_Boom()).fetch_ohlcv("BTC/USDT") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bfx_ohlcv.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bitfinex.ohlcv'`.

- [ ] **Step 3: Implement**

Create `bitfinex/ohlcv.py`:
```python
"""Public historical candles via ccxt (the only retained ccxt use).

Keyless ccxt instance — candles need no auth, so this never contends for the
bfxapi nonce. Display symbols in (BTC/USDT); ccxt handles its own form here.
"""

import logging
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)


class OhlcvFetcher:
    def __init__(self, exchange=None):
        if exchange is None:
            import ccxt
            exchange = ccxt.bitfinex({"enableRateLimit": True})
        self._exchange = exchange

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200,
                    since: Optional[int] = None) -> Optional[pd.DataFrame]:
        try:
            rows = self._exchange.fetch_ohlcv(symbol, timeframe, since=since,
                                              limit=limit)
        except Exception as exc:
            log.error("fetch_ohlcv(%s) failed: %s", symbol, exc)
            return None
        if not rows:
            return None
        df = pd.DataFrame(
            rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = df.set_index("timestamp")
        return df
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_bfx_ohlcv.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add bitfinex/ohlcv.py tests/test_bfx_ohlcv.py
git commit -m "feat(bitfinex): ccxt public OHLCV fetcher"
```

---

## Task 6: bfxapi REST wrapper (`rest.py`)

**Files:**
- Create: `bitfinex/rest.py`
- Test: `tests/test_bfx_rest.py`

The wrapper accepts an injected bfx client so tests need no network. Fakes mimic bfxapi's `Notification`/`Order`/`Position`/`Wallet`/`Trade`/`TradingPairTicker` attribute shapes.

- [ ] **Step 1: Write the failing test**

Create `tests/test_bfx_rest.py`:
```python
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from bitfinex.rest import BfxRest, REDUCE_ONLY
from bitfinex.errors import OrderRejected, AckUnparseable


def _order(**kw):
    base = dict(id=111, symbol="tBTCUST", amount=0.0, amount_orig=0.001,
                order_type="MARKET", order_status="EXECUTED @ 100.0",
                price=0.0, price_avg=100.0, flags=0, mts_create=1_700_000_000_000)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _notif(status, data, text=""):
    return types.SimpleNamespace(status=status, text=text, data=data,
                                 mts=1, type="on-req", message_id=None, code=None)


class _FakeAuth:
    def __init__(self):
        self.last_submit = None
    def submit_order(self, *, type, symbol, amount, price, flags=None, **k):
        self.last_submit = dict(type=type, symbol=symbol, amount=amount,
                                price=price, flags=flags)
        return _notif("SUCCESS", _order())
    def cancel_order(self, *, id):
        return _notif("SUCCESS", _order(id=id, order_status="CANCELED"))
    def get_positions(self):
        return [types.SimpleNamespace(symbol="tBTCUST", status="ACTIVE",
                amount=-0.3, base_price=100.0, pl=1.0, leverage=2.0,
                position_id=9, mts_update=1)]
    def get_wallets(self):
        return [types.SimpleNamespace(wallet_type="margin", currency="UST",
                balance=538.0, available_balance=500.0)]
    def get_trades_history(self, *, symbol=None, start=None, end=None,
                           limit=None, sort=None):
        return [types.SimpleNamespace(id=7, symbol="tBTCUST", order_id=111,
                exec_amount=0.001, exec_price=100.0, fee=-0.1, fee_currency="UST",
                mts_create=1_700_000_000_000)]


class _FakePublic:
    def get_t_ticker(self, symbol):
        return types.SimpleNamespace(bid=99.0, ask=101.0, last_price=100.0)


def _fake_client():
    return types.SimpleNamespace(
        rest=types.SimpleNamespace(auth=_FakeAuth(), public=_FakePublic()))


def _rest():
    return BfxRest(api_key="k", api_secret="s", client=_fake_client())


def test_submit_buy_market_signed_and_typed():
    r = _rest()
    order = r.submit_order("BTC/USDT", "buy", 0.001, order_type="market",
                           price=None, reduce_only=False)
    # signed positive amount string, margin MARKET type, no reduce-only flag
    sub = r._client.rest.auth.last_submit
    assert sub["type"] == "MARKET"
    assert sub["symbol"] == "tBTCUST"
    assert float(sub["amount"]) == pytest.approx(0.001)
    assert sub["flags"] in (None, 0)
    assert order.id == 111
    assert order.symbol == "BTC/USDT"           # mapped back to display
    assert order.side == "buy"
    assert order.avg_price == 100.0
    assert order.is_filled is True


def test_submit_sell_is_negative_amount():
    r = _rest()
    r.submit_order("BTC/USDT", "sell", 0.002, order_type="market", price=None,
                   reduce_only=False)
    assert float(r._client.rest.auth.last_submit["amount"]) == pytest.approx(-0.002)


def test_reduce_only_sets_flag():
    r = _rest()
    r.submit_order("BTC/USDT", "sell", 0.001, order_type="market", price=None,
                   reduce_only=True)
    assert r._client.rest.auth.last_submit["flags"] == REDUCE_ONLY


def test_rejected_notification_raises_order_rejected():
    fc = _fake_client()
    fc.rest.auth.submit_order = lambda **k: _notif("ERROR", None, "balance too low")
    r = BfxRest(api_key="k", api_secret="s", client=fc)
    with pytest.raises(OrderRejected):
        r.submit_order("BTC/USDT", "buy", 1.0, order_type="market", price=None,
                       reduce_only=False)


def test_transport_raise_becomes_ack_unparseable():
    fc = _fake_client()
    def boom(**k):
        raise RuntimeError("connection reset")
    fc.rest.auth.submit_order = boom
    r = BfxRest(api_key="k", api_secret="s", client=fc)
    with pytest.raises(AckUnparseable):
        r.submit_order("BTC/USDT", "buy", 1.0, order_type="market", price=None,
                       reduce_only=False)


def test_get_positions_typed():
    pos = _rest().get_positions()
    assert len(pos) == 1
    assert pos[0].symbol == "BTC/USDT"
    assert pos[0].side == "short"
    assert pos[0].amount == -0.3
    assert pos[0].unrealized_pnl == 1.0


def test_get_wallets_and_ticker_typed():
    r = _rest()
    w = r.get_wallets()[0]
    assert (w.currency, w.wallet_type, w.balance) == ("UST", "margin", 538.0)
    t = r.get_ticker("BTC/USDT")
    assert (t.bid, t.ask, t.last) == (99.0, 101.0, 100.0)


def test_get_trades_typed_fill():
    f = _rest().get_trades("BTC/USDT")[0]
    assert f.symbol == "BTC/USDT"
    assert f.order_id == 111
    assert f.price == 100.0
    assert f.fee == -0.1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bfx_rest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bitfinex.rest'`.

- [ ] **Step 3: Implement**

Create `bitfinex/rest.py`:
```python
"""bfxapi REST wrapper → typed models. Owns all authenticated calls + public
ticker. Accepts an injected bfx client for testing."""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from bitfinex import symbols
from bitfinex.models import Order, Position, Wallet, Fill, Ticker
from bitfinex.errors import OrderRejected, AckUnparseable

log = logging.getLogger(__name__)

REDUCE_ONLY = 1024  # Bitfinex flag bitmask (no enum in bfxapi)


class BfxRest:
    def __init__(self, api_key: str, api_secret: str, client=None):
        if client is None:
            from bfxapi import Client, REST_HOST
            client = Client(rest_host=REST_HOST, api_key=api_key,
                            api_secret=api_secret)
        self._client = client

    # ── orders ───────────────────────────────────────────────────────────
    def submit_order(self, symbol: str, side: str, amount: float, *,
                     order_type: str = "market", price: Optional[float] = None,
                     reduce_only: bool = False) -> Order:
        bfx_symbol = symbols.to_bitfinex(symbol)
        signed = abs(amount) if side.lower() == "buy" else -abs(amount)
        bfx_type = "MARKET" if order_type.lower() == "market" else "LIMIT"
        flags = REDUCE_ONLY if reduce_only else 0
        try:
            notif = self._client.rest.auth.submit_order(
                type=bfx_type, symbol=bfx_symbol, amount=f"{signed:.8f}",
                price=f"{price or 0}", flags=flags)
        except Exception as exc:  # transport/parse — outcome UNKNOWN
            raise AckUnparseable(
                f"submit_order raised, outcome unknown: {exc}") from exc
        if getattr(notif, "status", None) == "ERROR":
            raise OrderRejected(getattr(notif, "text", "order rejected"))
        return self._order_from_bfx(notif.data, symbol, side, order_type,
                                    abs(amount), reduce_only)

    def cancel_order(self, order_id: int) -> Order:
        try:
            notif = self._client.rest.auth.cancel_order(id=order_id)
        except Exception as exc:
            raise AckUnparseable(f"cancel_order raised: {exc}") from exc
        if getattr(notif, "status", None) == "ERROR":
            raise OrderRejected(getattr(notif, "text", "cancel rejected"))
        o = notif.data
        return self._order_from_bfx(o, symbols.to_display(o.symbol),
                                    "buy" if o.amount_orig >= 0 else "sell",
                                    "market", abs(o.amount_orig or 0.0), False)

    def _order_from_bfx(self, o, display_symbol, side, order_type, amount,
                        reduce_only) -> Order:
        status = (o.order_status or "").upper()
        return Order(
            id=o.id, symbol=display_symbol, side=side, order_type=order_type,
            amount=amount, filled=abs(o.amount_orig or 0.0) - abs(o.amount or 0.0),
            avg_price=(o.price_avg or None), status=status,
            reduce_only=reduce_only, fee=0.0, fee_currency=None, raw=o)

    # ── reads ────────────────────────────────────────────────────────────
    def get_positions(self) -> List[Position]:
        out = []
        for p in self._client.rest.auth.get_positions():
            amt = float(p.amount)
            out.append(Position(
                symbol=symbols.to_display(p.symbol),
                side="long" if amt > 0 else "short",
                amount=amt, entry_price=float(p.base_price or 0.0),
                unrealized_pnl=float(getattr(p, "pl", 0.0) or 0.0),
                leverage=float(getattr(p, "leverage", 0.0) or 0.0),
                raw_symbol=p.symbol))
        return out

    def get_wallets(self) -> List[Wallet]:
        return [Wallet(currency=w.currency, wallet_type=w.wallet_type,
                       balance=float(w.balance or 0.0),
                       available=float(getattr(w, "available_balance", 0.0) or 0.0))
                for w in self._client.rest.auth.get_wallets()]

    def get_ticker(self, symbol: str) -> Ticker:
        t = self._client.rest.public.get_t_ticker(symbols.to_bitfinex(symbol))
        return Ticker(symbol=symbol, bid=float(t.bid), ask=float(t.ask),
                      last=float(t.last_price))

    def get_trades(self, symbol: Optional[str] = None, since: Optional[int] = None,
                   limit: Optional[int] = None) -> List[Fill]:
        bfx_symbol = symbols.to_bitfinex(symbol) if symbol else None
        rows = self._client.rest.auth.get_trades_history(
            symbol=bfx_symbol, start=str(since) if since else None, limit=limit)
        out = []
        for t in rows:
            amt = float(t.exec_amount)
            out.append(Fill(
                symbol=symbols.to_display(t.symbol),
                side="buy" if amt > 0 else "sell", amount=abs(amt),
                price=float(t.exec_price), fee=float(t.fee or 0.0),
                fee_currency=getattr(t, "fee_currency", None),
                order_id=getattr(t, "order_id", None), trade_id=t.id,
                ts=datetime.fromtimestamp((t.mts_create or 0) / 1000,
                                          tz=timezone.utc).isoformat()))
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_bfx_rest.py -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add bitfinex/rest.py tests/test_bfx_rest.py
git commit -m "feat(bitfinex): bfxapi REST wrapper returning typed models"
```

---

## Task 7: Paper broker (`paper.py`)

**Files:**
- Create: `bitfinex/paper.py`
- Test: `tests/test_bfx_paper.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_bfx_paper.py`:
```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bitfinex.paper import PaperBroker
from bitfinex.models import Order, Position


def test_paper_open_then_position():
    pb = PaperBroker(initial_capital=1000.0)
    o = pb.submit_order("BTC/USDT", "buy", 0.5, order_type="market",
                        price=100.0, reduce_only=False)
    assert isinstance(o, Order)
    assert o.is_filled is True
    assert o.id is not None
    pos = pb.get_positions()
    assert len(pos) == 1
    assert pos[0].symbol == "BTC/USDT" and pos[0].side == "long"
    assert pos[0].amount == 0.5


def test_paper_reduce_only_close_removes_position():
    pb = PaperBroker(initial_capital=1000.0)
    pb.submit_order("BTC/USDT", "buy", 0.5, order_type="market", price=100.0,
                    reduce_only=False)
    o = pb.submit_order("BTC/USDT", "sell", 0.5, order_type="market",
                        price=110.0, reduce_only=True)
    assert o.is_filled is True
    assert pb.get_positions() == []


def test_paper_wallets():
    pb = PaperBroker(initial_capital=1000.0)
    w = pb.get_wallets()
    assert any(x.currency == "USDT" and x.balance == 1000.0 for x in w)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bfx_paper.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bitfinex.paper'`.

- [ ] **Step 3: Implement**

Create `bitfinex/paper.py`:
```python
"""Paper-mode broker: simulates auth methods, returns the SAME typed models as
the live path so callers are mode-agnostic. Public data (OHLCV/ticker) is NOT
here — it is always real/public via the client."""

from typing import List, Optional

from bitfinex.models import Order, Position, Wallet, Fill


class PaperBroker:
    def __init__(self, initial_capital: float = 1000.0, quote: str = "USDT"):
        self._capital = float(initial_capital)
        self._quote = quote
        self._positions: list[Position] = []
        self._next_id = 1

    def submit_order(self, symbol: str, side: str, amount: float, *,
                     order_type: str = "market", price: Optional[float] = None,
                     reduce_only: bool = False) -> Order:
        fill_price = float(price or 0.0)
        oid = self._next_id
        self._next_id += 1
        if reduce_only:
            self._positions = [p for p in self._positions if p.symbol != symbol]
        else:
            self._positions = [p for p in self._positions if p.symbol != symbol]
            signed = abs(amount) if side == "buy" else -abs(amount)
            self._positions.append(Position(
                symbol=symbol, side="long" if signed > 0 else "short",
                amount=signed, entry_price=fill_price, unrealized_pnl=0.0,
                leverage=1.0, raw_symbol=symbol))
        return Order(id=oid, symbol=symbol, side=side, order_type=order_type,
                     amount=abs(amount), filled=abs(amount), avg_price=fill_price,
                     status="EXECUTED", reduce_only=reduce_only, fee=0.0,
                     fee_currency=None, raw=None)

    def cancel_order(self, order_id: int) -> Order:
        return Order(id=order_id, symbol="", side="buy", order_type="market",
                     amount=0.0, filled=0.0, avg_price=None, status="CANCELED",
                     reduce_only=False, fee=0.0, fee_currency=None, raw=None)

    def get_positions(self) -> List[Position]:
        return list(self._positions)

    def get_wallets(self) -> List[Wallet]:
        return [Wallet(currency=self._quote, wallet_type="margin",
                       balance=self._capital, available=self._capital)]

    def get_trades(self, symbol: Optional[str] = None, since=None,
                   limit=None) -> List[Fill]:
        return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_bfx_paper.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add bitfinex/paper.py tests/test_bfx_paper.py
git commit -m "feat(bitfinex): paper broker returning typed models"
```

---

## Task 8: Public client (`client.py`)

**Files:**
- Create: `bitfinex/client.py`
- Modify: `bitfinex/__init__.py` (re-export `BitfinexClient`)
- Test: `tests/test_bfx_client.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_bfx_client.py`:
```python
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bitfinex.client import BitfinexClient
from bitfinex.models import Order, Position, Ticker


def _cfg(mode_capital=1000.0):
    return {"trading": {"initial_capital": mode_capital},
            "exchange": {"api_key": "", "api_secret": ""}}


def test_paper_client_orders_and_positions():
    c = BitfinexClient(_cfg(), mode="paper", instance="long")
    o = c.create_order("BTC/USDT", "buy", 0.5, order_type="market", price=100.0)
    assert isinstance(o, Order) and o.is_filled
    assert isinstance(c.fetch_positions()[0], Position)
    closed = c.close_position("BTC/USDT")
    assert closed.reduce_only is True
    assert c.fetch_positions() == []


def test_paper_client_ticker_uses_public_fetcher():
    c = BitfinexClient(_cfg(), mode="paper", instance="long")
    # Inject a fake public ticker source through the ohlcv/ticker seam.
    c._ticker_source = types.SimpleNamespace(
        get_ticker=lambda s: Ticker(symbol=s, bid=99.0, ask=101.0, last=100.0))
    t = c.fetch_ticker("BTC/USDT")
    assert t.last == 100.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bfx_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bitfinex.client'`.

- [ ] **Step 3: Implement**

Create `bitfinex/client.py`:
```python
"""BitfinexClient — public typed API composing rest/paper/ohlcv + keyguard.

- Auth methods (create_order/close_position/cancel/positions/balance/trades)
  use bfxapi in live, PaperBroker in paper.
- Public data (ticker/ohlcv) always use real public sources (mode-agnostic).
"""

import atexit
import logging
import os
from typing import List, Optional

import pandas as pd

from bitfinex import symbols
from bitfinex.models import Order, Position, Ticker, Wallet, Fill
from bitfinex.ohlcv import OhlcvFetcher
from bitfinex.paper import PaperBroker

log = logging.getLogger(__name__)

KEY_REGISTRY_PATH = "data/.bfx_key_registry.json"


class BitfinexClient:
    def __init__(self, config: dict, mode: str = "paper",
                 instance: str = "default"):
        self.mode = mode
        self.instance = instance
        self._ohlcv = OhlcvFetcher()
        self._ticker_source = None  # set to rest (live) or a public source

        if mode == "live":
            from bitfinex.rest import BfxRest
            from bitfinex import keyguard
            ex = config.get("exchange", {})
            api_key = ex.get("api_key", "")
            api_secret = ex.get("api_secret", "")
            keyguard.register_key(KEY_REGISTRY_PATH, instance=instance,
                                  api_key=api_key, pid=os.getpid())
            atexit.register(keyguard.release_key, KEY_REGISTRY_PATH,
                            api_key=api_key)
            self._auth = BfxRest(api_key=api_key, api_secret=api_secret)
            self._ticker_source = self._auth
        else:
            capital = float(config.get("trading", {}).get("initial_capital", 1000.0))
            self._auth = PaperBroker(initial_capital=capital)
            # Paper still wants REAL public tickers; build a keyless rest public.
            self._ticker_source = _PublicTicker()

    # ── orders (auth) ────────────────────────────────────────────────────
    def create_order(self, symbol: str, side: str, amount: float, *,
                     order_type: str = "market", price: Optional[float] = None,
                     reduce_only: bool = False) -> Order:
        return self._auth.submit_order(symbol, side, amount, order_type=order_type,
                                       price=price, reduce_only=reduce_only)

    def close_position(self, symbol: str) -> Order:
        pos = self.fetch_position(symbol)
        if pos is None:
            raise ValueError(f"no open position for {symbol}")
        close_side = "sell" if pos.side == "long" else "buy"
        price = self.fetch_ticker(symbol).last
        return self._auth.submit_order(symbol, close_side, pos.abs_amount,
                                       order_type="market", price=price,
                                       reduce_only=True)

    def cancel_order(self, order_id: int) -> Order:
        return self._auth.cancel_order(order_id)

    # ── reads (auth) ─────────────────────────────────────────────────────
    def fetch_positions(self) -> List[Position]:
        return self._auth.get_positions()

    def fetch_position(self, symbol: str) -> Optional[Position]:
        for p in self.fetch_positions():
            if p.symbol == symbol:
                return p
        return None

    def fetch_balance(self) -> List[Wallet]:
        return self._auth.get_wallets()

    def fetch_my_trades(self, symbol: Optional[str] = None, since=None,
                        limit=None) -> List[Fill]:
        return self._auth.get_trades(symbol, since, limit)

    # ── public data (mode-agnostic) ──────────────────────────────────────
    def fetch_ticker(self, symbol: str) -> Ticker:
        return self._ticker_source.get_ticker(symbol)

    def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200,
                  since: Optional[int] = None) -> Optional[pd.DataFrame]:
        return self._ohlcv.fetch_ohlcv(symbol, timeframe, limit, since)


class _PublicTicker:
    """Keyless public ticker via bfxapi public REST (used in paper mode)."""

    def __init__(self):
        self._pub = None

    def get_ticker(self, symbol: str) -> Ticker:
        if self._pub is None:
            from bfxapi import Client, REST_HOST
            self._pub = Client(rest_host=REST_HOST).rest.public
        t = self._pub.get_t_ticker(symbols.to_bitfinex(symbol))
        return Ticker(symbol=symbol, bid=float(t.bid), ask=float(t.ask),
                      last=float(t.last_price))
```

Update `bitfinex/__init__.py` to append:
```python
from bitfinex.client import BitfinexClient

__all__ = ["BitfinexClient", "Order", "Position", "Ticker", "Wallet", "Fill"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_bfx_client.py -v`
Expected: PASS (2 passed). The first test exercises paper orders/positions/close; the second injects a fake ticker source so no network is hit.

- [ ] **Step 5: Run the whole new package suite + commit**

Run: `.venv/bin/python -m pytest tests/test_bfx_*.py -v`
Expected: PASS (all bfx tests green).

```bash
git add bitfinex/client.py bitfinex/__init__.py tests/test_bfx_client.py
git commit -m "feat(bitfinex): BitfinexClient public typed API (paper + live)"
```

> At this point the package is complete and tested; nothing imports it yet, so the bot still runs on the old client. The remaining tasks migrate callers.

---

## Task 9: Migrate `execution/engine.py` (live paths) + retool `test_close_path.py`

**Files:**
- Modify: `execution/engine.py` (construction + `_live_execute_order` + live `close_position` branch + `_get_live_positions`/`_update_position_cache` + `_current_price` + balance read + `cancel_order`; replace engine `_normalize_symbol`/`symbol_map` with `bitfinex.symbols`)
- Modify: `tests/test_close_path.py` (stub returns typed models)
- Test: `tests/test_close_path.py`

> Engine paper paths are unchanged (they don't use the client). Only LIVE paths and symbol helpers change. The engine's outward `execute_order` result dict and the Phase-1 persistence mapping are preserved — the engine maps typed `Order`/`Position` into them.

- [ ] **Step 1: Update the test stub to the typed API**

In `tests/test_close_path.py`, the stub client's `create_order`/`fetch_ticker`/`fetch_positions` currently return dicts. Replace the stub so it returns typed models. Replace the stub's methods with:
```python
from bitfinex.models import Order, Position, Ticker

class _StubClient:
    def __init__(self, average=100.0):
        self.average = average
        self.mode = "live"
    def create_order(self, symbol, side, amount, *, order_type="market",
                     price=None, reduce_only=False):
        return Order(id=1234, symbol=symbol, side=side, order_type=order_type,
                     amount=amount, filled=amount, avg_price=self.average,
                     status="EXECUTED", reduce_only=reduce_only, fee=0.0,
                     fee_currency=None, raw=None)
    def close_position(self, symbol):
        return self.create_order(symbol, "sell", 0.3, reduce_only=True)
    def fetch_ticker(self, symbol):
        return Ticker(symbol=symbol, bid=self.average, ask=self.average,
                      last=self.average)
    def fetch_positions(self):
        return [Position(symbol="BTC/USDT", side="long", amount=0.3,
                         entry_price=100.0, unrealized_pnl=1.0, leverage=2.0,
                         raw_symbol="tBTCUST")]
    def fetch_position(self, symbol):
        for p in self.fetch_positions():
            if p.symbol == symbol:
                return p
        return None
```
Update the existing contract assertions in `test_close_path.py` that read dict keys (`result["success"]`, `result["id"]`, etc.) to read the engine's RETURNED dict (the engine still returns its outward dict — see Step 3), i.e. keep asserting on the engine's `execute_order`/`close_position` return, not on the client. Where a test constructed `BitfinexClient(cfg, mode="paper")` and called `create_order` expecting a dict, change it to assert on the typed `Order` (`.is_filled`, `.id`, `.avg_price`).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_close_path.py -v`
Expected: FAIL — engine reads dict keys off the now-typed `Order` (AttributeError / wrong values).

- [ ] **Step 3: Migrate the engine live paths**

In `execution/engine.py`:

(a) **Construction** — replace the import and live-mode client build:
```python
from bitfinex import BitfinexClient
...
if mode == "live":
    self._client = BitfinexClient(config, mode="live", instance=self.instance)
```
(`self.instance` was added in Phase 1 Task 7.)

(b) **`_live_execute_order`** — the call becomes the typed API and reads attributes; build the engine's existing internal position dict + the outward result dict from the `Order`:
```python
        try:
            order = self._client.create_order(
                symbol, side, amount, order_type=order_type, price=price)
        except (OrderRejected, AckUnparseable) as exc:
            self._log.error("Live order failed (%s): %s", type(exc).__name__, exc)
            return {"success": False, "error": str(exc)}
        # typed Order -> engine's outward result dict + internal position
        result = {
            "success": order.is_filled,
            "id": order.id,
            "average": order.avg_price or price,
            "filled": order.filled or amount,
            "fee": order.fee,
            "error": None if order.is_filled else order.status,
            "order_id": order.id,
        }
```
Add at the top of the file (with the other imports): `from bitfinex.errors import OrderRejected, AckUnparseable`. Keep the rest of `_live_execute_order` (it appends to `self._open_positions` etc.) but read from `order`/`result` instead of the old dict (`result["id"]`, `result["average"]`, `result["filled"]`).

(c) **live `close_position` branch** — replace the `self._client.create_order(..., params={"reduceOnly": True})` call with the typed call and attribute reads:
```python
                try:
                    order = self._client.create_order(
                        symbol, close_side, amount, order_type="market",
                        price=None, reduce_only=True)
                except (OrderRejected, AckUnparseable) as exc:
                    self._log.error("Failed to close position: %s", exc)
                    return {"success": False, "pnl": 0.0, "price": 0.0,
                            "error": str(exc)}
                if order.is_filled:
                    close_price = order.avg_price
                    if close_price in (None, 0, 0.0):
                        close_price = self._current_price(symbol, entry_price)
                    close_price = float(close_price)
                    # ...existing pnl math, _persist_close, _record_trade...
                    # use order.id for the persisted exchange_order_id
```
Where `_persist_close`/the live persistence read `result.get("order_id")`, pass `order.id`.

(d) **`_get_live_positions`** — `self._client.fetch_positions()` now returns `list[Position]`; map to the engine's internal dict shape (the rest of the engine reads `symbol/contracts/side/entryPrice/unrealizedPnl/amount`):
```python
        positions = []
        for p in self._client.fetch_positions():
            positions.append({
                "symbol": p.symbol, "contracts": p.abs_amount, "side": p.side,
                "entryPrice": p.entry_price, "unrealizedPnl": p.unrealized_pnl,
                "amount": p.amount,
            })
        return positions
```
This lets the engine's `_normalize_symbol` usage on positions go away (positions are already display form). Remove the now-unused engine `_normalize_symbol` and `symbol_map` builder; where the engine needed display↔exchange conversion for config symbols, call `bitfinex.symbols.to_display`/`to_bitfinex`. Add `from bitfinex import symbols as bfx_symbols` and replace `self._normalize_symbol(x)` with `bfx_symbols.to_display(x)`.

(e) **`_current_price`** — `fetch_ticker` returns a typed `Ticker`:
```python
        try:
            t = self._client.fetch_ticker(symbol)
            if t.last:
                return float(t.last)
            if t.bid and t.ask:
                return (t.bid + t.ask) / 2.0
        except Exception as exc:
            self._log.error("ticker fetch failed: %s", exc)
        return fallback
```

(f) **balance read** — wherever the engine reads `self._client.fetch_balance()` as `{free:{...}}`, change to consume `list[Wallet]`:
```python
        wallets = self._client.fetch_balance()
        free = {w.currency: w.available for w in wallets}
```
(Adjust the surrounding code that indexed `bal.get(currency, {}).get("free")` to `free.get(currency, 0.0)`.)

(g) **`cancel_order`** — returns a typed `Order` now; treat `order.status == "CANCELED"` (or no exception) as success.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_close_path.py tests/test_direction_filter.py -v`
Expected: PASS. If `test_direction_filter.py` constructs a client or reads dict results, update it the same way (typed `Order`).

- [ ] **Step 5: Commit**

```bash
git add execution/engine.py tests/test_close_path.py tests/test_direction_filter.py
git commit -m "refactor(engine): migrate live paths to typed BitfinexClient"
```

---

## Task 10: Migrate `market_data/collector.py`

**Files:**
- Modify: `market_data/collector.py`
- Test: run the existing collector-touching tests

- [ ] **Step 1: Update construction and data reads**

In `market_data/collector.py`:
- Replace `from market_data.bitfinex_client import ... create_bitfinex_client` with `from bitfinex import BitfinexClient`.
- Construction: `self._client = BitfinexClient(config, mode=mode, instance=config.get("instance", "default"))`.
- `get_current_price`: `fetch_ticker` now returns a `Ticker` — read attributes:
```python
        t = self._client.fetch_ticker(symbol)
        if t.bid > 0 and t.ask > 0:
            return (t.bid + t.ask) / 2.0
        return t.last if t.last > 0 else None
```
- `get_ohlcv`: `self._client.get_ohlcv(symbol, timeframe, limit, since=since)` (method renamed from `fetch_ohlcv` to `get_ohlcv` on the new client) — returns the same DataFrame shape (indexed by timestamp, columns open/high/low/close/volume). Update the column/index access if the old code used a `timestamp` column rather than the index (the new fetcher sets `timestamp` as the index).
- `get_orderbook`: the new client does not expose `fetch_orderbook` (YAGNI — confirm no caller needs it; the call-site report shows only `collector.get_orderbook`). If a caller needs it, add `get_orderbook` to `OhlcvFetcher`/client via the public ccxt instance; otherwise remove `collector.get_orderbook` and its callers. **Search first:** `grep -rn "get_orderbook\|fetch_orderbook" --include=*.py .` and act on the result (remove if unused; add a public ccxt passthrough if used).

- [ ] **Step 2: Verify imports + a smoke load**

Run:
```bash
.venv/bin/python -c "import market_data.collector; print('collector import ok')"
.venv/bin/python -m pytest tests/ -k "collector or ohlcv" -v
```
Expected: import ok; any collector/ohlcv tests pass.

- [ ] **Step 3: Commit**

```bash
git add market_data/collector.py
git commit -m "refactor(collector): use typed BitfinexClient (ohlcv/ticker)"
```

---

## Task 11: Migrate remaining consumers (scripts + telegram + small tests)

**Files:**
- Modify: `close_positions.py`, `monitoring/telegram_alerts.py`, `scripts/live_close_smoke_test.py`, `test_nonce_fix.py`, `test_imports.py`

- [ ] **Step 1: `monitoring/telegram_alerts.py`** — balance now returns `list[Wallet]`. Replace the `raw = ...fetch_balance(); raw.get("total")` block:
```python
        wallets = bot.collector.client.fetch_balance()
        total = {w.currency: w.balance for w in wallets}
        free = {w.currency: w.available for w in wallets}
```
Then the per-currency reads (`total.get("USDT", 0)`, etc.) work unchanged. Note Bitfinex currency is `UST` not `USDT` for the margin wallet — map it: after building `total`, add `total.setdefault("USDT", total.get("UST", 0))` so existing `USDT` lookups resolve.

- [ ] **Step 2: `close_positions.py`** — replace dict reads with typed:
```python
from bitfinex import BitfinexClient
client = BitfinexClient(config, mode="live", instance="default")
for p in client.fetch_positions():            # typed Position
    close_side = "sell" if p.side == "long" else "buy"
    order = client.create_order(p.symbol, close_side, p.abs_amount,
                                order_type="market", reduce_only=True)
    print("closed" if order.is_filled else f"failed: {order.status}")
```
Remove the `client._exchange.fetch_ticker` anti-pattern; use `client.fetch_ticker(symbol).last` if a price is needed.

- [ ] **Step 3: `scripts/live_close_smoke_test.py`** — update to the typed API (`fetch_position` → `Position`, `fetch_balance` → `list[Wallet]`, `fetch_ticker` → `Ticker`, `create_order` → `Order`). Read `.is_filled`, `.id`, `.avg_price`, `.abs_amount`, wallet `.balance`/`.currency`.

- [ ] **Step 4: `test_nonce_fix.py` and `test_imports.py`** — update construction (`from bitfinex import BitfinexClient`) and typed reads (`fetch_balance` → wallets; `fetch_positions` → `Position`; `fetch_ticker` → `Ticker`; `get_ohlcv` DataFrame). If `test_nonce_fix.py` specifically tested the old ccxt nonce behavior that no longer exists (bfxapi manages its own), retool it to assert the keyguard prevents a same-key second instance instead. `test_imports.py` should import `bitfinex` modules.

- [ ] **Step 5: Verify**

Run:
```bash
.venv/bin/python -c "import monitoring.telegram_alerts, close_positions; print('ok')"
.venv/bin/python -m pytest tests/ -v
```
Expected: imports ok; suite green (persistence + bitfinex + engine tests).

- [ ] **Step 6: Commit**

```bash
git add close_positions.py monitoring/telegram_alerts.py scripts/live_close_smoke_test.py test_nonce_fix.py test_imports.py
git commit -m "refactor: migrate remaining consumers to typed BitfinexClient"
```

---

## Task 12: Retool `test_onreq_recovery.py`, delete the old client files, final verification

**Files:**
- Modify/replace: `tests/test_onreq_recovery.py`
- Delete: `market_data/bitfinex_client.py`, `market_data/bitfinex_client_ccxt.py`, `market_data/bitfinex_client_legacy.py`
- Modify: `docs/superpowers/specs/2026-06-04-bitfinex-refactor-design.md` (status)

- [ ] **Step 1: Replace `test_onreq_recovery.py`**

The on-req array parsing is now bfxapi's job, not ours. Replace `tests/test_onreq_recovery.py` with a bfxapi-stub submit test asserting the wrapper's behavior (this duplicates the rest test's intent but keeps the historically-named regression file meaningful):
```python
import sys, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import pytest
from bitfinex.rest import BfxRest
from bitfinex.errors import OrderRejected, AckUnparseable


def _notif(status, data, text=""):
    return types.SimpleNamespace(status=status, text=text, data=data)


def _client(submit):
    auth = types.SimpleNamespace(submit_order=submit)
    return types.SimpleNamespace(rest=types.SimpleNamespace(auth=auth, public=None))


def test_success_ack_yields_real_id():
    order = types.SimpleNamespace(id=555, symbol="tBTCUST", amount=0.0,
        amount_orig=0.001, order_type="MARKET", order_status="EXECUTED",
        price=0.0, price_avg=100.0)
    r = BfxRest("k", "s", client=_client(lambda **k: _notif("SUCCESS", order)))
    o = r.submit_order("BTC/USDT", "buy", 0.001, order_type="market", price=None,
                       reduce_only=False)
    assert o.id == 555 and o.is_filled


def test_error_ack_raises():
    r = BfxRest("k", "s", client=_client(lambda **k: _notif("ERROR", None, "no")))
    with pytest.raises(OrderRejected):
        r.submit_order("BTC/USDT", "buy", 1.0, order_type="market", price=None,
                       reduce_only=False)


def test_unknown_outcome_never_retries():
    def boom(**k):
        raise RuntimeError("reset")
    r = BfxRest("k", "s", client=_client(boom))
    with pytest.raises(AckUnparseable):  # caller must NOT auto-retry
        r.submit_order("BTC/USDT", "buy", 1.0, order_type="market", price=None,
                       reduce_only=False)
```

- [ ] **Step 2: Confirm nothing imports the old client, then delete it**

Run:
```bash
grep -rn "market_data.bitfinex_client\|from market_data import bitfinex_client\|create_bitfinex_client\|PaperBitfinexClient" --include=*.py . | grep -v "_test\|tests/"
```
Expected: NO matches (all consumers migrated). If any remain, migrate them before deleting.

Then delete:
```bash
git rm market_data/bitfinex_client.py market_data/bitfinex_client_ccxt.py market_data/bitfinex_client_legacy.py
```
(The two `_ccxt`/`_legacy` files are untracked; remove them with `rm` if `git rm` reports they are not tracked.)

- [ ] **Step 3: Full suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: PASS for all bitfinex, persistence, and engine tests. The two pre-existing `test_nonce_atomic.py` failures (untracked, unrelated) may remain — confirm they are the ONLY failures and are unrelated to this work; everything touching the client must pass.

- [ ] **Step 4: Confirm the boundary invariants**

Run:
```bash
# ccxt is used ONLY for OHLCV now:
grep -rn "import ccxt\|ccxt\." --include=*.py bitfinex/ | grep -v ohlcv.py
# Expected: no matches (ccxt confined to bitfinex/ohlcv.py).
grep -rn "_parse_onreq\|private_post_auth" --include=*.py .
# Expected: no matches (raw on-req parsing fully removed).
```

- [ ] **Step 5: Update the spec status + commit**

In `docs/superpowers/specs/2026-06-04-bitfinex-refactor-design.md`, change `Status:` to:
```
Status: Implemented (Phase 2) — see docs/superpowers/plans/2026-06-04-bitfinex-refactor.md (pending live smoke-test gate)
```

```bash
git add tests/test_onreq_recovery.py docs/superpowers/specs/2026-06-04-bitfinex-refactor-design.md
git commit -m "refactor(bitfinex): retool on-req test, remove old client, finalize Phase 2"
```

---

## Task 13: Live smoke-test gate (requires explicit user go-ahead — real money)

**This task touches the real account (~\$538 collateral). Do NOT run it without the user's explicit confirmation in the session.**

- [ ] **Step 1: Confirm go-ahead**

Ask the user to confirm they want the live single-close smoke test run now, with the live API keys configured in the environment (`BITFINEX_LONG_API_KEY`/`SECRET`). Do not proceed otherwise.

- [ ] **Step 2: Run the migrated smoke test (smallest size)**

Run (paths/flags per `scripts/live_close_smoke_test.py`):
```bash
.venv/bin/python scripts/live_close_smoke_test.py --side buy --size 0.0001
```
Expected: opens a minimal margin position, confirms a real `order.id` and `EXECUTED` status, reduce-only closes it, and leaves the account flat. Capture the output.

- [ ] **Step 3: Verify flat + record result**

Confirm `fetch_positions()` returns empty and the margin wallet is intact. Record the smoke-test transcript in the commit message / a short note. If anything is left open, close it manually before stopping.

- [ ] **Step 4: Mark Phase 2 done**

Update the spec `Status:` to `Implemented (Phase 2) — live smoke test PASSED <date>` and commit.

---

## Self-review (completed by plan author)

**Spec coverage:**
- §2 transport (bfxapi for auth, ccxt OHLCV) → Tasks 5–8. ✔
- §3 package layout → Tasks 1–8 (every module). ✔
- §4 single symbol mapper → Task 2; engine converters removed in Task 9. ✔
- §5 typed models → Task 3. ✔
- §6 REST transport (submit/cancel/positions/wallets/trades/ticker, signed amount, flag 1024, OrderRejected/AckUnparseable) → Task 6. ✔
- §7 OHLCV via ccxt → Task 5. ✔
- §8 keyguard → Task 4; wired in client Task 8. ✔
- §9 public client API → Task 8. ✔
- §10 order/close semantics (margin MARKET, signed, reduce-only, no blind retry) → Tasks 6/8/9. ✔
- §11 caller migration (engine outward contract preserved) → Tasks 9–11. ✔
- §12 three bug fixes (phantom-success gone via typed status + raises; fee wired; success-contract typed) → Tasks 3/6/9. ✔
- §13 reliability → Tasks 6/9 (typed errors, failed read = no data). ✔
- §14 testing (unit per module + engine integration + live gate) → Tasks 2–13. ✔
- §15 dependency + requirements + gitignore → Tasks 1/4. ✔
- §16 DoD (package + tests, ccxt OHLCV-only, single mapper, keyguard, bugs fixed, callers migrated, old files deleted, suite green, live smoke) → Tasks 8/12/13. ✔

**Placeholder scan:** Two tasks intentionally branch on a `grep` result (Task 10 `get_orderbook`, Task 12 stray imports) — these are explicit "search first, then act" steps with both branches specified, not placeholders. No "TBD"/"add error handling"/"similar to" left.

**Type consistency:** `Order`/`Position`/`Ticker`/`Wallet`/`Fill` field names are used identically across `models.py` (Task 3), `rest.py` (Task 6), `paper.py` (Task 7), `client.py` (Task 8), and the engine migration (Task 9). `submit_order(symbol, side, amount, *, order_type, price, reduce_only)` has one signature everywhere; the client's order method is `create_order` (engine/callers) delegating to rest/paper `submit_order`. `get_ohlcv` is the client method name (collector updated in Task 10). `REDUCE_ONLY = 1024` defined once in `rest.py`.

**Deviations / notes:**
- bfxapi pinned to **v4.0.0** (latest stable; the spec said "latest v3.x" — v4's REST surface is byte-identical for the methods used; flagged in the plan).
- `Order.fee` is 0 from `submit_order` (bfxapi market acks don't carry fee until executed); real fees come from `get_trades`/`fetch_my_trades` and the Phase-3 WS `myTrades` — matches the spec's best-effort note.
- Paper mode keeps real public tickers (`_PublicTicker`) so strategies see real prices; only auth methods simulate.
