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
