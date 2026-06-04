# WebSocket Feed + State Store — Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-cycle authenticated REST polling with a push-maintained in-memory state store fed by a background bfxapi WebSocket thread, move order placement to WS (with a synchronous cid-correlated confirmation bridge), and keep the Phase-2 bfxapi REST path as a transparent fallback — all behind the existing `BitfinexClient` so the engine barely changes.

**Architecture:** A new thread-safe `state/` package (`MarketState`, `AccountState`) is fed by `bitfinex/ws_feed.py`, a daemon thread running its own asyncio loop over `bfx.wss`. `BitfinexClient` (live) composes the feed + `BfxRest` + the two state objects and routes WS-first / REST-fallback for ticker/positions/balance/orders. A WS submit blocks on a cid-keyed confirmation future; a timeout raises `AckUnparseable` (never auto-retried). Paper mode is unchanged.

**Tech Stack:** Python 3.11, `bitfinex-api-py==4.0.0` (`bfx.wss` asyncio WebSocket + `bfx.rest`), `threading`/`asyncio`, `pytest`. Tests inject a **fake `bfx`**; no network and no real event loop is required for unit tests.

**Source of truth:** spec `docs/superpowers/specs/2026-06-04-websocket-feed-design.md`.

---

## Verified bfxapi v4 WSS facts (read before starting)

- `bfx = Client(api_key, api_secret, wss_host=...)`; the WS client is `bfx.wss` (asyncio). Run with `asyncio.run(bfx.wss.start())` inside a background thread; submit work from another thread via `asyncio.run_coroutine_threadsafe(coro, loop)`.
- Register handlers with `bfx.wss.on(event, callback)` (works as a call, not only a decorator). Callbacks may be sync.
- Public ticker: `await bfx.wss.subscribe("ticker", symbol="tBTCUST")`; event **`t_ticker_update`** → `(subscription, TradingPairTicker)` where the ticker has `.bid`, `.ask`, `.last_price` and the subscription is a dict with `"symbol"`.
- Auth (automatic when creds set) events → the **same dataclasses REST uses**: `authenticated` (dict), `position_snapshot`(list[Position]) / `position_new` / `position_update` / `position_close`(Position), `wallet_snapshot`(list[Wallet]) / `wallet_update`(Wallet), `trade_execution` / `trade_execution_update`(Trade), `order_new`/`order_update`/`order_cancel`(Order), `disconnected`.
- `Position`: `.symbol`(tBTCUST), `.status`, `.amount`(signed), `.base_price`, `.pl`, `.leverage`. `Wallet`: `.wallet_type`, `.currency`(UST), `.balance`, `.available_balance`. `Trade`: `.id`, `.symbol`, `.order_id`, `.exec_amount`(signed), `.exec_price`, `.fee`, `.fee_currency`, `.mts_create`. `Order`: `.id`, `.cid`, `.symbol`, `.amount_orig`, `.order_type`, `.order_status`, `.price_avg`.
- Order submit over WS: `await bfx.wss.inputs.submit_order(type, symbol, amount, price, *, flags=None, cid=None)` returns **None**; confirmation arrives as event **`on-req-notification`** → `Notification[Order]` with `.status`("SUCCESS"/"ERROR"), `.text`, `.data` (the `Order`, carrying our `cid`). Cancel: `await bfx.wss.inputs.cancel_order(id=...)`; confirmation `oc-req-notification`.
- **Reconnect:** auto with backoff; subscriptions + auth re-established automatically; **snapshots fire only once per emitter instance** — NOT after reconnect. So re-`authenticated` must trigger a REST reconcile.

## Current code touchpoints

- `bitfinex/client.py` (full file is short) — live branch builds `BfxRest`; methods delegate to `self._auth`/`self._ticker_source`. Phase 3 adds WS routing in the live branch only.
- `execution/engine.py:964-991` `_persist_close(...)` writes a `FillRecord` with no fee. `close_position` live branch (`engine.py:1134`) calls `_persist_close(...)`. Fee enrichment reads `client.last_fill(symbol)`.
- `main.py:879` `_shutdown()` (calls `self.executor.close_all_positions()`); add `self.executor.close()` to stop the feed. `bitfinex.rest._CCY_TO_DISPLAY` and `REDUCE_ONLY` are reused.
- Models are the Phase-2 frozen `Order/Position/Ticker/Wallet/Fill` (`bitfinex/models.py`).

## File structure

**New:**
- `state/__init__.py`, `state/market_state.py`, `state/account_state.py`
- `bitfinex/ws_feed.py`
- Tests: `tests/test_market_state.py`, `tests/test_account_state.py`, `tests/test_ws_feed.py`, `tests/test_ws_feed_orders.py`, `tests/test_client_ws_routing.py`

**Modified:** `bitfinex/client.py`, `execution/engine.py`, `main.py`, `config/default.yaml`, `config/long.yaml`, `config/short.yaml`.

---

## Task 1: MarketState (`state/market_state.py`)

**Files:**
- Create: `state/__init__.py`, `state/market_state.py`
- Test: `tests/test_market_state.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_market_state.py`:
```python
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bitfinex.models import Ticker
from state.market_state import MarketState


def test_update_and_get():
    ms = MarketState()
    assert ms.get_ticker("BTC/USDT") is None
    ms.update_ticker(Ticker(symbol="BTC/USDT", bid=99.0, ask=101.0, last=100.0))
    t = ms.get_ticker("BTC/USDT")
    assert t.last == 100.0
    assert ms.age("BTC/USDT") is not None and ms.age("BTC/USDT") < 5.0
    assert ms.is_fresh("BTC/USDT", max_age=5.0) is True


def test_unknown_symbol_not_fresh():
    ms = MarketState()
    assert ms.age("ETH/USDT") is None
    assert ms.is_fresh("ETH/USDT", max_age=5.0) is False


def test_concurrent_updates_no_crash():
    ms = MarketState()
    def worker(n):
        for i in range(200):
            ms.update_ticker(Ticker(symbol="BTC/USDT", bid=i, ask=i, last=i))
            ms.get_ticker("BTC/USDT")
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert ms.get_ticker("BTC/USDT") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_market_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'state'`.

- [ ] **Step 3: Implement**

Create `state/__init__.py`:
```python
"""Thread-safe in-memory state stores fed by the WS feed (Phase 3).

MarketState holds latest prices; AccountState holds positions/wallets/fills.
The synchronous engine reads these instead of polling REST when the feed is
healthy. See docs/superpowers/specs/2026-06-04-websocket-feed-design.md.
"""
```

