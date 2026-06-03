"""
Unit tests for the position-close path (real-money critical bug fixes).

Covers:
  (a) a successful close returns success=True, records a trade, and clears meta
  (b) closing passes reduceOnly=True through to create_order
  (c) BitfinexClient.create_order's normalized success contract
  (d) symbol normalization to the unified display form

These tests use paper mode and a stubbed client so no network / real keys
are required.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.engine import ExecutionEngine
from market_data.bitfinex_client import BitfinexClient


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _paper_config(tmp_path):
    return {
        "trading": {"initial_capital": 10000.0, "symbol": "BTC/USDT"},
        "data": {"trades_file": str(tmp_path / "trades.csv")},
        "risk": {"stop_loss_pct": 2.0, "take_profit_pct": 4.0},
        "exchange": {"rate_limit": 0.0},
    }


class _StubClient:
    """Records create_order calls and returns the normalized success contract."""

    def __init__(self, average=105.0):
        self.calls = []
        self.average = average

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        self.calls.append({
            "symbol": symbol, "order_type": order_type, "side": side,
            "amount": amount, "price": price, "params": params or {},
        })
        return {
            "success": True,
            "id": "stub-1",
            "filled": amount,
            "average": self.average,
            "status": "closed",
            "symbol": symbol,
            "error": None,
            "raw": {},
        }

    def fetch_ticker(self, symbol):
        return {"last": self.average, "bid": self.average, "ask": self.average}


# ---------------------------------------------------------------------------
#  (a) successful close records a trade + clears meta  (paper mode)
# ---------------------------------------------------------------------------

def test_paper_close_success_records_trade_and_clears_meta(tmp_path):
    cfg = _paper_config(tmp_path)
    eng = ExecutionEngine(cfg, mode="paper", trade_direction="both")

    res = eng.execute_order("BTC/USDT", "buy", 0.1, 100.0, "market")
    assert res["success"] is True

    # Seed SL/TP meta to prove it gets cleared on close.
    eng._live_position_meta["BTC/USDT"] = {"stop_loss": 98.0}

    # Stub a client so paper close prices off a known ticker.
    eng._client = _StubClient(average=110.0)

    close = eng.close_position("BTC/USDT", reason="manual")

    assert close["success"] is True
    assert set(["success", "pnl", "price", "error"]).issubset(close.keys())
    assert close["error"] is None
    # long 0.1 @100 closed @110 -> ~+1.0 pnl
    assert close["pnl"] > 0
    # trade journaled
    assert len(eng._trade_history) == 1
    assert eng._trade_history[-1]["symbol"] == "BTC/USDT"
    # meta cleared, position gone
    assert "BTC/USDT" not in eng._live_position_meta
    assert all(p.get("symbol") != "BTC/USDT" for p in eng._open_positions)
    # Trade persisted to the database (CSV write was dropped in favour of SQLite)
    assert len(eng._repo.recent_trades(limit=1)) == 1


# ---------------------------------------------------------------------------
#  (b) live close passes reduceOnly=True to create_order
# ---------------------------------------------------------------------------

def test_live_close_passes_reduce_only(tmp_path):
    cfg = _paper_config(tmp_path)
    # Build engine in paper mode, then flip to live with a stub client so we
    # don't need real API keys.
    eng = ExecutionEngine(cfg, mode="paper", trade_direction="both")
    eng.mode = "live"
    stub = _StubClient(average=95.0)
    eng._client = stub

    # Inject a tracked long position (as live sync would).
    eng._open_positions = [{
        "symbol": "BTC/USDT", "side": "buy", "amount": 0.2,
        "entry_price": 100.0,
    }]
    eng._live_position_meta["BTC/USDT"] = {"stop_loss": 98.0}

    close = eng.close_position("BTC/USDT", reason="stop_loss")

    assert close["success"] is True
    assert close["error"] is None
    assert len(stub.calls) == 1
    call = stub.calls[0]
    # reduceOnly must be forwarded so the close does not open an opposing pos
    assert call["params"].get("reduceOnly") is True
    # close side is opposite of the long entry
    assert call["side"] == "sell"
    # unified symbol passed straight through (no t-format surgery)
    assert call["symbol"] == "BTC/USDT"
    # journaled + meta cleared on success
    assert len(eng._trade_history) == 1
    assert "BTC/USDT" not in eng._live_position_meta


def test_live_close_failure_does_not_record_or_clear(tmp_path):
    cfg = _paper_config(tmp_path)
    eng = ExecutionEngine(cfg, mode="paper", trade_direction="both")
    eng.mode = "live"

    class _FailClient(_StubClient):
        def create_order(self, *a, **k):
            super().create_order(*a, **k)
            return {
                "success": False, "id": None, "filled": 0.0, "average": None,
                "status": "rejected", "symbol": a[0],
                "error": "not enough tradable balance", "raw": None,
            }

    eng._client = _FailClient()
    eng._open_positions = [{
        "symbol": "BTC/USDT", "side": "buy", "amount": 0.2, "entry_price": 100.0,
    }]
    eng._live_position_meta["BTC/USDT"] = {"stop_loss": 98.0}

    close = eng.close_position("BTC/USDT", reason="manual")

    assert close["success"] is False
    assert close["error"] == "not enough tradable balance"
    assert close["pnl"] == 0.0
    # On failure: NOT recorded, meta retained
    assert len(eng._trade_history) == 0
    assert "BTC/USDT" in eng._live_position_meta


# ---------------------------------------------------------------------------
#  (c) create_order normalized success contract (paper mode, no network)
# ---------------------------------------------------------------------------

def test_create_order_normalized_contract_paper():
    cfg = {
        "trading": {"initial_capital": 10000.0},
        "exchange": {"rate_limit": 0.0},
    }
    client = BitfinexClient(cfg, mode="paper")

    result = client.create_order("BTC/USDT", "market", "buy", 0.01)

    for key in ("success", "id", "filled", "average", "status", "symbol", "error", "raw"):
        assert key in result, f"missing key {key}"
    assert result["success"] is True
    assert result["error"] is None
    assert isinstance(result["filled"], float)
    assert result["filled"] == 0.01


def test_create_order_error_contract_is_normalized():
    cfg = {"trading": {"initial_capital": 100.0}, "exchange": {"rate_limit": 0.0}}
    client = BitfinexClient(cfg, mode="paper")
    err = client._error_order_result("boom", "BTC/USDT")
    assert err["success"] is False
    assert err["error"] == "boom"
    assert err["id"] is None
    assert err["filled"] == 0.0


def test_client_close_position_uses_reduce_only(monkeypatch):
    cfg = {"trading": {"initial_capital": 100.0}, "exchange": {"rate_limit": 0.0}}
    client = BitfinexClient(cfg, mode="paper")

    captured = {}

    def fake_create_order(symbol, order_type, side, amount, price=None, params=None):
        captured.update({"symbol": symbol, "side": side, "params": params})
        return client._normalize_order_result({"id": "x", "filled": amount}, symbol)

    # Simulate an open long paper position.
    client._paper_positions["BTC/USDT"] = {
        "symbol": "BTC/USDT", "contracts": 0.5, "side": "long", "entryPrice": 100.0,
    }
    # Force the non-paper branch path by stubbing fetch_position + create_order.
    client.mode = "live"
    monkeypatch.setattr(client, "fetch_position", lambda s: {
        "symbol": s, "contracts": 0.5, "side": "long",
    })
    monkeypatch.setattr(client, "create_order", fake_create_order)

    res = client.close_position("BTC/USDT")
    assert res["success"] is True
    assert captured["params"].get("reduceOnly") is True
    assert captured["side"] == "sell"


# ---------------------------------------------------------------------------
#  (d) symbol normalization to unified display form
# ---------------------------------------------------------------------------

def test_symbol_normalization():
    norm = ExecutionEngine._normalize_symbol
    assert norm("tBTCUST") == "BTC/USDT"
    assert norm("BTC/USDT:USDT") == "BTC/USDT"
    assert norm("BTC/USDT") == "BTC/USDT"
    assert norm("tETHUSD") == "ETH/USD"
    assert norm("ETH/USD:USD") == "ETH/USD"


def test_get_live_positions_maps_unified_symbol(tmp_path):
    cfg = _paper_config(tmp_path)
    cfg["trading"]["symbols"] = [{"name": "BTC/USDT", "symbol": "tBTCUST", "enabled": True}]
    eng = ExecutionEngine(cfg, mode="paper", trade_direction="both")

    # fetch_positions returns CCXT unified derivative symbols.
    class _PosClient:
        def fetch_positions(self):
            return [{
                "symbol": "BTC/USDT:USDT", "contracts": 0.3, "side": "long",
                "entryPrice": 100.0, "unrealizedPnl": 1.0,
            }]

    eng._client = _PosClient()
    positions = eng._get_live_positions()
    assert len(positions) == 1
    # Must map to the config display name, NOT the raw derivative symbol.
    assert positions[0]["symbol"] == "BTC/USDT"
    assert positions[0]["side"] == "buy"
