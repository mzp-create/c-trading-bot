# WS Auto-Reconnect Supervisor + Single Auth WS Per Key — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Bitfinex WS feed self-heal from terminal disconnects (429 / keepalive / reconnection-timeout) via a sequential reconnect supervisor, and remove the collector's redundant authenticated WS feed so only one auth WS runs per API key.

**Architecture:** Two changes. (1) `BitfinexClient` gains `enable_ws` so `MarketDataCollector` can build a live REST client without an auth WS feed. (2) `WsFeed` replaces its one-shot `_main` with `_supervise`: a `while not stopping` loop that builds a fresh bfxapi client, runs it until it disconnects/raises, then (unless stopping) backs off and reconnects. Reconnection is strictly sequential — a new client is built only after the previous one is closed — so it can never run two WS connections on the shared key at once.

**Tech Stack:** Python 3.11, bfxapi v4 (asyncio WS), pytest. Tests drive the async supervisor via `asyncio.run(...)` with injected fake client-factory / sleep / clock — no pytest-asyncio, no network, no real delays.

**Run tests from the repo root** `/mnt/hermes-data/.hermes/hermes-agent/trading-bot` with `.venv/bin/python -m pytest`.

---

## File Structure

- `bitfinex/client.py` — add `enable_ws: bool = True` param; gate `_start_ws` on it. (modify)
- `market_data/collector.py` — pass `enable_ws=False` when building its client. (modify)
- `bitfinex/ws_feed.py` — constructor tunables/seams; `_is_ratelimit`, `_backoff`, `_build_client`, `_close_client`, `_supervise`; rewire `_run`; `stop()` sets `_stopping`; delete old `_main`. (modify)
- `tests/test_client_enable_ws.py` — BitfinexClient honors `enable_ws`. (create)
- `tests/test_collector_no_ws.py` — collector builds client with `enable_ws=False`. (create)
- `tests/test_ws_feed_reconnect.py` — supervisor reconnect/backoff/429-floor/healthy-reset/stop. (create)

---

## Task 1: `BitfinexClient.enable_ws` gate

**Files:**
- Modify: `bitfinex/client.py` (`__init__`, the live `ws_cfg` block)
- Test: `tests/test_client_enable_ws.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_client_enable_ws.py`:

```python
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import bitfinex.client as client_mod
import bitfinex.rest as rest_mod
import bitfinex.keyguard as keyguard_mod


def _patch_heavy_deps(monkeypatch):
    # Never construct a real bfxapi client or touch the real key registry.
    monkeypatch.setattr(rest_mod, "BfxRest",
                        lambda **k: types.SimpleNamespace(get_ticker=lambda s: None))
    monkeypatch.setattr(keyguard_mod, "register_key", lambda *a, **k: None)
    monkeypatch.setattr(keyguard_mod, "release_key", lambda *a, **k: None)


def _cfg():
    return {"exchange": {"testnet": False, "api_key": "k", "api_secret": "s",
                         "ws": {"enabled": True}}}


def test_live_client_skips_ws_when_enable_ws_false(monkeypatch):
    _patch_heavy_deps(monkeypatch)
    calls = {"start_ws": 0}
    monkeypatch.setattr(client_mod.BitfinexClient, "_start_ws",
                        lambda self, *a, **k: calls.__setitem__("start_ws", calls["start_ws"] + 1))
    client_mod.BitfinexClient(_cfg(), mode="live", instance="default", enable_ws=False)
    assert calls["start_ws"] == 0


def test_live_client_starts_ws_by_default(monkeypatch):
    _patch_heavy_deps(monkeypatch)
    calls = {"start_ws": 0}
    monkeypatch.setattr(client_mod.BitfinexClient, "_start_ws",
                        lambda self, *a, **k: calls.__setitem__("start_ws", calls["start_ws"] + 1))
    client_mod.BitfinexClient(_cfg(), mode="live", instance="default")
    assert calls["start_ws"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_client_enable_ws.py -v`
Expected: `test_live_client_skips_ws_when_enable_ws_false` FAILS (TypeError: unexpected keyword 'enable_ws', or start_ws called once).

- [ ] **Step 3: Implement the gate**