Create `state/market_state.py`:
```python
"""Latest market prices, push-maintained by the WS feed (thread-safe)."""

import threading
import time
from typing import Optional

from bitfinex.models import Ticker


class MarketState:
    def __init__(self):
        self._lock = threading.Lock()
        self._tickers: dict[str, Ticker] = {}
        self._ts: dict[str, float] = {}     # display symbol -> monotonic ts

    def update_ticker(self, ticker: Ticker) -> None:
        with self._lock:
            self._tickers[ticker.symbol] = ticker
            self._ts[ticker.symbol] = time.monotonic()

    def get_ticker(self, symbol: str) -> Optional[Ticker]:
        with self._lock:
            return self._tickers.get(symbol)

    def age(self, symbol: str) -> Optional[float]:
        """Seconds since the symbol's last update, or None if never seen."""
        with self._lock:
            ts = self._ts.get(symbol)
        return None if ts is None else (time.monotonic() - ts)

    def is_fresh(self, symbol: str, max_age: float) -> bool:
        age = self.age(symbol)
        return age is not None and age <= max_age
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_market_state.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add state/__init__.py state/market_state.py tests/test_market_state.py
git commit -m "feat(state): MarketState thread-safe price store"
```

---

## Task 2: AccountState (`state/account_state.py`)

**Files:**
- Create: `state/account_state.py`
- Test: `tests/test_account_state.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_account_state.py`:
```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bitfinex.models import Position, Wallet, Fill
from state.account_state import AccountState


def _pos(symbol, amount):
    return Position(symbol=symbol, side="long" if amount > 0 else "short",
                    amount=amount, entry_price=100.0, unrealized_pnl=1.0,
                    leverage=2.0, raw_symbol="tBTCUST")


def test_status_defaults_false():
    a = AccountState()
    assert a.connected is False and a.authenticated is False
    a.set_status(connected=True, authenticated=True)
    assert a.connected and a.authenticated


def test_position_snapshot_then_incremental():
    a = AccountState()
    a.apply_position_snapshot([_pos("BTC/USDT", 0.5), _pos("ETH/USDT", -1.0)])
    assert len(a.get_positions()) == 2
    a.apply_position(_pos("BTC/USDT", 0.8))           # upsert
    assert a.get_position("BTC/USDT").amount == 0.8
    a.remove_position("ETH/USDT")                     # close
    assert a.get_position("ETH/USDT") is None
    assert len(a.get_positions()) == 1


def test_wallets_and_fills():
    a = AccountState()
    a.apply_wallet_snapshot([Wallet(currency="USDT", wallet_type="margin",
                                    balance=538.0, available=500.0)])
    a.apply_wallet(Wallet(currency="USDT", wallet_type="margin",
                          balance=540.0, available=502.0))
    assert a.get_wallets()[0].balance == 540.0
    a.apply_fill(Fill(symbol="BTC/USDT", side="sell", amount=0.5, price=110.0,
                      fee=-0.1, fee_currency="USDT", order_id=1, trade_id=7,
                      ts="2026-06-04T00:00:00+00:00"))
    assert a.last_fill("BTC/USDT").fee == -0.1
    assert a.last_fill("ETH/USDT") is None


def test_mark_reconciled():
    a = AccountState()
    assert a.last_reconcile is None
    a.mark_reconciled()
    assert a.last_reconcile is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_account_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'state.account_state'`.

- [ ] **Step 3: Implement**

Create `state/account_state.py`:
```python
"""Push-maintained account state (positions/wallets/fills), thread-safe.

Authoritative when the feed is authenticated; otherwise callers fall back to
REST. Snapshot events replace; incremental events upsert/remove.
"""

import threading
import time
from typing import List, Optional

from bitfinex.models import Position, Wallet, Fill


class AccountState:
    def __init__(self):
        self._lock = threading.Lock()
        self._positions: dict[str, Position] = {}   # display symbol -> Position
        self._wallets: dict[str, Wallet] = {}       # currency -> Wallet
        self._fills: dict[str, Fill] = {}           # display symbol -> latest Fill
        self.connected = False
        self.authenticated = False
        self.last_reconcile: Optional[float] = None

    # ── status ───────────────────────────────────────────────────────────
    def set_status(self, *, connected: bool, authenticated: bool) -> None:
        with self._lock:
            self.connected = connected
            self.authenticated = authenticated

    def mark_reconciled(self) -> None:
        with self._lock:
            self.last_reconcile = time.monotonic()

    # ── positions ────────────────────────────────────────────────────────
    def apply_position_snapshot(self, positions: List[Position]) -> None:
        with self._lock:
            self._positions = {p.symbol: p for p in positions if p.amount != 0}

    def apply_position(self, position: Position) -> None:
        with self._lock:
            if position.amount == 0:
                self._positions.pop(position.symbol, None)
            else:
                self._positions[position.symbol] = position

    def remove_position(self, symbol: str) -> None:
        with self._lock:
            self._positions.pop(symbol, None)

    def get_positions(self) -> List[Position]:
        with self._lock:
            return list(self._positions.values())

    def get_position(self, symbol: str) -> Optional[Position]:
        with self._lock:
            return self._positions.get(symbol)

    # ── wallets ──────────────────────────────────────────────────────────
    def apply_wallet_snapshot(self, wallets: List[Wallet]) -> None:
        with self._lock:
            self._wallets = {w.currency: w for w in wallets}

    def apply_wallet(self, wallet: Wallet) -> None:
        with self._lock:
            self._wallets[wallet.currency] = wallet

    def get_wallets(self) -> List[Wallet]:
        with self._lock:
            return list(self._wallets.values())

    # ── fills ────────────────────────────────────────────────────────────
    def apply_fill(self, fill: Fill) -> None:
        with self._lock:
            self._fills[fill.symbol] = fill

    def last_fill(self, symbol: str) -> Optional[Fill]:
        with self._lock:
            return self._fills.get(symbol)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_account_state.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add state/account_state.py tests/test_account_state.py
git commit -m "feat(state): AccountState thread-safe positions/wallets/fills"
```

---

## Task 3: WsFeed core — handlers, state mapping, status, lifecycle

**Files:**
- Create: `bitfinex/ws_feed.py`
- Test: `tests/test_ws_feed.py`

