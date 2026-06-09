# Exchange-native Catastrophe-floor Stop — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a position opens, place one reduce-only exchange-native STOP resting at the initial stop level, and cancel it when the position closes by any path — so worst-case loss is capped near the stop level even when the bot is down, slow, or price gaps.

**Architecture:** Extend the thin order layer (`bitfinex/`) to submit/parse a margin `STOP` order and list open orders. Add a small `StopOrderManager` (`execution/stop_manager.py`) that owns place/cancel/reconcile and is called by `ExecutionEngine` at the open, close, and startup-reload points. All existing bot-side exit logic (trailing, take-profit, `_check_positions`) is unchanged and remains the primary path + backstop. The `reduce_only` flag is the safety invariant — an orphaned stop can never open a position.

**Tech Stack:** Python 3.11, bfxapi, pytest (+pytest-xdist via repo `pyproject.toml`). Run tests with `.venv/bin/python -m pytest`.

**Reference:** spec `docs/superpowers/specs/2026-06-09-exchange-catastrophe-stop-design.md`; live-verified bfxapi STOP semantics (memory `bitfinex-stop-order-semantics`): `type="STOP"`, trigger in `price`, `flags=1024` reduce-only, ack parses inline with an id, `cancel_order(id)` works, `auth.get_orders()` lists active orders.

---

## File Structure

- `bitfinex/rest.py` — add `stop` → `STOP` type mapping in `submit_order`; add `get_open_orders()`. (REST path)
- `bitfinex/ws_feed.py` — add `stop` → `STOP` mapping in `_send_submit`. (WS path)
- `bitfinex/paper.py` — `stop` orders rest inertly (place/cancel only, no trigger); add `get_open_orders()`.
- `bitfinex/client.py` — add `fetch_open_orders()` (routes to account/auth like `fetch_positions`).
- `execution/stop_manager.py` — **new** `StopOrderManager`: `place` / `cancel` / `reconcile`.
- `execution/engine.py` — construct the manager; call `place` after an entry fill, `cancel` on every close path, `reconcile` after the startup position reload.
- `tests/test_stop_order_layer.py` — **new**, order-layer mapping + open-orders parsing.
- `tests/test_stop_manager.py` — **new**, manager place/cancel/reconcile with a mock client.
- `tests/test_engine_stop_wiring.py` — **new**, engine calls the manager at the right points.

---

## Phase A — Order layer: STOP type + open-orders read

### Task A1: REST `submit_order` maps `stop` → `STOP`

**Files:**
- Modify: `bitfinex/rest.py:34`
- Test: `tests/test_stop_order_layer.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stop_order_layer.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from bitfinex.rest import BfxRest, REDUCE_ONLY


class _FakeOrder:
    def __init__(self, **kw):
        self.id = kw.get("id", 111)
        self.symbol = kw.get("symbol", "tSOLUST")
        self.order_status = kw.get("order_status", "ACTIVE")
        self.amount_orig = kw.get("amount_orig", -1.5)
        self.amount = kw.get("amount", -1.5)
        self.price_avg = kw.get("price_avg", None)


class _FakeNotif:
    def __init__(self, data):
        self.status = "SUCCESS"
        self.text = "ok"
        self.data = data


class _FakeAuth:
    def __init__(self):
        self.calls = []

    def submit_order(self, **kw):
        self.calls.append(kw)
        return _FakeNotif(_FakeOrder())


class _FakeClient:
    def __init__(self):
        self.rest = type("R", (), {"auth": _FakeAuth(), "public": None})()


def test_submit_order_maps_stop_to_STOP_with_trigger_and_reduce_only():
    client = _FakeClient()
    r = BfxRest("k", "s", client=client)
    order = r.submit_order("SOL/USDT", "sell", 1.5, order_type="stop",
                           price=33.14, reduce_only=True)
    call = client.rest.auth.submit_order.__self__.calls[-1]
    assert call["type"] == "STOP"
    assert call["price"] == "33.14"        # trigger goes in price
    assert call["flags"] == REDUCE_ONLY
    assert call["amount"] == "-1.50000000"  # sell -> negative
    assert order.order_type == "stop"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_stop_order_layer.py::test_submit_order_maps_stop_to_STOP_with_trigger_and_reduce_only -q`
Expected: FAIL — `assert 'LIMIT' == 'STOP'` (current code maps any non-market to LIMIT).

