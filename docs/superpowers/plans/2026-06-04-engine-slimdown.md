# Engine Slim-Down — Phase 4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Slim `execution/engine.py` (1218 lines) into a mode-agnostic execution core: it always goes through `BitfinexClient`, the ~390-line paper simulation moves into an enriched `PaperBroker`, bot-owned SL/TP metadata moves into a thread-safe `state/risk_state.py` `RiskState`, the per-cycle position-cache hacks collapse onto the client's push state, the margin-short fallback becomes a guarded `RiskState` union, and the dead code is removed — preserving the engine's outward contract and the Phase 1–3 wiring.

**Architecture:** The engine builds `BitfinexClient` in both modes (paper wraps the enriched `PaperBroker`, live wraps bfxapi), so order/close/positions/balance all flow through one client interface with no `mode == "paper"` branches. PnL is computed in the engine for both modes from the position entry price and the close order's `avg_price`. SL/TP lives in `RiskState`; positions come from the client unioned with RiskState-known symbols (guarded by `exchange.trust_exchange_positions`).

**Tech Stack:** Python 3.11, the existing `bitfinex/` typed client + `state/` package, `pytest`. Guardrails: paper-parity golden (characterization) tests, a read-only live position check, and a config escape-hatch.

**Source of truth:** spec `docs/superpowers/specs/2026-06-04-engine-slimdown-design.md`.

---

## Verified current code (read before starting)

- Constants at `execution/engine.py:48-50`: `TAKER_FEE = 0.001`, `MAKER_FEE = 0.0`, `SLIPPAGE = 0.0005`. `DEFAULT_QUOTE` is `"USDT"` (used in `_init_paper_balance`).
- Paper sim (to be replaced): `_init_paper_balance` (`:450`), `_paper_execute_order` (`:483-672`), `_create_paper_position` (`:674-737`), `_update_paper_balance_on_close` (`:739-763`). Balance model is **spot-style** per currency: `{currency: {"free", "used", "total"}}`. Buy: `quote.free/total -= cost+fee`, `base.free/total += amount`. Sell: `base.free/total -= amount`, `quote.free/total += cost-fee`. Market fill price = `price*(1±SLIPPAGE)` (buy up/sell down). Fee = `fill_price*amount*fee_rate`.
- Engine paper `execute_order` returns a dict `{success, order_id, filled_price, amount, fee, side}` (and on a netting close adds `pnl`/`position_closed`); `close_position` paper branch returns `{success, pnl, price, error}`. **These outward shapes must be preserved.**
- `get_balance` (`engine.py:428`): returns the available quote balance (read it to confirm exact return shape before Task 3).
- Current minimal `PaperBroker` (`bitfinex/paper.py`): no slippage/fee/wallet tracking; `submit_order` returns `Order(...avg_price=price, fee=0.0)`, `get_wallets` returns one fixed wallet.
- The engine constructs the client only in live mode (`if mode == "live"`); paper has `self._client = None`. The collector builds its own client for public data — unaffected.
- The bot's real flow: **open** via `execute_order` (direction filter blocks opposing opens), **close** via `close_position` (reduce-only) on SL/TP in `main._check_positions`. So `PaperBroker` only needs **open + reduce-only close** semantics (matching live); the old order-time *netting* is a paper-only artifact and is intentionally dropped — the golden tests (Task 1) pin the real scenarios.

## File structure

- **New:** `state/risk_state.py`; `scripts/check_live_positions.py`; tests `tests/test_paper_parity_golden.py`, `tests/test_bfx_paper_enriched.py`, `tests/test_risk_state.py`, `tests/test_engine_mode_agnostic.py`.
- **Modified:** `bitfinex/paper.py` (enrich), `bitfinex/client.py` (pass symbols/quote to PaperBroker), `execution/engine.py` (mode-agnostic; RiskState; remove cache + dead code), `main.py` (RiskState merge; drop `_update_position_cache` call), `config/default.yaml`+`long`+`short` (`trust_exchange_positions`), `monitoring/telegram_alerts.py` + `scripts/reconcile_readonly.py` + `scripts/reconcile_deep.py` (snake_case readers).

---

# PHASE 4a — Paper unification (mode-agnostic engine)

## Task 1: Paper-parity golden (characterization) tests

Capture the CURRENT engine's paper behavior as fixtures, so the refactor can be proven byte-for-byte. These tests run against today's engine and must pass before any refactor; they must still pass after.

**Files:**
- Test: `tests/test_paper_parity_golden.py`

- [ ] **Step 1: Write the characterization test**