The feed's handler methods are plain (sync) methods unit-tested by direct calls; the asyncio thread is a thin wrapper validated with an injected fake `bfx`. Mapping reuses `bitfinex.rest` constants.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ws_feed.py`:
```python
import sys
import types
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bitfinex.ws_feed import WsFeed
from state.market_state import MarketState
from state.account_state import AccountState


def _bfx_ticker():
    return types.SimpleNamespace(bid=99.0, ask=101.0, last_price=100.0)


def _bfx_position(symbol="tBTCUST", amount=-0.3, status="ACTIVE"):
    return types.SimpleNamespace(symbol=symbol, status=status, amount=amount,
                                 base_price=100.0, pl=1.0, leverage=2.0)


def _bfx_wallet():
    return types.SimpleNamespace(wallet_type="margin", currency="UST",
                                 balance=538.0, available_balance=500.0)


def _bfx_trade():
    return types.SimpleNamespace(id=7, symbol="tBTCUST", order_id=111,
                                 exec_amount=0.5, exec_price=110.0, fee=-0.1,
                                 fee_currency="UST", mts_create=1_700_000_000_000)


def _feed():
    ms, acc = MarketState(), AccountState()
    rest = types.SimpleNamespace(get_positions=lambda: [], get_wallets=lambda: [])
    return WsFeed("k", "s", ["BTC/USDT"], ms, acc, rest), ms, acc


def test_ticker_handler_updates_market():
    feed, ms, _ = _feed()
    feed._on_ticker({"symbol": "tBTCUST"}, _bfx_ticker())
    t = ms.get_ticker("BTC/USDT")
    assert (t.bid, t.ask, t.last) == (99.0, 101.0, 100.0)


def test_position_handlers_update_account():
    feed, _, acc = _feed()
    feed._on_position_snapshot([_bfx_position()])
    assert acc.get_position("BTC/USDT").amount == -0.3
    feed._on_position(_bfx_position(amount=0.8))
    assert acc.get_position("BTC/USDT").amount == 0.8
    feed._on_position_close(_bfx_position())
    assert acc.get_position("BTC/USDT") is None


def test_wallet_and_fill_handlers():
    feed, _, acc = _feed()
    feed._on_wallet_snapshot([_bfx_wallet()])
    assert acc.get_wallets()[0].currency == "USDT"        # normalized UST->USDT
    feed._on_fill(_bfx_trade())
    f = acc.last_fill("BTC/USDT")
    assert f.fee == -0.1 and f.fee_currency == "USDT" and f.order_id == 111


def test_status_and_health():
    feed, _, acc = _feed()
    assert feed.is_healthy() is False
    feed._on_open()
    feed._on_authenticated({"userId": 1})
    assert acc.connected and acc.authenticated
    assert feed.is_healthy() is True
    feed._on_disconnected()
    assert feed.is_healthy() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ws_feed.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bitfinex.ws_feed'`.

- [ ] **Step 3: Implement (feed core)**

