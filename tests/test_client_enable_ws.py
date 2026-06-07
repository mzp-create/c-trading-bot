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
