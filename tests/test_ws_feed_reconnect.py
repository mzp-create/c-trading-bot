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