Create `bitfinex/ws_feed.py`:
```python
"""Background WebSocket feed: pushes ticker + auth updates into MarketState /
AccountState, and submits orders over WS with a sync cid-correlated bridge.

Runs a daemon thread with its own asyncio loop. Handler methods are sync and
directly unit-testable. On reconnect (re-auth) it REST-reconciles, because
bfxapi snapshots fire only once per connection. Reuses bitfinex.rest mapping.
"""

import asyncio
import itertools
import logging
import threading
import time
from datetime import datetime, timezone
from typing import List, Optional

from bitfinex import symbols
from bitfinex.models import Order, Position, Ticker, Wallet, Fill
from bitfinex.errors import OrderRejected, AckUnparseable
from bitfinex.rest import REDUCE_ONLY, _CCY_TO_DISPLAY

log = logging.getLogger(__name__)


def _iso(mts: Optional[int]) -> str:
    return datetime.fromtimestamp((mts or 0) / 1000, tz=timezone.utc).isoformat()


def _ccy(c):
    return _CCY_TO_DISPLAY.get(c, c)


class WsFeed:
    def __init__(self, api_key: str, api_secret: str, symbols_list: List[str],
                 market_state, account_state, rest, *,
                 ticker_staleness: float = 15.0,
                 order_confirm_timeout: float = 10.0,
                 reconcile_interval: float = 300.0,
                 wss_host: Optional[str] = None, bfx=None):
        self._api_key = api_key
        self._api_secret = api_secret
        self._symbols = list(symbols_list)
        self._market = market_state
        self._account = account_state
        self._rest = rest
        self.ticker_staleness = ticker_staleness
        self._order_confirm_timeout = order_confirm_timeout
        self._reconcile_interval = reconcile_interval
        self._wss_host = wss_host
        self._bfx = bfx                         # injectable; built in start()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._cid = itertools.count(1)
        self._pending: dict[int, tuple] = {}    # cid -> (Event, holder)
        self._pending_lock = threading.Lock()
        self._auth_count = 0

    # ── mapping ──────────────────────────────────────────────────────────
    def _pos(self, p) -> Position:
        amt = float(p.amount)
        return Position(symbol=symbols.to_display(p.symbol),
                        side="long" if amt > 0 else "short", amount=amt,
                        entry_price=float(getattr(p, "base_price", 0.0) or 0.0),
                        unrealized_pnl=float(getattr(p, "pl", 0.0) or 0.0),
                        leverage=float(getattr(p, "leverage", 0.0) or 0.0),
                        raw_symbol=p.symbol)

    def _wallet(self, w) -> Wallet:
        return Wallet(currency=_ccy(w.currency), wallet_type=w.wallet_type,
                      balance=float(w.balance or 0.0),
                      available=float(getattr(w, "available_balance", 0.0) or 0.0))

    def _fill(self, t) -> Fill:
        amt = float(t.exec_amount)
        return Fill(symbol=symbols.to_display(t.symbol),
                    side="buy" if amt > 0 else "sell", amount=abs(amt),
                    price=float(t.exec_price), fee=float(t.fee or 0.0),
                    fee_currency=_ccy(getattr(t, "fee_currency", None)),
                    order_id=getattr(t, "order_id", None), trade_id=t.id,
                    ts=_iso(getattr(t, "mts_create", 0)))

    def _order(self, o) -> Order:
        status = (getattr(o, "order_status", "") or "").upper()
        amt = float(getattr(o, "amount_orig", 0.0) or 0.0)
        return Order(id=o.id, symbol=symbols.to_display(o.symbol),
                     side="buy" if amt >= 0 else "sell",
                     order_type="market", amount=abs(amt), filled=abs(amt),
                     avg_price=(getattr(o, "price_avg", None) or None),
                     status=status, reduce_only=False, fee=0.0,
                     fee_currency=None, raw=o)

    # ── handlers (sync; unit-tested directly) ────────────────────────────
    def _on_open(self):
        self._account.set_status(connected=True,
                                 authenticated=self._account.authenticated)

    def _on_authenticated(self, data=None):
        self._auth_count += 1
        self._account.set_status(connected=True, authenticated=True)
        if self._auth_count > 1:        # reconnect: snapshot won't refire
            self._reconcile()

    def _on_disconnected(self, *a):
        self._account.set_status(connected=False, authenticated=False)

    def _on_ticker(self, sub, ticker):
        disp = symbols.to_display(sub["symbol"])
        self._market.update_ticker(Ticker(symbol=disp, bid=float(ticker.bid),
                                          ask=float(ticker.ask),
                                          last=float(ticker.last_price)))

    def _on_position_snapshot(self, positions):
        self._account.apply_position_snapshot([self._pos(p) for p in positions])

    def _on_position(self, p):
        self._account.apply_position(self._pos(p))

    def _on_position_close(self, p):
        self._account.remove_position(symbols.to_display(p.symbol))

    def _on_wallet_snapshot(self, wallets):
        self._account.apply_wallet_snapshot([self._wallet(w) for w in wallets])

    def _on_wallet(self, w):
        self._account.apply_wallet(self._wallet(w))

    def _on_fill(self, t):
        self._account.apply_fill(self._fill(t))

    def _reconcile(self):
        try:
            self._account.apply_position_snapshot(self._rest.get_positions())
            self._account.apply_wallet_snapshot(self._rest.get_wallets())
            self._account.mark_reconciled()
            log.info("WS feed reconciled via REST")
        except Exception as exc:
            log.error("WS reconcile failed: %s", exc)

    # ── health ───────────────────────────────────────────────────────────
    def is_healthy(self) -> bool:
        return self._account.connected and self._account.authenticated

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="bfx-wss",
                                        daemon=True)
        self._thread.start()

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        if self._bfx is None:
            from bfxapi import Client
            kwargs = {"api_key": self._api_key, "api_secret": self._api_secret}
            if self._wss_host:
                kwargs["wss_host"] = self._wss_host
            self._bfx = Client(**kwargs)
        self._register_handlers()
        try:
            loop.run_until_complete(self._bfx.wss.start())
        except Exception as exc:                # never crash the process
            log.error("WS feed loop exited: %s", exc)
        finally:
            loop.close()

    def _register_handlers(self):
        wss = self._bfx.wss

        @wss.on("open")
        async def _open():
            self._safe(self._on_open)
            for s in self._symbols:
                await wss.subscribe("ticker", symbol=symbols.to_bitfinex(s))

        wss.on("authenticated", lambda data=None: self._safe(self._on_authenticated, data))
        wss.on("disconnected", lambda *a: self._safe(self._on_disconnected))
        wss.on("t_ticker_update", lambda sub, tk: self._safe(self._on_ticker, sub, tk))
        wss.on("position_snapshot", lambda ps: self._safe(self._on_position_snapshot, ps))
        wss.on("position_new", lambda p: self._safe(self._on_position, p))
        wss.on("position_update", lambda p: self._safe(self._on_position, p))
        wss.on("position_close", lambda p: self._safe(self._on_position_close, p))
        wss.on("wallet_snapshot", lambda ws: self._safe(self._on_wallet_snapshot, ws))
        wss.on("wallet_update", lambda w: self._safe(self._on_wallet, w))
        wss.on("trade_execution", lambda t: self._safe(self._on_fill, t))
        wss.on("trade_execution_update", lambda t: self._safe(self._on_fill, t))
        wss.on("on-req-notification", lambda n: self._safe(self._on_req_notification, n))
        wss.on("oc-req-notification", lambda n: self._safe(self._on_req_notification, n))

    def _safe(self, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:                # a bad event never kills the loop
            log.error("WS handler %s failed: %s", getattr(fn, "__name__", fn), exc)

    def stop(self) -> None:
        loop, thread, bfx = self._loop, self._thread, self._bfx
        if loop is None or thread is None:
            return
        async def _close():
            try:
                await bfx.wss.close()
            except Exception:
                pass
        try:
            fut = asyncio.run_coroutine_threadsafe(_close(), loop)
            fut.result(timeout=5)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        self._thread = None

    # ── order bridge (Task 4 adds submit/cancel + _on_req_notification) ──
    def _on_req_notification(self, notif):
        raise NotImplementedError  # implemented in Task 4
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_ws_feed.py -v`
Expected: PASS (4 passed). The handler tests call the sync methods directly; no loop/network is started.

- [ ] **Step 5: Commit**

```bash
git add bitfinex/ws_feed.py tests/test_ws_feed.py
git commit -m "feat(ws): WsFeed core — handlers push to state, lifecycle, health"
```

---

## Task 4: WsFeed order bridge + reconnect reconcile

