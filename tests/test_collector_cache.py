"""Regression tests for MarketDataCollector mode resolution and the OHLCV
cache staleness guard (both tied to the 2026-06-05 stale-price incident)."""

import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

import market_data.collector as collector_mod


class _FakeClient:
    """Stand-in for BitfinexClient that records the mode it was built with and
    makes no network calls / key registrations."""

    def __init__(self, config, mode=None, instance=None, enable_ws=True):
        self.config = config
        self.mode = mode
        self.instance = instance


def _collector(monkeypatch, tmp_path, config):
    monkeypatch.setattr(collector_mod, "BitfinexClient", _FakeClient)
    config.setdefault("data", {})["ohlcv_dir"] = str(tmp_path)
    return collector_mod.MarketDataCollector(config, mode=config.pop("_mode", None))


# ── C1: mode resolution ──────────────────────────────────────────────────────

def test_explicit_paper_mode_wins_over_testnet_false(monkeypatch, tmp_path):
    """C1 regression: an explicit mode must override the testnet derivation, so
    a paper run never builds a live client just because testnet=false."""
    c = _collector(monkeypatch, tmp_path,
                   {"exchange": {"testnet": False}, "_mode": "paper"})
    assert c.client.mode == "paper"


def test_mode_derived_live_when_not_given(monkeypatch, tmp_path):
    """Documents the fallback: no explicit mode + testnet=false -> live."""
    c = _collector(monkeypatch, tmp_path, {"exchange": {"testnet": False}})
    assert c.client.mode == "live"


def test_trading_mode_config_override(monkeypatch, tmp_path):
    c = _collector(monkeypatch, tmp_path,
                   {"exchange": {"testnet": False}, "trading": {"mode": "paper"}})
    assert c.client.mode == "paper"


# ── candle staleness ─────────────────────────────────────────────────────────

def _write_cache(path, newest_ts_ms):
    """Write a 2-row OHLCV CSV (epoch-ms timestamp index) ending at newest_ts_ms."""
    pd.DataFrame({
        "timestamp": [newest_ts_ms - 3_600_000, newest_ts_ms],
        "open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
        "close": [1.0, 1.0], "volume": [1.0, 1.0],
    }).to_csv(path, index=False)


def test_cache_rejects_stale_candles_despite_fresh_mtime(monkeypatch, tmp_path):
    """The core 2026-06-05 fix: a freshly *written* file holding days-old candles
    must be rejected (file mtime alone is not enough)."""
    c = _collector(monkeypatch, tmp_path, {"exchange": {"testnet": True}})
    path = tmp_path / "BTC_USDT_1h.csv"
    old_ms = int((time.time() - 10 * 86_400) * 1000)  # 10 days old
    _write_cache(path, old_ms)
    assert c._load_from_cache(path, "1h") is None


def test_cache_accepts_fresh_candles(monkeypatch, tmp_path):
    c = _collector(monkeypatch, tmp_path, {"exchange": {"testnet": True}})
    path = tmp_path / "BTC_USDT_1h.csv"
    fresh_ms = int(time.time() * 1000)  # current candle
    _write_cache(path, fresh_ms)
    df = c._load_from_cache(path, "1h")
    assert df is not None and len(df) == 2


# ── since window (stale-fetch regression) ────────────────────────────────────

def test_ohlcv_since_window_ends_near_now(monkeypatch, tmp_path):
    """Regression for the 2026-06-05 stale-price bug: the 1h `since` must cover
    the most recent `limit` candles (window ends at ~now), not start 2x too far
    back and end ~limit periods in the PAST (the old `* 2` lookback)."""
    captured = {}

    class _CaptureClient:
        def __init__(self, config, mode=None, instance=None, enable_ws=True):
            pass

        def get_ohlcv(self, symbol, timeframe, limit, since=None):
            captured["since"] = since
            captured["limit"] = limit
            now_ms = int(time.time() * 1000)
            df = pd.DataFrame(
                {"open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
                 "close": [1.0, 1.0], "volume": [1.0, 1.0]},
                index=[now_ms - 3_600_000, now_ms])
            df.index.name = "timestamp"
            return df

    monkeypatch.setattr(collector_mod, "BitfinexClient", _CaptureClient)
    c = collector_mod.MarketDataCollector(
        {"exchange": {"testnet": True}, "data": {"ohlcv_dir": str(tmp_path)}},
        mode="paper")

    limit, tf = 200, 3600
    c.get_ohlcv("ETH/USDT", "1h", limit=limit)

    since_s = captured["since"] / 1000.0
    now = time.time()
    # Newest candle the API can return ≈ since + limit*tf; it must reach ~now,
    # not sit ~limit periods (8+ days for 1h) in the past.
    newest_expected = since_s + captured["limit"] * tf
    assert newest_expected >= now - 2 * tf, (
        f"fetch window ends {(now - newest_expected) / 86400:.1f}d in the past "
        f"— stale (the '* 2' regression)")
    # Lookback is ~limit periods, not 2x.
    assert abs((now - since_s) - limit * tf) < tf