In `bitfinex/client.py`, change the `__init__` signature:

```python
    def __init__(self, config: dict, mode: str = "paper",
                 instance: str = "default", enable_ws: bool = True):
```

Then in the live branch, change the WS-start condition from:

```python
            ws_cfg = ex.get("ws", {})
            if ws_cfg.get("enabled", True):
                self._start_ws(config, api_key, api_secret, ws_cfg)
                atexit.register(self.close)
```

to:

```python
            ws_cfg = ex.get("ws", {})
            if enable_ws and ws_cfg.get("enabled", True):
                self._start_ws(config, api_key, api_secret, ws_cfg)
                atexit.register(self.close)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_client_enable_ws.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add bitfinex/client.py tests/test_client_enable_ws.py
git commit -m "feat(client): add enable_ws flag to skip the auth WS feed

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Collector builds its client without WS

**Files:**
- Modify: `market_data/collector.py:51-53`
- Test: `tests/test_collector_no_ws.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_collector_no_ws.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import market_data.collector as collector_mod


def test_collector_builds_client_without_ws(monkeypatch, tmp_path):
    captured = {}

    class _SpyClient:
        def __init__(self, config, mode=None, instance=None, enable_ws=True):
            captured["mode"] = mode
            captured["enable_ws"] = enable_ws

    monkeypatch.setattr(collector_mod, "BitfinexClient", _SpyClient)
    cfg = {"exchange": {"testnet": False}, "data": {"ohlcv_dir": str(tmp_path)}}
    collector_mod.MarketDataCollector(cfg, mode="live")

    assert captured["mode"] == "live"
    assert captured["enable_ws"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_collector_no_ws.py -v`
Expected: FAIL — `captured["enable_ws"]` is `True` (default), collector does not pass it yet.

- [ ] **Step 3: Pass enable_ws=False**

In `market_data/collector.py`, change:

```python
        self._client: BitfinexClient = BitfinexClient(
            config, mode=mode, instance=config.get("instance", "default")
        )
```

to:

```python
        # The collector only needs public candles (OhlcvFetcher) and REST ticker,
        # so it must NOT open an authenticated WS feed: a second auth WS on the
        # shared API key is the nonce-collision surface (see 2026-06-07 incident).
        self._client: BitfinexClient = BitfinexClient(
            config, mode=mode, instance=config.get("instance", "default"),
            enable_ws=False,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_collector_no_ws.py tests/test_collector_cache.py -v`
Expected: new test PASSES; existing collector-cache tests still PASS.

- [ ] **Step 5: Commit**

```bash
git add market_data/collector.py tests/test_collector_no_ws.py
git commit -m "feat(collector): build live client with enable_ws=False (one auth WS per key)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `WsFeed` constructor tunables + pure backoff helpers

**Files:**
- Modify: `bitfinex/ws_feed.py` (`__init__`, add `_is_ratelimit`, `_backoff`)
- Test: `tests/test_ws_feed_reconnect.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_ws_feed_reconnect.py`:

```python
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bitfinex.ws_feed import WsFeed
from state.market_state import MarketState
from state.account_state import AccountState


def _mk(**kw):
    rest = types.SimpleNamespace(get_positions=lambda: [], get_wallets=lambda: [])
    return WsFeed("k", "s", ["BTC/USDT"], MarketState(), AccountState(), rest, **kw)


def test_is_ratelimit_detects_429():
    feed = _mk()
    assert feed._is_ratelimit(Exception("server rejected WebSocket connection: HTTP 429"))
    assert feed._is_ratelimit(types.SimpleNamespace(status_code=429))
    assert not feed._is_ratelimit(Exception("1006 abnormal closure"))
    assert not feed._is_ratelimit(None)


def test_backoff_no_jitter_returns_delay():
    feed = _mk(reconnect_jitter=0.0)
    assert feed._backoff(5.0, None) == 5.0


def test_backoff_applies_ratelimit_floor():
    feed = _mk(reconnect_jitter=0.0, ratelimit_floor=60.0)
    assert feed._backoff(5.0, Exception("HTTP 429")) == 60.0


def test_backoff_jitter_within_bounds():
    feed = _mk(reconnect_jitter=0.3)
    for _ in range(50):
        v = feed._backoff(10.0, None)
        assert 7.0 <= v <= 13.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ws_feed_reconnect.py -v`
Expected: FAIL — `__init__` rejects `reconnect_jitter` kwarg / `_is_ratelimit` missing.

- [ ] **Step 3: Extend the constructor and add helpers**

In `bitfinex/ws_feed.py`, add `import random` near the top imports (after `import itertools`):

```python
import itertools
import logging
import random
import threading
```

Replace the `__init__` method with (adds keyword-only tunables and seams; keeps every existing param and default):

```python
    def __init__(self, api_key: str, api_secret: str, symbols_list: List[str],
                 market_state, account_state, rest, *,
                 ticker_staleness: float = 15.0,
                 order_confirm_timeout: float = 10.0,
                 reconcile_interval: float = 300.0,
                 wss_host: Optional[str] = None, bfx=None,
                 reconnect_min: float = 5.0, reconnect_max: float = 300.0,
                 reconnect_factor: float = 1.7, reconnect_jitter: float = 0.3,
                 ratelimit_floor: float = 60.0, healthy_reset_after: float = 120.0,
                 bfx_factory=None, sleep=None, clock=None):
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
        self._injected_bfx = bfx                # honored on the first build only
        self._used_injected = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._cid = itertools.count(1)
        self._pending: dict[int, tuple] = {}    # cid -> (Event, holder)
        self._pending_lock = threading.Lock()
        # reconnect supervisor config + test seams
        self._reconnect_min = reconnect_min
        self._reconnect_max = reconnect_max
        self._reconnect_factor = reconnect_factor
        self._reconnect_jitter = reconnect_jitter
        self._ratelimit_floor = ratelimit_floor
        self._healthy_reset_after = healthy_reset_after
        self._bfx_factory = bfx_factory
        self._sleep = sleep or asyncio.sleep
        import time as _time
        self._clock = clock or _time.monotonic
        self._stopping = False
```

Add these two helper methods (place them just above `# ── health ──`):

```python
    # ── reconnect helpers ────────────────────────────────────────────────
    @staticmethod
    def _is_ratelimit(exc) -> bool:
        if exc is None:
            return False
        return getattr(exc, "status_code", None) == 429 or "429" in str(exc)

    def _backoff(self, delay: float, terminal) -> float:
        out = delay
        if self._reconnect_jitter:
            out = out * (1.0 + self._reconnect_jitter * (2 * random.random() - 1))
        if self._is_ratelimit(terminal):
            out = max(out, self._ratelimit_floor)
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_ws_feed_reconnect.py tests/test_ws_feed.py tests/test_ws_feed_orders.py -v`
Expected: the four new helper tests PASS; existing ws_feed tests still PASS.

- [ ] **Step 5: Commit**

```bash
git add bitfinex/ws_feed.py tests/test_ws_feed_reconnect.py
git commit -m "feat(ws): WsFeed reconnect tunables + backoff/ratelimit helpers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `WsFeed` reconnect supervisor loop

**Files:**
- Modify: `bitfinex/ws_feed.py` (`_run`, replace `_main` with `_supervise`, add `_build_client`/`_close_client`, `stop()`)
- Test: `tests/test_ws_feed_reconnect.py` (append)

- [ ] **Step 1: Append the failing supervisor tests**

Append to `tests/test_ws_feed_reconnect.py`:

```python
import asyncio

import pytest


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class _FakeWss:
    def __init__(self, behavior, clock=None, advance=0.0):
        self._behavior = behavior
        self._clock = clock
        self._advance = advance
        self.closed = False

    def on(self, name, fn=None):
        if fn is None:
            def deco(f):
                return f
            return deco
        return fn

    async def subscribe(self, *a, **k):
        pass

    async def start(self):
        if self._advance and self._clock is not None:
            self._clock.t += self._advance
        b = self._behavior
        if isinstance(b, BaseException):
            raise b
        return

    async def close(self):
        self.closed = True


class _FakeClient:
    def __init__(self, wss):
        self.wss = wss


def _factory(behaviors, clock=None, advances=None):
    advances = advances or [0.0] * len(behaviors)
    built = []

    def make():
        i = len(built)
        b = behaviors[i] if i < len(behaviors) else behaviors[-1]
        a = advances[i] if i < len(advances) else 0.0
        c = _FakeClient(_FakeWss(b, clock=clock, advance=a))
        built.append(c)
        return c

    make.built = built
    return make


def _run_supervisor(feed, stop_after):
    delays = []

    async def fake_sleep(d):
        delays.append(d)
        if len(delays) >= stop_after:
            feed._stopping = True

    feed._sleep = fake_sleep
    asyncio.run(feed._supervise())
    return delays


def test_reconnects_after_terminal_error():
    fac = _factory([Exception("boom")] * 4)
    feed = _mk(bfx_factory=fac, reconnect_jitter=0.0,
               reconnect_min=5.0, reconnect_factor=1.7, reconnect_max=300.0)
    _run_supervisor(feed, stop_after=3)
    assert len(fac.built) == 3  # rebuilt a fresh client each cycle


def test_backoff_sequence_grows():
    fac = _factory([Exception("boom")] * 4)
    feed = _mk(bfx_factory=fac, reconnect_jitter=0.0,
               reconnect_min=5.0, reconnect_factor=1.7, reconnect_max=300.0)
    delays = _run_supervisor(feed, stop_after=3)
    assert delays == pytest.approx([5.0, 8.5, 14.45])


def test_429_uses_ratelimit_floor():
    fac = _factory([Exception("server rejected: HTTP 429")])
    feed = _mk(bfx_factory=fac, reconnect_jitter=0.0,
               reconnect_min=5.0, ratelimit_floor=60.0)
    delays = _run_supervisor(feed, stop_after=1)
    assert delays[0] == 60.0


def test_healthy_connection_resets_backoff():
    clock = _Clock()
    fac = _factory([Exception("boom")] * 4, clock=clock,
                   advances=[0.0, 0.0, 200.0, 0.0])
    feed = _mk(bfx_factory=fac, reconnect_jitter=0.0, clock=clock,
               reconnect_min=5.0, reconnect_factor=1.7, healthy_reset_after=120.0)
    delays = _run_supervisor(feed, stop_after=4)
    assert delays == pytest.approx([5.0, 8.5, 5.0, 8.5])


def test_stop_halts_reconnect():
    fac = _factory([Exception("boom")] * 4)
    feed = _mk(bfx_factory=fac, reconnect_jitter=0.0)
    _run_supervisor(feed, stop_after=1)
    assert len(fac.built) == 1  # stopped; no second build


def test_marks_account_down_between_connections():
    fac = _factory([Exception("boom")] * 2)
    feed = _mk(bfx_factory=fac, reconnect_jitter=0.0)
    feed._account.set_status(connected=True, authenticated=True)
    _run_supervisor(feed, stop_after=1)
    assert feed._account.connected is False
    assert feed._account.authenticated is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_ws_feed_reconnect.py -v`
Expected: the six supervisor tests FAIL (`_supervise` / `_build_client` not defined).

- [ ] **Step 3: Implement the supervisor**

In `bitfinex/ws_feed.py`, replace the current `_run` and `_main` methods:

```python
    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._supervise())
        except Exception as exc:                # never crash the process
            log.error("WS supervisor crashed: %s", exc)
        finally:
            self._mark_down()
            loop.close()

    def _build_client(self):
        if self._bfx_factory is not None:
            return self._bfx_factory()
        if self._injected_bfx is not None and not self._used_injected:
            self._used_injected = True
            return self._injected_bfx
        from bfxapi import Client
        kwargs = {"api_key": self._api_key, "api_secret": self._api_secret}
        if self._wss_host:
            kwargs["wss_host"] = self._wss_host
        return Client(**kwargs)

    async def _close_client(self, bfx) -> None:
        try:
            await bfx.wss.close()
        except Exception:
            pass

    async def _supervise(self):
        """Sequential reconnect loop: connect -> run until disconnect/terminal
        -> (unless stopping) backoff -> reconnect with a FRESH client. A new
        client is built only after the previous one is closed, so two WS
        connections never coexist on the shared key."""
        delay = self._reconnect_min
        while not self._stopping:
            self._bfx = self._build_client()
            self._register_handlers()
            recon = asyncio.create_task(self._reconcile_loop())
            connected_at = self._clock()
            terminal = None
            try:
                await self._bfx.wss.start()     # blocks for the connection lifetime
            except Exception as exc:
                terminal = exc
                log.error("WS feed loop exited: %s", exc)
            finally:
                recon.cancel()
                try:
                    await recon
                except BaseException:
                    pass
                self._mark_down()               # REST fallback during the gap
                await self._close_client(self._bfx)
            if self._stopping:
                break
            if self._clock() - connected_at >= self._healthy_reset_after:
                delay = self._reconnect_min
            sleep_for = self._backoff(delay, terminal)
            log.warning("WS reconnecting in %.1fs", sleep_for)
            await self._sleep(sleep_for)
            delay = min(delay * self._reconnect_factor, self._reconnect_max)
```

Then update `stop()` to set the flag first. Change the opening of `stop()` from:

```python
    def stop(self) -> None:
        loop, thread, bfx = self._loop, self._thread, self._bfx
```

to:

```python
    def stop(self) -> None:
        self._stopping = True
        loop, thread, bfx = self._loop, self._thread, self._bfx
```

(The rest of `stop()` is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_ws_feed_reconnect.py -v`
Expected: all ten tests in the file PASS.

- [ ] **Step 5: Commit**

```bash
git add bitfinex/ws_feed.py tests/test_ws_feed_reconnect.py
git commit -m "feat(ws): sequential reconnect supervisor with capped backoff

Replaces the one-shot _main with _supervise: rebuilds a fresh bfxapi client
after a disconnect/terminal error (429, keepalive 1011, reconnect-timeout),
with exponential backoff + jitter, a higher floor for rate-limit causes, and a
healthy-connection reset. stop() halts reconnection. One client at a time.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the entire suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all tests PASS (prior baseline was 155; this plan adds the new tests). No failures, no errors.

- [ ] **Step 2: Confirm no stray references**

Run: `grep -rn "_main" bitfinex/ws_feed.py`
Expected: no remaining `_main` definition or call (it was replaced by `_supervise`).

---

## Task 6: Live deployment (OPERATOR STEP — not for automated execution)

> This step places the change on the LIVE Bitfinex connection with open positions. Do NOT run it as part of automated task execution. Perform it explicitly with the user, using the SIGKILL-preserves-positions procedure (a graceful stop would close the open shorts via `_shutdown()`).

- [ ] **Step 1: Identify the running live PID**

Run: `pgrep -af "main.py --mode live"`

- [ ] **Step 2: Snapshot current state**

Read the last `equity_snapshots` row and order count from
`instances/live536/data/trading.live.db`, and note open position count.

- [ ] **Step 3: Hard-kill (preserves open positions)**

Run: `kill -9 <PID>` then confirm dead with `ps -p <PID>`.
Rationale: graceful SIGTERM triggers `_shutdown()` → `close_all_positions()`. SIGKILL leaves the shorts on the exchange.

- [ ] **Step 4: Relaunch detached, identical command**

Run:
```bash
cd /mnt/hermes-data/.hermes/hermes-agent/trading-bot
setsid nohup .venv/bin/python main.py --mode live --instance default \
  --config config/live_536.yaml \
  > instances/live536/logs/restart_$(date +%F).out 2>&1 < /dev/null &
```

- [ ] **Step 5: Verify (within ~2–3 min)**

- Process alive (`pgrep -af "main.py --mode live"`), PPID 1.
- `data/.bfx_key_registry.json` shows the new PID (old reaped).
- `bot.log`: "Position sync complete: N position(s) loaded" with N = the open count from Step 2 (positions NOT closed).
- **Exactly one** auth WS connection: at most one "WS feed reconciled via REST" per ~90s (the collector no longer reconciles). Memory flat over time.
- After any induced/real WS drop, logs show "WS reconnecting in …s" followed by recovery, and **zero** `nonce: small` errors.
```