**Files:**
- Modify: `bitfinex/ws_feed.py` (replace the `_on_req_notification` stub; add `submit_order_sync`/`cancel_order_sync` + `_send_submit`/`_send_cancel`)
- Test: `tests/test_ws_feed_orders.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_ws_feed_orders.py`:
```python
import sys
import types
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from bitfinex.ws_feed import WsFeed
from bitfinex.errors import OrderRejected, AckUnparseable
from state.market_state import MarketState
from state.account_state import AccountState


def _feed(**kw):
    rest = types.SimpleNamespace(
        get_positions=lambda: [], get_wallets=lambda: [])
    return WsFeed("k", "s", ["BTC/USDT"], MarketState(), AccountState(), rest,
                  order_confirm_timeout=kw.get("timeout", 1.0))


def _bfx_order(cid, status="EXECUTED @ 100.0"):
    return types.SimpleNamespace(id=555, cid=cid, symbol="tBTCUST",
                                 amount_orig=0.001, order_type="MARKET",
                                 order_status=status, price_avg=100.0)


def _notif(status, data, text=""):
    return types.SimpleNamespace(status=status, text=text, data=data)


def test_submit_resolves_on_success_notification():
    feed = _feed()
    def fake_send(symbol, side, amount, order_type, price, reduce_only, cid):
        # simulate the exchange replying asynchronously
        threading.Timer(0.02, lambda: feed._on_req_notification(
            _notif("SUCCESS", _bfx_order(cid)))).start()
    feed._send_submit = fake_send
    order = feed.submit_order_sync("BTC/USDT", "buy", 0.001, price=100.0)
    assert order.id == 555 and order.is_accepted and order.symbol == "BTC/USDT"


def test_submit_error_raises_order_rejected():
    feed = _feed()
    def fake_send(*a):
        cid = a[-1]
        threading.Timer(0.02, lambda: feed._on_req_notification(
            _notif("ERROR", None, "balance too low"))).start()
        feed._last_cid = cid  # not needed; ERROR path matches by cid below
    # ERROR notification must carry the cid too — emulate via data with cid:
    def fake_send2(symbol, side, amount, order_type, price, reduce_only, cid):
        threading.Timer(0.02, lambda: feed._on_req_notification(
            _notif("ERROR", types.SimpleNamespace(cid=cid), "balance too low"))).start()
    feed._send_submit = fake_send2
    with pytest.raises(OrderRejected):
        feed.submit_order_sync("BTC/USDT", "buy", 1.0, price=100.0)


def test_submit_timeout_raises_ack_unparseable():
    feed = _feed(timeout=0.2)
    feed._send_submit = lambda *a: None        # no reply ever
    with pytest.raises(AckUnparseable):
        feed.submit_order_sync("BTC/USDT", "buy", 1.0, price=100.0)


def test_reconnect_reauth_triggers_reconcile():
    calls = {"pos": 0, "wal": 0}
    rest = types.SimpleNamespace(
        get_positions=lambda: (calls.__setitem__("pos", calls["pos"] + 1) or []),
        get_wallets=lambda: (calls.__setitem__("wal", calls["wal"] + 1) or []))
    feed = WsFeed("k", "s", ["BTC/USDT"], MarketState(), AccountState(), rest)
    feed._on_authenticated({})      # first auth: no reconcile (snapshot will come)
    assert calls["pos"] == 0
    feed._on_authenticated({})      # re-auth: reconcile
    assert calls["pos"] == 1 and calls["wal"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ws_feed_orders.py -v`
Expected: FAIL — `_on_req_notification` raises `NotImplementedError` / `submit_order_sync` missing.

- [ ] **Step 3: Implement**

In `bitfinex/ws_feed.py`, REPLACE the `_on_req_notification` stub at the bottom with the full bridge:
```python
    # ── order bridge ─────────────────────────────────────────────────────
    def submit_order_sync(self, symbol: str, side: str, amount: float, *,
                          order_type: str = "market",
                          price: Optional[float] = None,
                          reduce_only: bool = False,
                          timeout: Optional[float] = None) -> Order:
        timeout = timeout or self._order_confirm_timeout
        cid = next(self._cid)
        ev, holder = threading.Event(), {}
        with self._pending_lock:
            self._pending[cid] = (ev, holder)
        try:
            self._send_submit(symbol, side, amount, order_type, price,
                              reduce_only, cid)
            if not ev.wait(timeout):
                raise AckUnparseable(
                    f"WS order cid={cid} unconfirmed within {timeout}s")
            if "error" in holder:
                raise holder["error"]
            return holder["order"]
        finally:
            with self._pending_lock:
                self._pending.pop(cid, None)

    def cancel_order_sync(self, order_id: int,
                          timeout: Optional[float] = None) -> Order:
        timeout = timeout or self._order_confirm_timeout
        cid = next(self._cid)
        ev, holder = threading.Event(), {}
        with self._pending_lock:
            self._pending[cid] = (ev, holder)
        try:
            self._send_cancel(order_id, cid)
            if not ev.wait(timeout):
                raise AckUnparseable(
                    f"WS cancel cid={cid} unconfirmed within {timeout}s")
            if "error" in holder:
                raise holder["error"]
            return holder["order"]
        finally:
            with self._pending_lock:
                self._pending.pop(cid, None)

    def _send_submit(self, symbol, side, amount, order_type, price,
                     reduce_only, cid):
        bfx_symbol = symbols.to_bitfinex(symbol)
        signed = abs(amount) if side.lower() == "buy" else -abs(amount)
        bfx_type = "MARKET" if order_type.lower() == "market" else "LIMIT"
        flags = REDUCE_ONLY if reduce_only else 0
        coro = self._bfx.wss.inputs.submit_order(
            type=bfx_type, symbol=bfx_symbol, amount=f"{signed:.8f}",
            price=f"{price or 0}", flags=flags, cid=cid)
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _send_cancel(self, order_id, cid):
        coro = self._bfx.wss.inputs.cancel_order(id=order_id, cid=cid)
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _on_req_notification(self, notif):
        data = getattr(notif, "data", None)
        cid = getattr(data, "cid", None)
        if cid is None:
            return
        with self._pending_lock:
            entry = self._pending.get(cid)
        if entry is None:
            return
        ev, holder = entry
        if getattr(notif, "status", None) == "ERROR":
            holder["error"] = OrderRejected(getattr(notif, "text", "rejected"))
        else:
            holder["order"] = self._order(data)
        ev.set()
```

> `_send_submit`/`_send_cancel` use `self._loop` (set in `_run`); unit tests monkeypatch them so no loop is needed. `_on_req_notification` correlates by `cid`, which bfxapi echoes on `notif.data.cid` for both SUCCESS and ERROR.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_ws_feed_orders.py tests/test_ws_feed.py -v`
Expected: PASS (8 passed total).

- [ ] **Step 5: Commit**

```bash
git add bitfinex/ws_feed.py tests/test_ws_feed_orders.py
git commit -m "feat(ws): order sync bridge (cid-correlated) + reconnect reconcile"
```

---

## Task 5: Config — `exchange.ws` block

**Files:**
- Modify: `config/default.yaml`, `config/long.yaml`, `config/short.yaml`

- [ ] **Step 1: Add the ws block to `config/default.yaml`**

In `config/default.yaml`, in the `exchange:` block (which has `ws_reconnect_delay: 5`), add a nested `ws:` mapping right after the `name`/`rate_limit` lines:
```yaml
  ws:
    enabled: true                       # false => pure Phase-2 REST behavior
    wss_host: "wss://api.bitfinex.com/ws/2"
    ticker_staleness_seconds: 15
    order_confirm_timeout_seconds: 10
    reconcile_interval_seconds: 300