- [ ] **Step 3: Write minimal implementation**

In `bitfinex/rest.py`, replace the single mapping line inside `submit_order` (currently line 34):

```python
        bfx_type = {"market": "MARKET", "limit": "LIMIT",
                    "stop": "STOP"}.get(order_type.lower(), "LIMIT")
```

(Leave the rest of `submit_order` unchanged — `price=f"{price or 0}"` already carries the trigger, and `_order_from_bfx(..., order_type, ...)` already echoes `order_type="stop"`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_stop_order_layer.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bitfinex/rest.py tests/test_stop_order_layer.py
git commit -m "feat(order): REST submit_order maps stop -> Bitfinex STOP"
```

---

### Task A2: WS `_send_submit` maps `stop` → `STOP`

**Files:**
- Modify: `bitfinex/ws_feed.py:350`
- Test: `tests/test_stop_order_layer.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_stop_order_layer.py
def test_ws_send_submit_maps_stop_to_STOP(monkeypatch):
    import bitfinex.ws_feed as wf

    captured = {}

    class _Inputs:
        def submit_order(self, **kw):
            captured.update(kw)
            return None  # coroutine stand-in; not awaited in this test

    # Build a bare WsFeed without running its __init__/threads.
    feed = object.__new__(wf.WsFeed)
    feed._bfx = type("B", (), {"wss": type("W", (), {"inputs": _Inputs()})()})()
    feed._loop = None

    # Patch run_coroutine_threadsafe so the coroutine is just discarded.
    monkeypatch.setattr(wf.asyncio, "run_coroutine_threadsafe",
                        lambda coro, loop: None)

    feed._send_submit("SOL/USDT", "sell", 1.5, "stop", 33.14, True, cid=7)
    assert captured["type"] == "STOP"
    assert captured["price"] == "33.14"
    assert captured["flags"] == wf.REDUCE_ONLY
    assert captured["amount"] == "-1.50000000"
    assert captured["cid"] == 7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_stop_order_layer.py::test_ws_send_submit_maps_stop_to_STOP -q`
Expected: FAIL — `captured["type"] == 'LIMIT'`.

- [ ] **Step 3: Write minimal implementation**

In `bitfinex/ws_feed.py`, replace the mapping line in `_send_submit` (currently line 350):

```python
        bfx_type = {"market": "MARKET", "limit": "LIMIT",
                    "stop": "STOP"}.get(order_type.lower(), "LIMIT")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_stop_order_layer.py -q`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add bitfinex/ws_feed.py tests/test_stop_order_layer.py
git commit -m "feat(order): WS _send_submit maps stop -> Bitfinex STOP"
```

---

### Task A3: Paper broker rests `stop` orders inertly + lists them

**Files:**
- Modify: `bitfinex/paper.py` (`__init__`, `submit_order`, `cancel_order`, add `get_open_orders`)
- Test: `tests/test_stop_order_layer.py`

**Rationale:** In paper mode the bot-side `_check_positions` performs exits. A paper stop must therefore NOT fill/close on submit (that would double-close and break the golden P&L tests). It rests inertly: `submit_order(order_type="stop")` records the order and returns it `ACTIVE` without touching wallets/positions; `cancel_order` removes it; `get_open_orders` lists resting stops. This exercises the place/cancel/reconcile code paths with paper parity.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_stop_order_layer.py
from bitfinex.paper import PaperBroker


def test_paper_stop_rests_without_closing_position_and_cancels():
    broker = PaperBroker({"USDT": 1000.0})
    # open a long so there is a position to (not) touch
    broker.submit_order("SOL/USDT", "buy", 1.0, order_type="market", price=60.0)
    assert len(broker.get_positions()) == 1

    stop = broker.submit_order("SOL/USDT", "sell", 1.0, order_type="stop",
                               price=30.0, reduce_only=True)
    assert stop.status == "ACTIVE"
    assert stop.id is not None
    # position is untouched by placing the stop
    assert len(broker.get_positions()) == 1
    # the resting stop is listed
    assert any(o.id == stop.id for o in broker.get_open_orders())

    broker.cancel_order(stop.id)
    assert all(o.id != stop.id for o in broker.get_open_orders())
```

(If `PaperBroker.__init__` takes a different signature, adapt the construction to match the existing paper tests in `tests/test_bfx_paper.py`; the behavior asserted is what matters.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_stop_order_layer.py::test_paper_stop_rests_without_closing_position_and_cancels -q`
Expected: FAIL — currently a `stop` submit hits the `else` branch, fills immediately, and (reduce_only) drops the position; `get_open_orders` does not exist.

- [ ] **Step 3: Write minimal implementation**

In `bitfinex/paper.py`:

1. In `__init__`, add a resting-stop store (near `self._fills = {}`):

```python
        self._open_orders: dict[int, Order] = {}   # id -> resting stop
```

2. At the TOP of `submit_order`, before the wallet/position logic, intercept stops:

```python
        if order_type.lower() == "stop":
            oid = self._next_id
            self._next_id += 1
            signed = abs(amount) if side == "buy" else -abs(amount)
            o = Order(id=oid, symbol=symbol, side=side, order_type="stop",
                      amount=abs(amount), filled=0.0, avg_price=None,
                      status="ACTIVE", reduce_only=reduce_only, fee=0.0,
                      fee_currency=None, raw={"trigger": float(price or 0.0),
                                              "amount_orig": signed})
            self._open_orders[oid] = o
            return o
```

3. Make `cancel_order` remove a resting stop if present:

```python
    def cancel_order(self, order_id: int) -> Order:
        self._open_orders.pop(order_id, None)
        return Order(id=order_id, symbol="", side="buy", order_type="market",
                     amount=0.0, filled=0.0, avg_price=None, status="CANCELED",
                     reduce_only=False, fee=0.0, fee_currency=None, raw=None)
```

4. Add `get_open_orders` (place it next to `get_positions`):

```python
    def get_open_orders(self) -> List[Order]:
        return list(self._open_orders.values())
```

(`List` is already imported in `bitfinex/paper.py`; if not, add `from typing import List`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_stop_order_layer.py tests/test_bfx_paper.py tests/test_paper_parity_golden.py -q`
Expected: PASS (new test passes; paper golden tests still green — stops never reach the fill path).

- [ ] **Step 5: Commit**

```bash
git add bitfinex/paper.py tests/test_stop_order_layer.py
git commit -m "feat(paper): rest stop orders inertly + get_open_orders"
```

---

### Task A4: `get_open_orders()` on REST wrapper + `fetch_open_orders()` on client

**Files:**
- Modify: `bitfinex/rest.py` (add `get_open_orders`), `bitfinex/client.py` (add `fetch_open_orders`)
- Test: `tests/test_stop_order_layer.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_stop_order_layer.py
def test_rest_get_open_orders_parses_active_orders():
    class _ActiveOrder:
        id = 222
        symbol = "tSOLUST"
        amount_orig = -1.5
        price = 30.0
        flags = REDUCE_ONLY
        order_type = "STOP"
        order_status = "ACTIVE"

    class _Auth:
        def get_orders(self):
            return [_ActiveOrder()]

    class _Client:
        rest = type("R", (), {"auth": _Auth(), "public": None})()

    r = BfxRest("k", "s", client=_Client())
    orders = r.get_open_orders()
    assert len(orders) == 1
    o = orders[0]
    assert o.id == 222
    assert o.symbol == "SOL/USDT"     # display form
    assert o.side == "sell"            # negative amount_orig
    assert o.reduce_only is True
    assert o.order_type == "stop"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_stop_order_layer.py::test_rest_get_open_orders_parses_active_orders -q`
Expected: FAIL — `AttributeError: 'BfxRest' object has no attribute 'get_open_orders'`.

- [ ] **Step 3: Write minimal implementation**

In `bitfinex/rest.py`, add to `BfxRest` (after `cancel_order`):

```python
    def get_open_orders(self) -> List[Order]:
        out = []
        for o in self._client.rest.auth.get_orders():
            amt = float(o.amount_orig or 0.0)
            out.append(Order(
                id=o.id, symbol=symbols.to_display(o.symbol),
                side="buy" if amt >= 0 else "sell",
                order_type=(o.order_type or "").lower().replace(" ", "_") or "stop",
                amount=abs(amt), filled=0.0,
                avg_price=(getattr(o, "price", None) or None),
                status=(o.order_status or "").upper(),
                reduce_only=bool((getattr(o, "flags", 0) or 0) & REDUCE_ONLY),
                fee=0.0, fee_currency=None, raw=o))
        return out
```

In `bitfinex/client.py`, add (after `cancel_order`):

```python
    def fetch_open_orders(self) -> List[Order]:
        return self._auth.get_open_orders()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_stop_order_layer.py -q`
Expected: PASS (all order-layer tests)

- [ ] **Step 5: Commit**

```bash
git add bitfinex/rest.py bitfinex/client.py tests/test_stop_order_layer.py
git commit -m "feat(order): get_open_orders / fetch_open_orders for stop reconcile"
```

---

## Phase B — Stop lifecycle manager + engine wiring

### Task B1: `StopOrderManager.place` and `.cancel`

**Files:**
- Create: `execution/stop_manager.py`
- Test: `tests/test_stop_manager.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stop_manager.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.stop_manager import StopOrderManager


class _Order:
    def __init__(self, oid, symbol="SOL/USDT", side="sell", reduce_only=True):
        self.id = oid
        self.symbol = symbol
        self.side = side
        self.reduce_only = reduce_only


class _Client:
    def __init__(self):
        self.created = []
        self.cancelled = []
        self._next = 500

    def create_order(self, symbol, side, amount, *, order_type, price,
                     reduce_only):
        self.created.append(dict(symbol=symbol, side=side, amount=amount,
                                 order_type=order_type, price=price,
                                 reduce_only=reduce_only))
        self._next += 1
        return _Order(self._next, symbol=symbol, side=side,
                      reduce_only=reduce_only)

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)

    def fetch_open_orders(self):
        return []


def test_place_for_long_submits_reduce_only_sell_stop():
    client = _Client()
    mgr = StopOrderManager(client)
    oid = mgr.place("SOL/USDT", "buy", 1.5, 60.0)
    assert oid == 501
    c = client.created[-1]
    assert c["side"] == "sell"            # opposite of a long
    assert c["order_type"] == "stop"
    assert c["price"] == 60.0
    assert c["reduce_only"] is True
    assert c["amount"] == 1.5


def test_place_for_short_submits_buy_stop():
    client = _Client()
    mgr = StopOrderManager(client)
    mgr.place("ETH/USDT", "sell", 0.1, 1800.0)
    assert client.created[-1]["side"] == "buy"


def test_cancel_cancels_tracked_id_and_forgets_it():
    client = _Client()
    mgr = StopOrderManager(client)
    mgr.place("SOL/USDT", "buy", 1.5, 60.0)
    mgr.cancel("SOL/USDT")
    assert client.cancelled == [501]
    # second cancel is a no-op (id already forgotten)
    mgr.cancel("SOL/USDT")
    assert client.cancelled == [501]


def test_place_swallows_client_error_and_returns_none():
    class _Boom(_Client):
        def create_order(self, *a, **k):
            raise RuntimeError("rejected")
    mgr = StopOrderManager(_Boom())
    assert mgr.place("SOL/USDT", "buy", 1.5, 60.0) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_stop_manager.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'execution.stop_manager'`.

- [ ] **Step 3: Write minimal implementation**

```python
# execution/stop_manager.py
"""Owns the reduce-only exchange-native catastrophe stop for each open
position: place on entry, cancel on close, reconcile on restart.

The reduce_only flag is the safety invariant — a stop with no position to
reduce is rejected by the exchange, so an orphaned stop can never open a new
position. All methods are best-effort: they log and swallow client errors so a
stop-management failure never breaks the trade loop (the bot-side stop check
remains as backstop).
"""

import logging
from typing import Optional


class StopOrderManager:
    def __init__(self, client, log: Optional[logging.Logger] = None):
        self._client = client
        self._log = log or logging.getLogger(__name__)
        self._ids: dict[str, int] = {}   # symbol -> resting stop order id

    @staticmethod
    def _stop_side(position_side: str) -> str:
        return "sell" if position_side.lower() == "buy" else "buy"

    def place(self, symbol: str, position_side: str, amount: float,
              stop_price: float) -> Optional[int]:
        try:
            order = self._client.create_order(
                symbol, self._stop_side(position_side), abs(amount),
                order_type="stop", price=stop_price, reduce_only=True)
            oid = getattr(order, "id", None)
            if oid is not None:
                self._ids[symbol] = oid
                self._log.info("[%s] catastrophe stop placed id=%s @ %.6f",
                               symbol, oid, stop_price)
            return oid
        except Exception as exc:
            self._log.error("[%s] failed to place catastrophe stop: %s",
                            symbol, exc)
            return None

    def cancel(self, symbol: str) -> None:
        oid = self._ids.pop(symbol, None)
        if oid is None:
            return
        try:
            self._client.cancel_order(oid)
            self._log.info("[%s] catastrophe stop cancelled id=%s", symbol, oid)
        except Exception as exc:
            self._log.error("[%s] failed to cancel catastrophe stop id=%s: %s",
                            symbol, oid, exc)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_stop_manager.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add execution/stop_manager.py tests/test_stop_manager.py
git commit -m "feat(stop): StopOrderManager place/cancel"
```

---

### Task B2: `StopOrderManager.reconcile`

**Files:**
- Modify: `execution/stop_manager.py`
- Test: `tests/test_stop_manager.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_stop_manager.py
def test_reconcile_adopts_existing_places_missing_cancels_orphans():
    client = _Client()
    # Exchange already has a reduce-only stop for SOL (adopt), an orphan stop
    # for XRP (no position -> cancel), and none for ETH (place).
    client.fetch_open_orders = lambda: [
        _Order(901, symbol="SOL/USDT", side="sell", reduce_only=True),
        _Order(902, symbol="XRP/USDT", side="sell", reduce_only=True),
    ]
    mgr = StopOrderManager(client)
    positions = [
        {"symbol": "SOL/USDT", "side": "buy", "amount": 1.5, "stop_loss": 60.0},
        {"symbol": "ETH/USDT", "side": "sell", "amount": 0.1, "stop_loss": 1800.0},
    ]
    mgr.reconcile(positions)

    # adopted SOL id, placed ETH (new id), cancelled the XRP orphan
    assert mgr._ids["SOL/USDT"] == 901
    assert "ETH/USDT" in mgr._ids
    assert 902 in client.cancelled
    # only ETH required a new create_order
    assert [c["symbol"] for c in client.created] == ["ETH/USDT"]


def test_reconcile_skips_position_without_stop_loss():
    client = _Client()
    client.fetch_open_orders = lambda: []
    mgr = StopOrderManager(client)
    mgr.reconcile([{"symbol": "SOL/USDT", "side": "buy", "amount": 1.5}])
    assert client.created == []   # no stop_loss -> cannot place, skip
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_stop_manager.py::test_reconcile_adopts_existing_places_missing_cancels_orphans -q`
Expected: FAIL — `AttributeError: 'StopOrderManager' object has no attribute 'reconcile'`.

- [ ] **Step 3: Write minimal implementation**

Add to `StopOrderManager`:

```python
    def reconcile(self, positions) -> None:
        """On restart: adopt an existing reduce-only stop per position, place
        one where missing, and cancel orphan reduce-only stops with no matching
        open position."""
        try:
            open_orders = self._client.fetch_open_orders()
        except Exception as exc:
            self._log.error("stop reconcile: fetch_open_orders failed: %s", exc)
            return

        by_symbol: dict[str, int] = {}
        for o in open_orders:
            if getattr(o, "reduce_only", False):
                by_symbol.setdefault(o.symbol, o.id)

        held = {p.get("symbol") for p in positions}

        # adopt or place per held position
        for p in positions:
            symbol = p.get("symbol")
            if symbol in by_symbol:
                self._ids[symbol] = by_symbol[symbol]
                self._log.info("[%s] adopted existing catastrophe stop id=%s",
                               symbol, by_symbol[symbol])
                continue
            stop_loss = p.get("stop_loss")
            if stop_loss is None:
                self._log.warning("[%s] no stop_loss on reload — cannot place "
                                  "catastrophe stop", symbol)
                continue
            self.place(symbol, p.get("side", "buy"),
                       float(p.get("amount", 0.0)), float(stop_loss))

        # cancel orphan reduce-only stops (defensive; reduce_only already safe)
        for symbol, oid in by_symbol.items():
            if symbol not in held:
                try:
                    self._client.cancel_order(oid)
                    self._log.info("[%s] cancelled orphan catastrophe stop id=%s",
                                   symbol, oid)
                except Exception as exc:
                    self._log.error("[%s] failed to cancel orphan stop id=%s: %s",
                                    symbol, oid, exc)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_stop_manager.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add execution/stop_manager.py tests/test_stop_manager.py
git commit -m "feat(stop): StopOrderManager reconcile (adopt/place/orphan-cancel)"
```

---

### Task B3: Wire the manager into `ExecutionEngine`

**Files:**
- Modify: `execution/engine.py` — construct manager in `__init__`; `place` in `_persist_open` after the entry fill; `cancel` in `close_position` (after a successful close) and in `close_all_positions`.
- Test: `tests/test_engine_stop_wiring.py` (create)

**Notes:**
- `_persist_open` already computes `sl_price` (around `bitfinex/...`/`execution/engine.py:461-484`) and calls `self._risk_state.set(...)`. Place the stop right after that `set`, using the filled amount and the position side.
- `close_position` ends by clearing tracking on success; cancel the stop on the success path.
- The manager is constructed for ALL modes — in paper it drives the inert paper stop (Task A3), keeping parity.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_engine_stop_wiring.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.engine import ExecutionEngine


def _engine(tmp_path):
    config = {"data": {"trades_file": str(tmp_path / "t.csv"),
                       "db_file": str(tmp_path / "t.db")},
              "exchange": {"rate_limit": 0.0},
              "risk": {"stop_loss_pct": 3.0, "take_profit_pct": 6.0}}
    return ExecutionEngine(config, mode="paper", trade_direction="both",
                           instance="long")


def test_engine_has_stop_manager(tmp_path):
    eng = _engine(tmp_path)
    assert eng._stop_mgr is not None


def test_entry_places_stop_and_close_cancels_it(tmp_path):
    eng = _engine(tmp_path)
    calls = {"place": [], "cancel": []}
    eng._stop_mgr.place = lambda *a, **k: calls["place"].append((a, k))
    eng._stop_mgr.cancel = lambda symbol: calls["cancel"].append(symbol)

    eng.execute_order(symbol="SOL/USDT", side="buy", amount=1.0, price=60.0,
                      stop_loss_pct=3.0, take_profit_pct=6.0)
    assert len(calls["place"]) == 1            # stop placed on entry
    assert calls["place"][0][0][0] == "SOL/USDT"

    eng.close_position("SOL/USDT", reason="test")
    assert calls["cancel"] == ["SOL/USDT"]     # stop cancelled on close
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_engine_stop_wiring.py -q`
Expected: FAIL — `AttributeError: 'ExecutionEngine' object has no attribute '_stop_mgr'`.

- [ ] **Step 3: Write minimal implementation**

1. In `execution/engine.py`, add the import near the top (with the other `from ...` imports, ~line 32):

```python
from execution.stop_manager import StopOrderManager
```

2. In `ExecutionEngine.__init__`, after `self._client` is constructed (it is built around line 105 via `from bitfinex import BitfinexClient`), add:

```python
        self._stop_mgr = StopOrderManager(self._client, self._log)
```

3. In `_persist_open`, immediately after the `self._risk_state.set(... stop_loss=sl_price ...)` call (around line 482-492), add:

```python
        self._stop_mgr.place(symbol, side, filled, sl_price)
```

(Use the same `symbol`, `side`, `filled` amount, and `sl_price` already in scope in `_persist_open`.)

4. In `close_position`, on the SUCCESS path — right before the function returns the success dict and after tracking is cleared — add:

```python
        self._stop_mgr.cancel(symbol)
```

5. In `close_all_positions` (around line 373), inside the loop over positions, after each close, add:

```python
            self._stop_mgr.cancel(pos.get("symbol"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_engine_stop_wiring.py -q`
Expected: PASS

- [ ] **Step 5: Run the full engine + paper suite (no regressions)**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (all; golden/paper parity unaffected because paper stops are inert)

- [ ] **Step 6: Commit**

```bash
git add execution/engine.py tests/test_engine_stop_wiring.py
git commit -m "feat(engine): place catastrophe stop on entry, cancel on close"
```

---

### Task B4: Reconcile stops after the startup position reload

**Files:**
- Modify: `execution/engine.py` — call `self._stop_mgr.reconcile(self.open_positions)` right after the "Position sync complete: N position(s) loaded" log.
- Test: `tests/test_engine_stop_wiring.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_engine_stop_wiring.py
def test_sync_positions_triggers_stop_reconcile(tmp_path):
    eng = _engine(tmp_path)
    seen = {}
    eng._stop_mgr.reconcile = lambda positions: seen.setdefault("called", positions)
    # Call the same position-sync entrypoint the startup path uses.
    eng.sync_positions()
    assert "called" in seen
```

(If the engine's startup reload method is named differently, point the test and the implementation at that method — grep `Position sync complete` in `execution/engine.py` to find it.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_engine_stop_wiring.py::test_sync_positions_triggers_stop_reconcile -q`
Expected: FAIL — reconcile is never called (no `seen`).

- [ ] **Step 3: Write minimal implementation**

In `execution/engine.py`, locate the method that logs `Position sync complete: N position(s) loaded` (grep for that string). At the end of that method add:

```python
        self._stop_mgr.reconcile(self.open_positions)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_engine_stop_wiring.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add execution/engine.py tests/test_engine_stop_wiring.py
git commit -m "feat(engine): reconcile catastrophe stops after position sync"
```

---

## Phase C — Live verification + deploy

### Task C1: Gated live smoke test (bot paused)

**Goal:** Confirm the integrated path places a real reduce-only STOP for each live position and reconcile adopts it on restart — on the real account, without nonce competition.

- [ ] **Step 1: Stop the live bot (SIGKILL preserves positions)**

```bash
cd /mnt/hermes-data/.hermes/hermes-agent/trading-bot
PID=$(pgrep -f "main.py --mode live --instance default" | head -1); echo "PID=$PID"
kill -9 "$PID"; sleep 3
kill -0 "$PID" 2>/dev/null && echo "STILL ALIVE — abort" || echo "stopped ✓"
```

- [ ] **Step 2: Start the bot on the new code and watch reconcile place stops**

```bash
LOG="instances/live536/logs/restart_$(date +%F)_stops.out"
setsid nohup .venv/bin/python main.py --mode live --instance default \
  --config config/live_536.yaml > "$LOG" 2>&1 < /dev/null &
sleep 45
grep -iE "position sync|catastrophe stop|nonce| ERROR " "$LOG" | grep -vi divergence | tail -20
```

Expected: "Position sync complete: 3 position(s) loaded", then three "catastrophe stop placed" lines (ETH/BTC/SOL), `nonce` count 0.

- [ ] **Step 3: Confirm the three reduce-only stops rest on the exchange**

Confirm via the running bot's own logs (do NOT spawn a second authenticated client while the bot holds the key — nonce risk). Look for the three "catastrophe stop placed id=… @ …" lines and verify each trigger price ≈ `entry × (1 ∓ stop_loss_pct)` for the position side.

- [ ] **Step 4: Restart once more; confirm reconcile ADOPTS (does not duplicate)**

```bash
PID=$(pgrep -f "main.py --mode live --instance default" | head -1)
kill -9 "$PID"; sleep 3
LOG2="instances/live536/logs/restart_$(date +%F)_stops2.out"
setsid nohup .venv/bin/python main.py --mode live --instance default \
  --config config/live_536.yaml > "$LOG2" 2>&1 < /dev/null &
sleep 45
grep -iE "adopted existing catastrophe stop|catastrophe stop placed|nonce" "$LOG2" | tail -20
```

Expected: three "adopted existing catastrophe stop" lines, NO new "placed" lines, `nonce` 0. (Proves no duplication.)

- [ ] **Step 5: Record the result in memory**

Update memory `bitfinex-stop-order-semantics` / the spec with the live placement + adopt confirmation and the three resting order ids.

---

### Task C2: Final verification + close-out

- [ ] **Step 1: Full suite green**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (all)

- [ ] **Step 2: Confirm bot stable and stops resting**

```bash
ps -o pid,etime -p "$(pgrep -f 'main.py --mode live --instance default' | head -1)"
tail -5 instances/live536/logs/bot.log
```

Expected: one live process, recent cycle lines, no errors.

- [ ] **Step 3: Update the spec status to Deployed and commit any doc updates**

```bash
git add docs/superpowers/specs/2026-06-09-exchange-catastrophe-stop-design.md
git commit -m "docs(spec): catastrophe stop deployed + live-verified"
```

---

## Self-Review notes
- **Spec coverage:** order-layer STOP (A1/A2/A3), open-orders read (A4), place-on-entry (B3), cancel-on-close (B3), reconcile-on-restart (B2/B4), reduce_only invariant (B1), paper parity (A3), live test (C1) — all covered.
- **Out of scope (per spec):** exchange trailing, exchange TP/OCO, cadence changes — none added.
- **Backstop preserved:** `_check_positions` is untouched; the manager is additive and best-effort (errors swallowed), so a stop-management failure cannot break the trade loop.
