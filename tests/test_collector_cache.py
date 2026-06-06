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

    def __init__(self, config, mode=None, instance=None):
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