```

- [ ] **Step 2: Mirror it into `config/long.yaml` and `config/short.yaml`**

Add the identical `ws:` block under each instance config's `exchange:` block.

- [ ] **Step 3: Verify YAML loads**

Run:
```bash
.venv/bin/python -c "import yaml; [print(yaml.safe_load(open(f'config/{n}.yaml'))['exchange']['ws']['enabled']) for n in ('default','long','short')]"
```
Expected: prints `True` three times.

- [ ] **Step 4: Commit**

```bash
git add config/default.yaml config/long.yaml config/short.yaml
git commit -m "feat(ws): exchange.ws config block (enabled kill-switch + tunables)"
```

---

## Task 6: BitfinexClient live WS routing

**Files:**
- Modify: `bitfinex/client.py`
- Test: `tests/test_client_ws_routing.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_client_ws_routing.py`:
```python
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from bitfinex.client import BitfinexClient
from bitfinex.models import Order, Position, Ticker, Wallet, Fill


class _FakeFeed:
    def __init__(self, healthy=True):
        self._healthy = healthy
        self.submitted = None
    def is_healthy(self):
        return self._healthy
    def submit_order_sync(self, symbol, side, amount, *, order_type="market",
                          price=None, reduce_only=False):
        self.submitted = (symbol, side, amount, reduce_only)
        return Order(id=1, symbol=symbol, side=side, order_type=order_type,
                     amount=amount, filled=amount, avg_price=100.0,
                     status="EXECUTED", reduce_only=reduce_only, fee=0.0,
                     fee_currency=None, raw=None)
    def start(self): pass
    def stop(self): pass


def _live_client(healthy=True, fresh=True):
    # Build a paper client then swap in WS internals to avoid live construction.
    c = BitfinexClient({"trading": {"initial_capital": 1000.0}},
                       mode="paper", instance="long")
    from state.market_state import MarketState
    from state.account_state import AccountState
    ms, acc = MarketState(), AccountState()
    if fresh:
        ms.update_ticker(Ticker(symbol="BTC/USDT", bid=99, ask=101, last=100.0))
    acc.set_status(connected=healthy, authenticated=healthy)
    if healthy:
        acc.apply_position_snapshot([Position(symbol="BTC/USDT", side="long",
            amount=0.5, entry_price=100.0, unrealized_pnl=1.0, leverage=2.0,
            raw_symbol="tBTCUST")])
        acc.apply_wallet_snapshot([Wallet(currency="USDT", wallet_type="margin",
            balance=538.0, available=500.0)])
    rest = types.SimpleNamespace(
        get_ticker=lambda s: Ticker(symbol=s, bid=1, ask=1, last=1.0),
        get_positions=lambda: [],
        get_wallets=lambda: [Wallet(currency="USDT", wallet_type="margin",
                                    balance=1.0, available=1.0)],
        submit_order=lambda *a, **k: Order(id=9, symbol="BTC/USDT", side="buy",
            order_type="market", amount=1.0, filled=1.0, avg_price=1.0,
            status="EXECUTED", reduce_only=k.get("reduce_only", False),
            fee=0.0, fee_currency=None, raw=None),
        get_trades=lambda *a, **k: [])
    feed = _FakeFeed(healthy=healthy)
    c._enable_ws(market=ms, account=acc, feed=feed, rest=rest,
                 ticker_staleness=15.0)
    return c, feed, rest


def test_fresh_ticker_from_state():
    c, _, _ = _live_client(fresh=True)
    assert c.fetch_ticker("BTC/USDT").last == 100.0     # from MarketState


def test_stale_ticker_falls_back_to_rest():
    c, _, _ = _live_client(healthy=True, fresh=False)
    assert c.fetch_ticker("BTC/USDT").last == 1.0       # from REST fallback


def test_positions_from_state_when_authenticated():
    c, _, _ = _live_client(healthy=True)
    assert c.fetch_positions()[0].symbol == "BTC/USDT"


def test_positions_fall_back_to_rest_when_unauthenticated():
    c, _, _ = _live_client(healthy=False)
    assert c.fetch_positions() == []                    # REST returns []


def test_order_routes_to_ws_when_healthy():
    c, feed, _ = _live_client(healthy=True)
    o = c.create_order("BTC/USDT", "buy", 0.001, price=100.0)
    assert feed.submitted == ("BTC/USDT", "buy", 0.001, False)
    assert o.id == 1


def test_order_routes_to_rest_when_unhealthy():
    c, feed, _ = _live_client(healthy=False)
    o = c.create_order("BTC/USDT", "buy", 0.001, price=100.0)
    assert feed.submitted is None and o.id == 9         # REST path
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_client_ws_routing.py -v`
Expected: FAIL — `BitfinexClient` has no `_enable_ws`.

- [ ] **Step 3: Implement**

In `bitfinex/client.py`:

(a) Add WS attributes in `__init__` (both modes) and start the feed in live mode. Replace the `if mode == "live":` block's body and the `else` block so live builds the feed when `exchange.ws.enabled`:
```python
        self._market = None
        self._account = None
        self._feed = None
        self._ticker_staleness = 15.0

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
            ws_cfg = ex.get("ws", {})
            if ws_cfg.get("enabled", True):
                self._start_ws(config, api_key, api_secret, ws_cfg)
        else:
            capital = float(config.get("trading", {}).get("initial_capital", 1000.0))
            self._auth = PaperBroker(initial_capital=capital)
            self._ticker_source = _PublicTicker()
```

(b) Add the WS wiring + a test seam, after `__init__`:
```python
    def _start_ws(self, config, api_key, api_secret, ws_cfg):
        from state.market_state import MarketState
        from state.account_state import AccountState
        from bitfinex.ws_feed import WsFeed
        symbols_cfg = config.get("trading", {}).get("symbols", [])
        names = [s.get("name") for s in symbols_cfg if s.get("name")] or ["BTC/USDT"]
        market, account = MarketState(), AccountState()
        feed = WsFeed(
            api_key, api_secret, names, market, account, self._auth,
            ticker_staleness=float(ws_cfg.get("ticker_staleness_seconds", 15)),
            order_confirm_timeout=float(ws_cfg.get("order_confirm_timeout_seconds", 10)),
            reconcile_interval=float(ws_cfg.get("reconcile_interval_seconds", 300)),
            wss_host=ws_cfg.get("wss_host"))
        self._enable_ws(market=market, account=account, feed=feed,
                        rest=self._auth,
                        ticker_staleness=float(ws_cfg.get("ticker_staleness_seconds", 15)))
        feed.start()

    def _enable_ws(self, *, market, account, feed, rest, ticker_staleness):
        """Wire WS state/feed into the client (also the test seam)."""
        self._market = market
        self._account = account
        self._feed = feed
        self._auth = rest          # REST fallback for reads/orders
        self._ticker_source = rest  # ticker REST fallback == BfxRest (live)
        self._ticker_staleness = ticker_staleness