Create `tests/test_paper_parity_golden.py`:
```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.engine import ExecutionEngine


def _engine(tmp_path, capital=1000.0):
    config = {
        "trading": {"initial_capital": capital,
                    "symbols": [{"name": "BTC/USDT", "enabled": True}]},
        "data": {"trades_file": str(tmp_path / "t.csv"),
                 "db_file": str(tmp_path / "t.db")},
        "risk": {"trailing_stop": False},
        "exchange": {"rate_limit": 0.0},
    }
    return ExecutionEngine(config, mode="paper", trade_direction="both",
                           instance="long")


def test_golden_open_long(tmp_path):
    eng = _engine(tmp_path)
    r = eng.execute_order("BTC/USDT", "buy", 0.01, 100.0, "market",
                          stop_loss_pct=2.0, take_profit_pct=4.0)
    # fill = 100 * (1+0.0005) = 100.05 ; fee = 100.05*0.01*0.001 = 0.00100050
    assert r["success"] is True
    assert r["filled_price"] == round(100.0 * 1.0005, 2)        # 100.05
    assert abs(r["fee"] - (100.0 * 1.0005 * 0.01 * 0.001)) < 1e-9
    assert r["side"] == "buy"
    pos = [p for p in eng.open_positions if p["symbol"] == "BTC/USDT"]
    assert len(pos) == 1 and abs(pos[0]["amount"] - 0.01) < 1e-12
    assert pos[0]["entry_price"] == round(100.0 * 1.0005, 2) or \
        abs(pos[0]["entry_price"] - 100.0 * 1.0005) < 1e-9


def test_golden_close_long_pnl(tmp_path):
    eng = _engine(tmp_path)
    eng.execute_order("BTC/USDT", "buy", 0.01, 100.0, "market",
                      stop_loss_pct=2.0, take_profit_pct=4.0)
    # Close at a higher price: paper close uses _current_price; force via stub.
    eng._current_price = lambda symbol, fallback: 110.0
    res = eng.close_position("BTC/USDT", reason="take_profit")
    assert res["success"] is True
    # entry ~100.05, close 110.0, amount 0.01 -> pnl ~ (110-100.05)*0.01 = 0.0995
    assert abs(res["pnl"] - (110.0 - 100.0 * 1.0005) * 0.01) < 1e-6


def test_golden_balance_after_open(tmp_path):
    eng = _engine(tmp_path, capital=1000.0)
    eng.execute_order("BTC/USDT", "buy", 0.01, 100.0, "market",
                      stop_loss_pct=None, take_profit_pct=None)
    bal = eng.get_balance()           # available quote balance
    # spent cost+fee = 100.05*0.01 + 0.0010005 = 1.0005 + 0.0010005
    spent = 100.0 * 1.0005 * 0.01 + 100.0 * 1.0005 * 0.01 * 0.001
    # get_balance returns the USDT available figure; confirm it dropped by ~spent
    assert isinstance(bal, (int, float, dict))
```

> NOTE: `get_balance`'s exact return type must be confirmed by reading `engine.py:428`. If it returns a dict, assert on the `USDT` available entry instead of `isinstance`. Make the final assertion concrete against the real shape before committing.

- [ ] **Step 2: Run against the CURRENT engine**

