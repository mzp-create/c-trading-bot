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