```

(c) Route the methods. Replace `create_order`, `close_position`, `fetch_positions`, `fetch_balance`, `fetch_ticker`, and add `last_fill`/`close`:
```python
    def _ws_healthy(self) -> bool:
        return self._feed is not None and self._feed.is_healthy()

    def create_order(self, symbol: str, side: str, amount: float, *,
                     order_type: str = "market", price: Optional[float] = None,
                     reduce_only: bool = False) -> Order:
        if self._ws_healthy():
            return self._feed.submit_order_sync(
                symbol, side, amount, order_type=order_type, price=price,
                reduce_only=reduce_only)
        return self._auth.submit_order(symbol, side, amount, order_type=order_type,
                                       price=price, reduce_only=reduce_only)

    def close_position(self, symbol: str) -> Order:
        pos = self.fetch_position(symbol)
        if pos is None:
            raise ValueError(f"no open position for {symbol}")
        close_side = "sell" if pos.side == "long" else "buy"
        price = self.fetch_ticker(symbol).last
        return self.create_order(symbol, close_side, pos.abs_amount,
                                 order_type="market", price=price,
                                 reduce_only=True)

    def fetch_positions(self) -> List[Position]:
        if self._account is not None and self._account.authenticated:
            return self._account.get_positions()
        return self._auth.get_positions()

    def fetch_balance(self) -> List[Wallet]:
        if self._account is not None and self._account.authenticated:
            return self._account.get_wallets()
        return self._auth.get_wallets()

    def fetch_ticker(self, symbol: str) -> Ticker:
        if (self._ws_healthy() and self._market is not None
                and self._market.is_fresh(symbol, self._ticker_staleness)):
            t = self._market.get_ticker(symbol)
            if t is not None:
                return t
        return self._ticker_source.get_ticker(symbol)

    def last_fill(self, symbol: str) -> Optional[Fill]:
        if self._account is not None:
            return self._account.last_fill(symbol)
        return None

    def close(self) -> None:
        if self._feed is not None:
            self._feed.stop()
```
Leave `cancel_order` delegating to `self._auth.cancel_order` (REST) for now — WS cancel exists (`feed.cancel_order_sync`) but the engine's cancel path is rare; route it WS-first too for consistency:
```python
    def cancel_order(self, order_id: int) -> Order:
        if self._ws_healthy():
            return self._feed.cancel_order_sync(order_id)
        return self._auth.cancel_order(order_id)
```
`fetch_my_trades` stays REST-backed (full history isn't in state); leave as-is.

> Note: `fetch_ticker` keeps using `self._ticker_source` for fallback (live: `BfxRest`; paper: `_PublicTicker`). In live, `self._ticker_source is self._auth` (BfxRest), so the REST fallback and ticker source are the same object — correct.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_client_ws_routing.py tests/test_bfx_client.py -v`
Expected: PASS. (`test_bfx_client.py` paper tests still pass — paper never enables WS; `_feed`/`_account` are None so all routing falls to the paper `_auth`/`_ticker_source`.)

- [ ] **Step 5: Commit**

```bash
git add bitfinex/client.py tests/test_client_ws_routing.py
git commit -m "feat(ws): BitfinexClient WS-first/REST-fallback routing + feed lifecycle"
```

---

## Task 7: Fee enrichment + shutdown wiring

**Files:**
- Modify: `execution/engine.py` (`_persist_close` accepts fee; live close passes it from `client.last_fill`; add `close()`)
- Modify: `main.py` (`_shutdown` calls `self.executor.close()`)
- Test: `tests/test_engine_fee_enrichment.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_engine_fee_enrichment.py`:
```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.engine import ExecutionEngine
from bitfinex.models import Fill


def _engine(tmp_path):
    config = {"data": {"trades_file": str(tmp_path / "trades.csv"),
                       "db_file": str(tmp_path / "trading.db")},
              "exchange": {"rate_limit": 0.0}}
    return ExecutionEngine(config, mode="paper", trade_direction="both",
                           instance="long")


def test_persist_close_records_fee_when_provided(tmp_path):
    eng = _engine(tmp_path)
    eng._persist_close(symbol="BTC/USDT", side="buy", close_side="sell",
                       amount=0.5, entry_price=100.0, close_price=110.0,
                       pnl=5.0, reason="take_profit", opened_at=None,
                       fee=0.25, fee_currency="USDT")
    row = eng._repo._conn.execute(
        "SELECT fee, fee_currency FROM fills ORDER BY id DESC LIMIT 1").fetchone()
    assert row["fee"] == 0.25 and row["fee_currency"] == "USDT"


def test_engine_close_noop_safe(tmp_path):
    eng = _engine(tmp_path)
    eng.close()        # paper: no client / no-op, must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_engine_fee_enrichment.py -v`
Expected: FAIL — `_persist_close` has no `fee`/`fee_currency` params; `close` undefined.

- [ ] **Step 3: Implement**

In `execution/engine.py`, change `_persist_close` signature and the `record_fill` call (currently `engine.py:964-991`):
```python
    def _persist_close(self, *, symbol, side, close_side, amount, entry_price,
                       close_price, pnl, reason, opened_at,
                       exchange_order_id=None, fee=0.0, fee_currency=None):
```
and the fill write:
```python
        self._repo.record_fill(FillRecord(
            ts=now, symbol=symbol, side=close_side, amount=abs(amount),
            price=close_price, order_id=close_oid, fee=fee,
            fee_currency=fee_currency))
```
(`FillRecord` already has `fee`/`fee_currency` fields from Phase 1.)

In the **live** branch of `close_position` (the call at `engine.py:1134` `self._persist_close(...)`), enrich from the WS/REST fill if the client exposes one. Right before that `_persist_close(...)` call, add:
```python
                    fill = None
                    if hasattr(self._client, "last_fill"):
                        try:
                            fill = self._client.last_fill(symbol)
                        except Exception:
                            fill = None
                    fee = fill.fee if fill is not None else 0.0
                    fee_ccy = fill.fee_currency if fill is not None else None
```
and pass `fee=fee, fee_currency=fee_ccy` into that `_persist_close(...)` call (keep the existing args). Leave the paper-branch `_persist_close(...)` call unchanged (defaults fee=0).