Run: `.venv/bin/python -m pytest tests/test_paper_parity_golden.py -v`
Expected: PASS (these characterize today's behavior). If a test fails, your assertion doesn't match current behavior — fix the ASSERTION to match what the engine actually returns (this is characterization, not aspiration). Tighten the `get_balance` assertion to the real shape.

- [ ] **Step 3: Commit**

```bash
git add tests/test_paper_parity_golden.py
git commit -m "test(engine): paper-parity golden characterization tests (pre-refactor baseline)"
```

---

## Task 2: Enrich `PaperBroker`

Move slippage/fee/wallet/position logic into `PaperBroker` so the client is the single paper simulator. Open + reduce-only-close semantics (matching live); engine computes PnL.

**Files:**
- Modify: `bitfinex/paper.py`
- Test: `tests/test_bfx_paper_enriched.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_bfx_paper_enriched.py`:
```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bitfinex.paper import PaperBroker, SLIPPAGE, TAKER_FEE


def _broker():
    return PaperBroker(initial_capital=1000.0, quote="USDT",
                       base_symbols=["BTC/USDT"])


def test_market_buy_slippage_and_fee():
    b = _broker()
    o = b.submit_order("BTC/USDT", "buy", 0.01, order_type="market", price=100.0)
    assert o.avg_price == round(100.0 * (1 + SLIPPAGE), 8)
    assert abs(o.fee - (100.0 * (1 + SLIPPAGE) * 0.01 * TAKER_FEE)) < 1e-12
    assert o.is_filled
    pos = b.get_positions()
    assert len(pos) == 1 and pos[0].symbol == "BTC/USDT" and pos[0].amount == 0.01


def test_buy_decrements_quote_increments_base():
    b = _broker()
    b.submit_order("BTC/USDT", "buy", 0.01, order_type="market", price=100.0)
    w = {x.currency: x for x in b.get_wallets()}
    fill = 100.0 * (1 + SLIPPAGE)
    spent = fill * 0.01 + fill * 0.01 * TAKER_FEE
    assert abs(w["USDT"].available - (1000.0 - spent)) < 1e-9
    assert abs(w["BTC"].available - 0.01) < 1e-12


def test_reduce_only_closes_position():
    b = _broker()
    b.submit_order("BTC/USDT", "buy", 0.01, order_type="market", price=100.0)
    o = b.submit_order("BTC/USDT", "sell", 0.01, order_type="market",
                       price=110.0, reduce_only=True)
    assert o.is_filled and o.reduce_only
    assert b.get_positions() == []


def test_market_sell_slippage_down():
    b = _broker()
    # seed base so the sell-to-open passes the balance check
    b._wallets["BTC"]["available"] = 1.0
    b._wallets["BTC"]["balance"] = 1.0
    o = b.submit_order("BTC/USDT", "sell", 0.01, order_type="market", price=100.0)
    assert o.avg_price == round(100.0 * (1 - SLIPPAGE), 8)


def test_insufficient_quote_rejected():
    import pytest
    from bitfinex.errors import OrderRejected
    b = PaperBroker(initial_capital=0.5, quote="USDT", base_symbols=["BTC/USDT"])
    with pytest.raises(OrderRejected):
        b.submit_order("BTC/USDT", "buy", 0.01, order_type="market", price=100.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bfx_paper_enriched.py -v`
Expected: FAIL — `PaperBroker` has no slippage/fee/wallet tracking and no `base_symbols` arg.

- [ ] **Step 3: Implement the enriched PaperBroker**

Replace the entire contents of `bitfinex/paper.py`:
```python
"""Paper-mode broker: simulates auth methods with slippage/fee/wallet tracking,
returning the SAME typed models as the live path so the engine is mode-agnostic.
PnL is computed by the engine (from position entry + the close order avg_price);
this broker only produces realistic fills and tracks balances/positions.
Open + reduce-only-close semantics, matching the live flow.
"""

from typing import List, Optional

from bitfinex.models import Order, Position, Wallet, Fill
from bitfinex.errors import OrderRejected

# Mirror execution/engine.py's paper constants.
TAKER_FEE = 0.001    # 0.1%
MAKER_FEE = 0.0      # 0.0%
SLIPPAGE = 0.0005    # 0.05% on market fills


class PaperBroker:
    def __init__(self, initial_capital: float = 1000.0, quote: str = "USDT",
                 base_symbols: Optional[List[str]] = None):
        self._quote = quote
        # spot-style wallets: currency -> {"balance", "available"}
        self._wallets = {quote: {"balance": float(initial_capital),
                                 "available": float(initial_capital)}}
        for sym in (base_symbols or []):
            base = sym.split("/")[0]
            self._wallets.setdefault(base, {"balance": 0.0, "available": 0.0})
        self._positions: dict[str, Position] = {}   # symbol -> Position
        self._fills: dict[str, Fill] = {}
        self._next_id = 1

    # ── orders ───────────────────────────────────────────────────────────
    def submit_order(self, symbol: str, side: str, amount: float, *,
                     order_type: str = "market", price: Optional[float] = None,
                     reduce_only: bool = False) -> Order:
        base, quote = symbol.split("/")
        ref = float(price or 0.0)
        if order_type == "market":
            fill_price = ref * (1 + SLIPPAGE) if side == "buy" else ref * (1 - SLIPPAGE)
        else:
            fill_price = ref
        fill_price = round(fill_price, 8)
        fee_rate = TAKER_FEE if order_type == "market" else MAKER_FEE
        fee = fill_price * amount * fee_rate
        cost = fill_price * amount
        oid = self._next_id
        self._next_id += 1

        self._wallets.setdefault(base, {"balance": 0.0, "available": 0.0})
        self._wallets.setdefault(quote, {"balance": 0.0, "available": 0.0})

        if reduce_only:
            # Close/reduce: opposite-side wallet move, drop the position.
            pos = self._positions.get(symbol)
            if pos is not None and pos.side == "long":
                self._wallets[base]["available"] -= amount
                self._wallets[base]["balance"] -= amount
                self._wallets[quote]["available"] += cost - fee
                self._wallets[quote]["balance"] += cost - fee
            elif pos is not None and pos.side == "short":
                self._wallets[quote]["available"] += cost - fee
                self._wallets[quote]["balance"] += cost - fee
                self._wallets[base]["available"] += amount
                self._wallets[base]["balance"] += amount
            self._positions.pop(symbol, None)
        else:
            # Open: balance check, wallet move, create position.
            if side == "buy":
                need = cost + fee
                if need > self._wallets[quote]["available"] + 1e-12:
                    raise OrderRejected(
                        f"Insufficient {quote}: need {need:.2f}, "
                        f"have {self._wallets[quote]['available']:.2f}")
                self._wallets[quote]["available"] -= need
                self._wallets[quote]["balance"] -= need
                self._wallets[base]["available"] += amount
                self._wallets[base]["balance"] += amount
            else:  # sell to open (spot-style, mirrors current paper sim)
                if amount > self._wallets[base]["available"] + 1e-12:
                    raise OrderRejected(
                        f"Insufficient {base}: need {amount:.6f}, "
                        f"have {self._wallets[base]['available']:.6f}")
                self._wallets[base]["available"] -= amount
                self._wallets[base]["balance"] -= amount
                self._wallets[quote]["available"] += cost - fee
                self._wallets[quote]["balance"] += cost - fee
            self._positions[symbol] = Position(
                symbol=symbol, side="long" if side == "buy" else "short",
                amount=abs(amount) if side == "buy" else -abs(amount),
                entry_price=fill_price, unrealized_pnl=0.0, leverage=1.0,
                raw_symbol=symbol)

        self._fills[symbol] = Fill(symbol=symbol, side=side, amount=abs(amount),
                                   price=fill_price, fee=fee, fee_currency=quote,
                                   order_id=oid, trade_id=oid, ts="")
        return Order(id=oid, symbol=symbol, side=side, order_type=order_type,
                     amount=abs(amount), filled=abs(amount), avg_price=fill_price,
                     status="EXECUTED", reduce_only=reduce_only, fee=fee,
                     fee_currency=quote, raw=None)

    def cancel_order(self, order_id: int) -> Order:
        return Order(id=order_id, symbol="", side="buy", order_type="market",
                     amount=0.0, filled=0.0, avg_price=None, status="CANCELED",
                     reduce_only=False, fee=0.0, fee_currency=None, raw=None)

    # ── reads ────────────────────────────────────────────────────────────
    def get_positions(self) -> List[Position]:
        return list(self._positions.values())

    def get_wallets(self) -> List[Wallet]:
        return [Wallet(currency=c, wallet_type="margin",
                       balance=w["balance"], available=w["available"])
                for c, w in self._wallets.items()]

    def get_trades(self, symbol: Optional[str] = None, since=None,
                   limit=None) -> List[Fill]:
        if symbol:
            f = self._fills.get(symbol)
            return [f] if f else []
        return list(self._fills.values())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_bfx_paper_enriched.py tests/test_bfx_paper.py -v`
Expected: PASS. (`test_bfx_paper.py` from Phase 2 may assert the old fixed-wallet behavior — update those assertions to the enriched wallet/`base_symbols` shape if they fail; the enriched broker is a superset.)

- [ ] **Step 5: Update `BitfinexClient` to pass symbols to the paper broker**

In `bitfinex/client.py`, the paper branch builds `PaperBroker(initial_capital=capital)`. Change it to pass the configured symbols + quote:
```python
            capital = float(config.get("trading", {}).get("initial_capital", 1000.0))
            names = [s.get("name") for s in
                     config.get("trading", {}).get("symbols", []) if s.get("name")]
            self._auth = PaperBroker(initial_capital=capital, base_symbols=names)
            self._ticker_source = _PublicTicker()
```
Run `.venv/bin/python -m pytest tests/test_bfx_client.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add bitfinex/paper.py bitfinex/client.py tests/test_bfx_paper_enriched.py tests/test_bfx_paper.py
git commit -m "feat(paper): enrich PaperBroker with slippage/fee/wallet tracking"
```

---

## Task 3: Make the engine mode-agnostic (delete the paper sim)

**Files:**
- Modify: `execution/engine.py`
- Test: `tests/test_engine_mode_agnostic.py`, plus the golden tests (Task 1) must still pass

- [ ] **Step 1: Write the failing test**

Create `tests/test_engine_mode_agnostic.py`:
```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.engine import ExecutionEngine


def _engine(tmp_path):
    config = {
        "trading": {"initial_capital": 1000.0,
                    "symbols": [{"name": "BTC/USDT", "enabled": True}]},
        "data": {"db_file": str(tmp_path / "t.db")},
        "risk": {"trailing_stop": False},
        "exchange": {},
    }
    return ExecutionEngine(config, mode="paper", trade_direction="both",
                           instance="long")


def test_paper_engine_has_client():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        eng = _engine(Path(d))
        assert eng._client is not None          # client built in BOTH modes now
        # No paper-sim attributes remain:
        assert not hasattr(eng, "_paper_balance")
        assert not hasattr(eng, "_paper_execute_order")


def test_open_then_position_via_client(tmp_path):
    eng = _engine(tmp_path)
    r = eng.execute_order("BTC/USDT", "buy", 0.01, 100.0, "market",
                          stop_loss_pct=2.0, take_profit_pct=4.0)
    assert r["success"] is True
    pos = [p for p in eng.open_positions if p["symbol"] == "BTC/USDT"]
    assert len(pos) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_engine_mode_agnostic.py -v`
Expected: FAIL — paper mode still has `_client is None` and `_paper_balance`.

- [ ] **Step 3: Refactor the engine to mode-agnostic**

In `execution/engine.py`:

(a) `__init__`: build the client in BOTH modes. Replace the `self._client = None; if mode == "live": ...` block with:
```python
        from bitfinex import BitfinexClient
        self._client = BitfinexClient(config, mode=mode, instance=self.instance)
```
Delete `self._init_paper_balance()` and the `self._paper_balance` attribute. Keep `self._open_positions`/`_live_position_meta` for now (Phase 4b removes them).

(b) `execute_order`: delete the `if self.mode == "paper": result = self._paper_execute_order(...)` branch and the paper/live split. The body becomes the single client path (this is essentially today's `_live_execute_order` logic, already typed): record the entry order, call `self._client.create_order(...)`, map the typed `Order` into the outward dict (`success`, `order_id`, `filled_price`, `amount`, `fee`, `side`), append to `self._open_positions`, set SL/TP on the position, and (Phase 4b) `RiskState`. Reuse the EXACT mapping `_live_execute_order` already does; just drop the mode check so it runs for paper too. The outward dict keys must match the golden fixtures.

(c) `close_position`: delete the paper branch (`if self.mode == "paper": ... _update_paper_balance_on_close ...`). The single path: fetch the position, submit `self._client.create_order(..., reduce_only=True)`, compute PnL from `entry_price` + `order.avg_price` (fallback `_current_price`), `_persist_close` (with the Phase-3 fee enrichment), `_record_trade`, remove from `_open_positions`/clear meta. Reuse the existing live-branch logic; drop the mode check.

(d) DELETE these methods entirely: `_paper_execute_order`, `_create_paper_position`, `_update_paper_balance_on_close`, `_init_paper_balance`. Remove the `SLIPPAGE`/`TAKER_FEE`/`MAKER_FEE`/`DEFAULT_QUOTE` module constants if now unused (grep first).

(e) `get_balance`: replace the paper-vs-live split with `wallets = self._client.fetch_balance(); ...` returning the same shape it returns today (read the current method first; map `list[Wallet]` → that shape, e.g. the `USDT` available figure).

(f) `open_positions` property: paper previously returned `self._open_positions`. Now both modes can read from the client too, but the engine still appends to `_open_positions` on open (until Phase 4b). For Task 3 keep `open_positions` returning the engine's tracked list (so existing readers see the SL/TP-bearing dicts); Phase 4b switches it to the client+RiskState union. (Minimal change in 4a: just make sure paper open still populates `_open_positions` with the SL/TP-bearing dict, as `_live_execute_order`'s position-append already does.)

> The guiding rule: after this task there is ONE `execute_order` and ONE `close_position` with no `mode == "paper"` branch, both going through `self._client`. The golden tests (Task 1) are the parity gate.

- [ ] **Step 4: Run the golden + new tests**

Run: `.venv/bin/python -m pytest tests/test_paper_parity_golden.py tests/test_engine_mode_agnostic.py tests/test_close_path.py tests/test_direction_filter.py -v`
Expected: PASS. **The golden tests passing proves paper behavior is preserved.** If a golden test fails, the enriched `PaperBroker` or the engine mapping diverged from the baseline — fix until parity holds (do NOT change the golden fixtures).

- [ ] **Step 5: Run persistence regression + commit**

Run: `.venv/bin/python -m pytest tests/test_persistence.py tests/test_engine_persistence.py tests/test_main_persistence.py -v` → PASS.

```bash
git add execution/engine.py tests/test_engine_mode_agnostic.py
git commit -m "refactor(engine): mode-agnostic order/close via client; delete paper sim"
```

---

# PHASE 4b — RiskState + position consolidation

## Task 4: RiskState (`state/risk_state.py`)

**Files:**
- Create: `state/risk_state.py`
- Test: `tests/test_risk_state.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_risk_state.py`:
```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from state.risk_state import RiskState


def test_set_get_clear():
    rs = RiskState()
    assert rs.get("BTC/USDT") is None
    rs.set("BTC/USDT", stop_loss=98.0, take_profit=104.0, trailing_stop=True,
           trailing_activation=2.0, trailing_distance=0.5, entry_price=100.0,
           side="buy")
    m = rs.get("BTC/USDT")
    assert m["stop_loss"] == 98.0 and m["take_profit"] == 104.0
    assert m["highest_price"] == 100.0 and m["lowest_price"] == float("inf")
    assert rs.symbols() == ["BTC/USDT"]
    rs.clear("BTC/USDT")
    assert rs.get("BTC/USDT") is None and rs.symbols() == []


def test_update_trailing():
    rs = RiskState()
    rs.set("BTC/USDT", stop_loss=98.0, take_profit=104.0, trailing_stop=True,
           trailing_activation=2.0, trailing_distance=0.5, entry_price=100.0,
           side="buy")
    rs.update_trailing("BTC/USDT", stop_loss=101.0, highest_price=103.0)
    m = rs.get("BTC/USDT")
    assert m["stop_loss"] == 101.0 and m["highest_price"] == 103.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_risk_state.py -v`
Expected: FAIL — no module `state.risk_state`.

- [ ] **Step 3: Implement**

Create `state/risk_state.py`:
```python
"""Per-symbol, bot-owned SL/TP + trailing metadata (NOT exchange state).

The engine sets this on entry and clears it on close; main._check_positions
merges it onto exchange positions for the SL/TP threshold check. Thread-safe.
"""

import threading
from typing import List, Optional


class RiskState:
    def __init__(self):
        self._lock = threading.Lock()
        self._meta: dict[str, dict] = {}

    def set(self, symbol: str, *, stop_loss: float, take_profit: float,
            trailing_stop: bool, trailing_activation: float,
            trailing_distance: float, entry_price: float, side: str) -> None:
        with self._lock:
            self._meta[symbol] = {
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "trailing_stop": trailing_stop,
                "trailing_activation": trailing_activation,
                "trailing_distance": trailing_distance,
                "highest_price": entry_price if side == "buy" else 0.0,
                "lowest_price": entry_price if side == "sell" else float("inf"),
            }

    def get(self, symbol: str) -> Optional[dict]:
        with self._lock:
            m = self._meta.get(symbol)
            return dict(m) if m is not None else None

    def update_trailing(self, symbol: str, *, stop_loss: Optional[float] = None,
                        highest_price: Optional[float] = None,
                        lowest_price: Optional[float] = None) -> None:
        with self._lock:
            m = self._meta.get(symbol)
            if m is None:
                return
            if stop_loss is not None:
                m["stop_loss"] = stop_loss
            if highest_price is not None:
                m["highest_price"] = highest_price
            if lowest_price is not None:
                m["lowest_price"] = lowest_price

    def clear(self, symbol: str) -> None:
        with self._lock:
            self._meta.pop(symbol, None)

    def symbols(self) -> List[str]:
        with self._lock:
            return list(self._meta.keys())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_risk_state.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add state/risk_state.py tests/test_risk_state.py
git commit -m "feat(state): RiskState for bot-owned SL/TP metadata"
```

---

## Task 5: Wire RiskState + collapse the position cache

**Files:**
- Modify: `execution/engine.py`, `main.py`, `config/default.yaml`+`long`+`short`
- Test: `tests/test_engine_positions_union.py`

- [ ] **Step 1: Add config**

In each `config/*.yaml` `exchange:` block, add: `trust_exchange_positions: false`.

- [ ] **Step 2: Write the failing test**

Create `tests/test_engine_positions_union.py`:
```python
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.engine import ExecutionEngine
from bitfinex.models import Position


def _engine(tmp_path, trust=False):
    config = {"trading": {"initial_capital": 1000.0,
                          "symbols": [{"name": "BTC/USDT", "enabled": True}]},
              "data": {"db_file": str(tmp_path / "t.db")},
              "risk": {}, "exchange": {"trust_exchange_positions": trust}}
    return ExecutionEngine(config, mode="paper", trade_direction="both",
                           instance="long")


def test_union_surfaces_riskstate_known_position(tmp_path):
    eng = _engine(tmp_path, trust=False)
    # Client reports NO positions; RiskState knows BTC/USDT (bot opened a short).
    eng._client.fetch_positions = lambda: []
    eng._risk_state.set("BTC/USDT", stop_loss=0, take_profit=0,
                        trailing_stop=False, trailing_activation=0,
                        trailing_distance=0, entry_price=100.0, side="sell")
    eng._risk_entry["BTC/USDT"] = {"symbol": "BTC/USDT", "side": "sell",
                                   "amount": 0.01, "entry_price": 100.0}
    syms = [p["symbol"] for p in eng.open_positions]
    assert "BTC/USDT" in syms          # surfaced via the union safety net


def test_trust_true_skips_union(tmp_path):
    eng = _engine(tmp_path, trust=True)
    eng._client.fetch_positions = lambda: []
    eng._risk_state.set("BTC/USDT", stop_loss=0, take_profit=0,
                        trailing_stop=False, trailing_activation=0,
                        trailing_distance=0, entry_price=100.0, side="sell")
    eng._risk_entry["BTC/USDT"] = {"symbol": "BTC/USDT", "side": "sell",
                                   "amount": 0.01, "entry_price": 100.0}
    assert eng.open_positions == []    # trust=true => client only
```

- [ ] **Step 3: Implement**

In `execution/engine.py`:
- In `__init__`: `from state.risk_state import RiskState; self._risk_state = RiskState()`. Add `self._risk_entry: dict[str, dict] = {}` (the bot's record of what it opened: symbol → {symbol, side, amount, entry_price} — the union's data source). Read `self._trust_positions = bool(config.get("exchange", {}).get("trust_exchange_positions", False))`.
- On `execute_order` success: `self._risk_state.set(symbol, stop_loss=..., take_profit=..., trailing_stop=..., trailing_activation=..., trailing_distance=..., entry_price=fill_price, side=side)` and `self._risk_entry[symbol] = {"symbol": symbol, "side": side, "amount": amount, "entry_price": fill_price}`. (Derive SL/TP prices from the `stop_loss_pct`/`take_profit_pct` args exactly as `_create_paper_position` did.)
- On `close_position` success: `self._risk_state.clear(symbol); self._risk_entry.pop(symbol, None)`.
- Replace the `open_positions` property body:
```python
    @property
    def open_positions(self):
        out = []
        seen = set()
        for p in self._client.fetch_positions():       # typed Position
            meta = self._risk_state.get(p.symbol) or {}
            out.append({
                "symbol": p.symbol, "side": "buy" if p.side == "long" else "sell",
                "amount": p.abs_amount, "entry_price": p.entry_price,
                "unrealized_pnl": p.unrealized_pnl, **meta,
            })
            seen.add(p.symbol)
        if not self._trust_positions:
            for sym, e in self._risk_entry.items():
                if sym not in seen:                     # client didn't report it
                    meta = self._risk_state.get(sym) or {}
                    out.append({**e, **meta})
        return out
```
- DELETE: `_cached_live_positions`, `_update_position_cache`, `_merge_live_positions_with_meta`, `_live_position_meta`, `_get_live_positions`, `_open_positions` (and references). `sync_positions_at_startup` now seeds `self._risk_state`/`self._risk_entry` from `self._client.fetch_positions()` instead of `_open_positions`/`_live_position_meta` (port its loop to write RiskState).

In `main.py`:
- Remove the per-cycle `self.executor._update_position_cache()` call (around `:677`).
- `_check_positions`: it already reads `self.executor.open_positions` (now the union, with SL/TP merged in). Where it previously relied on the position dict's `stop_loss`/`take_profit` keys, those are now present via the RiskState merge. Where a trailing update mutated the position dict, call `self.executor._risk_state.update_trailing(symbol, stop_loss=..., highest_price=..., lowest_price=...)` instead. (Read `_check_positions` and the risk-manager `update_position` to wire the trailing write-back.)

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_engine_positions_union.py tests/test_paper_parity_golden.py tests/test_close_path.py tests/test_direction_filter.py tests/test_persistence.py tests/test_engine_persistence.py tests/test_main_persistence.py -v`
Expected: PASS (golden parity still holds; union behaves per the trust flag).

- [ ] **Step 5: Verify main imports + commit**

Run: `.venv/bin/python -c "import main, execution.engine; print('ok')"`
```bash
git add execution/engine.py main.py config/default.yaml config/long.yaml config/short.yaml tests/test_engine_positions_union.py
git commit -m "refactor(engine): RiskState SL/TP + client-union positions; drop position cache"
```

---

## Task 6: Read-only live position check script

**Files:**
- Create: `scripts/check_live_positions.py`

- [ ] **Step 1: Implement (no test — it's an operational read-only script)**

Create `scripts/check_live_positions.py`:
```python
#!/usr/bin/env python3
"""READ-ONLY live check: does bfxapi report our (margin/short) positions?

Places NO orders. Use this to decide whether to flip
`exchange.trust_exchange_positions` to true (dropping the RiskState union).

    python scripts/check_live_positions.py --config config/long.yaml
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from bitfinex import BitfinexClient


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/long.yaml")
    args = p.parse_args()
    cfg = yaml.safe_load(open(args.config))
    # Force REST-only read (no WS) for a clean snapshot.
    cfg.setdefault("exchange", {}).setdefault("ws", {})["enabled"] = False
    client = BitfinexClient(cfg, mode="live", instance="check")
    positions = client.fetch_positions()        # READ-ONLY
    print(f"fetch_positions() returned {len(positions)} position(s):")
    for pos in positions:
        print(f"  {pos.symbol}  side={pos.side}  amount={pos.amount}  "
              f"entry={pos.entry_price}")
    longs = [p for p in positions if p.side == "long"]
    shorts = [p for p in positions if p.side == "short"]
    print(f"\nlongs={len(longs)} shorts={len(shorts)}")
    print("If your open SHORTS appear above, it is safe to set "
          "exchange.trust_exchange_positions: true.")
    client.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-check it imports (do NOT run live without keys/go-ahead)**

Run: `.venv/bin/python -c "import scripts.check_live_positions; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add scripts/check_live_positions.py
git commit -m "feat(ops): read-only live position check (margin-short visibility)"
```

---

# PHASE 4c — Dead-code cleanup + reader migration

## Task 7: Migrate camelCase readers, drop aliases

**Files:**
- Modify: `monitoring/telegram_alerts.py`, `scripts/reconcile_readonly.py`, `scripts/reconcile_deep.py`

- [ ] **Step 1: Find every camelCase position-key reader**

Run: `grep -rn "entryPrice\|unrealizedPnl\|\\bcontracts\\b" --include=*.py . | grep -v tests/`
Report the hits. For each, change the read to the snake_case key the union now provides (`entry_price`, `unrealized_pnl`, `amount`). The `open_positions` dicts now expose only snake_case (`symbol`, `side`, `amount`, `entry_price`, `unrealized_pnl`, plus RiskState SL/TP keys).

- [ ] **Step 2: Apply the edits**

In each reader file, replace `pos.get("entryPrice", ...)` → `pos.get("entry_price", ...)`, `pos.get("unrealizedPnl", ...)` → `pos.get("unrealized_pnl", ...)`, and `pos.get("contracts", ...)` → `pos.get("amount", ...)`. (`telegram_alerts.py` `_cmd_portfolio`/position displays; `reconcile_*` scripts.)

- [ ] **Step 3: Verify imports + commit**

Run: `.venv/bin/python -c "import monitoring.telegram_alerts; print('ok')"` and `.venv/bin/python -c "import importlib; importlib.import_module('scripts.reconcile_readonly')" 2>/dev/null; echo done`
```bash
git add monitoring/telegram_alerts.py scripts/reconcile_readonly.py scripts/reconcile_deep.py
git commit -m "refactor: migrate position readers to snake_case keys"
```

---

## Task 8: Remove dead code; simplify `_current_price`

**Files:**
- Modify: `execution/engine.py`

- [ ] **Step 1: Remove `_trades_csv` / rate-limit / unused getters**

In `execution/engine.py`:
- Delete `self._trades_csv` and the `trades_path` block; compute `db_path = self.data_config.get("db_file") or str(Path(self.data_config.get("data_dir", "data")) / "trading.db")`. (Confirm no remaining `self._trades_csv` references with grep.)
- Delete `_enforce_rate_limit` and `self._last_api_call`/`self._rate_limit`; remove all `self._enforce_rate_limit()` calls (the client owns throttling).
- Delete `get_open_positions()` (unused wrapper — grep to confirm no caller; main uses `open_positions` property).
- Delete `_normalize_symbol`; replace its sole remaining caller (if any) with `from bitfinex import symbols as bfx_symbols; bfx_symbols.to_display(x)`.
- Remove orphaned imports left by the Task-3 paper-sim deletion (grep to confirm unused): `import math`, `defaultdict` (from `collections import deque, defaultdict` → keep `deque`), and `from decimal import Decimal, ROUND_DOWN, ROUND_UP`.
- Remove the now-unreachable `if self._client is None:` guard in the unified `_live_execute_order` (the client is always built) and any other always-true `if self._client:` sentinels. Optionally rename `_live_execute_order` → `_execute_order_via_client` and fix the stale "for live positions" comment (cosmetic; only if cheap).

- [ ] **Step 2: Simplify `_current_price`**

Replace `_current_price` body with the thin client delegation:
```python
    def _current_price(self, symbol: str, fallback: float) -> float:
        try:
            t = self._client.fetch_ticker(symbol)   # client already does WS/REST
            if t.last:
                return float(t.last)
            if t.bid and t.ask:
                return (t.bid + t.ask) / 2.0
        except Exception as exc:
            self._log.warning("price fetch failed for %s: %s", symbol, exc)
        return float(fallback)
```

- [ ] **Step 3: Run tests**

Run: `.venv/bin/python -m pytest tests/test_close_path.py tests/test_direction_filter.py tests/test_paper_parity_golden.py tests/test_engine_mode_agnostic.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add execution/engine.py
git commit -m "refactor(engine): remove dead code (trades_csv, rate-limit, normalize_symbol)"
```

---

## Task 9: Full regression + line-count + spec status

**Files:**
- Modify: `docs/superpowers/specs/2026-06-04-engine-slimdown-design.md`

- [ ] **Step 1: Full suite**

Run: `.venv/bin/python -m pytest tests/ -q 2>&1 | tail -12`
Expected: all pass except the 2 pre-existing unrelated `tests/test_nonce_atomic.py`. Investigate any other failure (do not weaken tests).

- [ ] **Step 2: Confirm the slim-down + no mode branch**

Run:
```bash
wc -l execution/engine.py
grep -n "mode == \"paper\"\|mode == 'paper'" execution/engine.py
.venv/bin/python -c "import main, execution.engine, bitfinex, state; print('core imports ok')"
```
Expected: engine well under ~800 lines; NO `mode == "paper"` branch in the order/close paths (any remaining match must be benign, e.g. a log line — report it); core imports ok.

- [ ] **Step 3: Update spec status + commit**

In `docs/superpowers/specs/2026-06-04-engine-slimdown-design.md`, set `Status:` to `Implemented (Phase 4) — see docs/superpowers/plans/2026-06-04-engine-slimdown.md (pending live gates)`.

```bash
git add docs/superpowers/specs/2026-06-04-engine-slimdown-design.md
git commit -m "docs(engine): mark Phase 4 implemented"
```

---

## Self-review (completed by plan author)

**Spec coverage:** §3 components → Tasks 2/4/3. §4 mode-agnostic engine → Task 3. §5 enriched PaperBroker → Task 2. §6 RiskState → Task 4. §7 position consolidation + margin-short union + trust flag → Task 5. §8 guardrails (golden Task 1, read-only check Task 6, escape-hatch Task 5) → ✔. §9 dead code → Tasks 7/8. §10 main.py → Task 5. §12 testing → Tasks 1–9. §14 DoD → Task 9. ✔

**Deviation (noted):** the spec said PaperBroker "ports the current netting"; the plan intentionally implements **open + reduce-only-close** semantics (matching live) instead of the paper-only order-time netting, because the bot's real flow never nets in `execute_order` (it opens via `execute_order`, closes via `close_position`). The golden tests (Task 1) pin the real scenarios, so paper *results* for the bot's actual usage are preserved; the unused netting artifact is dropped. This is a simplification consistent with the mode-agnostic goal.

**Placeholder scan:** the two spots that say "read the current method first / confirm the exact shape" (`get_balance` return shape in Tasks 1/3; `_check_positions` trailing write-back in Task 5) are explicit "inspect then map" steps with the mapping target specified — not open-ended placeholders. The intricate engine `execute_order`/`close_position` refactor (Task 3) instructs reusing the EXISTing typed `_live_execute_order`/live-`close_position` logic verbatim minus the mode branch, gated by the golden tests.

**Type consistency:** `PaperBroker(initial_capital, quote, base_symbols)`, `RiskState.set/get/update_trailing/clear/symbols`, the `open_positions` union dict keys (`symbol/side/amount/entry_price/unrealized_pnl` + RiskState SL/TP keys), and `trust_exchange_positions` are used consistently across Tasks 2–8.