Add an engine `close()` near the other lifecycle methods:
```python
    def close(self) -> None:
        """Stop the live WS feed (if any). Safe in paper mode."""
        client = getattr(self, "_client", None)
        if client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception as exc:
                self._log.error("client close failed: %s", exc)
```

In `main.py` `_shutdown` (`main.py:879`), after `self.executor.close_all_positions()` add:
```python
        try:
            self.executor.close()
        except Exception:
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_engine_fee_enrichment.py tests/test_close_path.py -v`
Expected: PASS. (`test_close_path.py` regression: the live-close stub has no `last_fill`, so `hasattr` is False and fee stays 0 — unchanged behavior.)

- [ ] **Step 5: Commit**

```bash
git add execution/engine.py main.py tests/test_engine_fee_enrichment.py
git commit -m "feat(ws): enrich close FillRecord with WS fee; wire feed shutdown"
```

---

## Task 8: Full regression + kill-switch verification

**Files:**
- Test only (no production change unless a regression is found)

- [ ] **Step 1: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q 2>&1 | tail -8`
Expected: all state/ws/bitfinex/persistence/engine tests pass. The only allowed failures are the pre-existing, unrelated `tests/test_nonce_atomic.py` (2). If anything else fails, fix it before proceeding.

- [ ] **Step 2: Confirm the kill-switch path**

Run:
```bash
.venv/bin/python -c "
from bitfinex.client import BitfinexClient
# ws disabled => no feed, pure REST/paper behavior; paper construction is safe
c = BitfinexClient({'trading': {'initial_capital': 1000.0},
                    'exchange': {'ws': {'enabled': False}}},
                   mode='paper', instance='long')
print('ws disabled ok; feed is', c._feed)
"
```
Expected: prints `ws disabled ok; feed is None`.

- [ ] **Step 3: Confirm core imports with state package present**

Run: `.venv/bin/python -c "import main, execution.engine, market_data.collector, bitfinex, state; print('core imports ok')"`
Expected: `core imports ok`.

- [ ] **Step 4: Commit (if any regression fix was needed; otherwise skip)**

```bash
git add -A && git commit -m "test(ws): full-suite regression green with feed disabled/stubbed"
```

---

## Task 9: Live WS smoke-test gate (requires explicit user go-ahead — real money)

**This task connects the live WS feed and places a real order. Do NOT run it without the user's explicit confirmation and live API keys in the environment.**

- [ ] **Step 1: Confirm go-ahead** — ask the user to confirm a live WS smoke run now, with live keys set.

- [ ] **Step 2: Extend `scripts/live_close_smoke_test.py`** with an optional WS check: construct a live `BitfinexClient` (ws enabled), wait up to ~15s for `client._feed.is_healthy()`, assert a ticker arrives in `MarketState` and a `position_snapshot` populated `AccountState`, then place a minimal order through the client (routes over WS), confirm a real `order.id`, reduce-only close, verify flat. Keep all live actions minimal-size and abort on any leftover position.

- [ ] **Step 3: Run with explicit user confirmation only**

Run: `.venv/bin/python scripts/live_close_smoke_test.py --side buy --size 0.0001 --ws`
Expected: feed connects + authenticates; ticker + position update observed over WS; order placed over WS confirms with a real id; reduce-only close; account flat. Capture the transcript.

- [ ] **Step 4: Mark Phase 3 done** — update the spec `Status:` to `Implemented (Phase 3) — live WS smoke test PASSED <date>` and commit.

---

## Self-review (completed by plan author)

**Spec coverage:**
- §3 components / layout → Tasks 1–4 (state + ws_feed), Task 6 (client). ✔
- §4 MarketState/AccountState API → Tasks 1–2. ✔
- §5 WsFeed (thread/loop, handlers, submit_order_sync/cancel, mapping) → Tasks 3–4. ✔
- §6 client routing (ticker/positions/balance/orders, last_fill, close) → Task 6. ✔
- §7 reconnect reconcile (re-auth → REST reconcile) + backstop interval → Task 4 (`_on_authenticated`/`_reconcile`), config Task 5 (`reconcile_interval_seconds`; the periodic loop task is a follow-up noted below). ✔
- §8 fee enrichment → Task 7. ✔
- §9 threading/safety (locks, daemon, REST fallback) → Tasks 1–4, 6. ✔
- §10 config (`exchange.ws`, kill-switch) → Task 5; kill-switch verified Task 8. ✔
- §11 error handling (OrderRejected vs AckUnparseable; REST fallback) → Tasks 4, 6. ✔
- §12 testing (state, ws_feed incl. fake bfx, client routing, regression, live gate) → Tasks 1–4, 6, 8, 9. ✔
- §13 DoD → Tasks 1–9. ✔

**Deviation noted:** §7's *periodic* backstop reconcile (every `reconcile_interval_seconds`) is wired as config + the re-auth reconcile in Task 4, but the periodic asyncio timer task itself is intentionally deferred to the live-integration step (it needs the running loop and is hard to unit-test without one). The re-auth reconcile — the correctness-critical path — is implemented and tested in Task 4. If the periodic backstop is wanted before the live gate, add a small `loop.call_later`-driven task in `WsFeed._run` that schedules `self._reconcile()` every interval; it is additive and low-risk.

**Placeholder scan:** none — every code step is complete. Task 9 is intentionally a live, user-gated manual step (not code-with-placeholders).

**Type consistency:** `MarketState`/`AccountState` method names match across state, ws_feed, and client (`update_ticker`, `is_fresh`, `apply_position_snapshot`, `apply_position`, `remove_position`, `get_positions`, `apply_wallet`, `get_wallets`, `apply_fill`, `last_fill`, `set_status`, `mark_reconciled`). `WsFeed.submit_order_sync`/`cancel_order_sync`/`is_healthy`/`start`/`stop`/`last_fill`(via account) are used consistently in client Task 6. `_CCY_TO_DISPLAY`/`REDUCE_ONLY` are imported from `bitfinex.rest` (defined there in Phase 2). The order bridge raises `OrderRejected`/`AckUnparseable` (Phase-2 errors the engine already catches).
