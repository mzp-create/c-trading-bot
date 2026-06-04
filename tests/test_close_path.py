"""
Unit tests for the position-close path (real-money critical bug fixes).

Covers:
  (a) a successful close returns success=True, records a trade, and clears meta
  (b) closing passes reduce_only=True through to create_order
  (c) BitfinexClient (typed) create_order paper-mode contract
  (d) symbol normalization to the unified display form

These tests use paper mode and a stubbed client so no network / real keys
are required.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.engine import ExecutionEngine
from bitfinex import BitfinexClient
from bitfinex.models import Order, Position, Ticker
from bitfinex.errors import OrderRejected


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
    """Typed stub: returns typed models matching bitfinex.BitfinexClient."""

    def __init__(self, average=105.0):
        self.calls = []
        self.average = average
        self.mode = "live"

    def create_order(self, symbol, side, amount, *, order_type="market",
                     price=None, reduce_only=False):
        self.calls.append({
            "symbol": symbol, "side": side, "amount": amount,
            "order_type": order_type, "price": price,
            "reduce_only": reduce_only,
        })
        return Order(id=1234, symbol=symbol, side=side, order_type=order_type,
                     amount=amount, filled=amount, avg_price=self.average,
                     status="EXECUTED", reduce_only=reduce_only, fee=0.0,
                     fee_currency=None, raw=None)

    def close_position(self, symbol):
        return self.create_order(symbol, "sell", 0.3, reduce_only=True)

    def fetch_ticker(self, symbol):
        return Ticker(symbol=symbol, bid=self.average, ask=self.average,
                      last=self.average)

    def fetch_positions(self):
        return [Position(symbol="BTC/USDT", side="long", amount=0.3,
                         entry_price=100.0, unrealized_pnl=1.0, leverage=2.0,
                         raw_symbol="tBTCUST")]

    def fetch_position(self, symbol):
        for p in self.fetch_positions():
            if p.symbol == symbol:
                return p
        return None


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
#  (b) live close passes reduce_only=True to create_order
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
    # reduce_only must be True so the close does not open an opposing pos
    assert call["reduce_only"] is True
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
        def create_order(self, symbol, side, amount, *, order_type="market",
                         price=None, reduce_only=False):
            # Record the call then raise OrderRejected
            self.calls.append({
                "symbol": symbol, "side": side, "amount": amount,
                "order_type": order_type, "price": price,
                "reduce_only": reduce_only,
            })
            raise OrderRejected("not enough tradable balance")

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
#  (c) create_order paper-mode contract (typed return)
# ---------------------------------------------------------------------------

def test_create_order_normalized_contract_paper():
    cfg = {
        "trading": {"initial_capital": 10000.0},
        "exchange": {"rate_limit": 0.0},
    }
    client = BitfinexClient(cfg, mode="paper")

    order = client.create_order("BTC/USDT", "buy", 0.01, price=100.0)

    assert isinstance(order, Order)
    assert order.is_filled is True
    assert order.id is not None
    assert isinstance(order.filled, float)
    assert order.filled == 0.01
    assert order.symbol == "BTC/USDT"
    assert order.side == "buy"


def test_create_order_paper_reduce_only():
    cfg = {"trading": {"initial_capital": 100.0}, "exchange": {"rate_limit": 0.0}}
    client = BitfinexClient(cfg, mode="paper")

    order = client.create_order("BTC/USDT", "sell", 0.01, price=100.0,
                                reduce_only=True)
    assert isinstance(order, Order)
    assert order.reduce_only is True
    assert order.is_filled is True


def test_client_close_position_uses_reduce_only(monkeypatch):
    cfg = {"trading": {"initial_capital": 100.0}, "exchange": {"rate_limit": 0.0}}
    client = BitfinexClient(cfg, mode="paper")

    captured = {}

    def fake_submit_order(symbol, side, amount, *, order_type="market",
                          price=None, reduce_only=False):
        captured.update({"symbol": symbol, "side": side,
                         "reduce_only": reduce_only})
        return Order(id=1, symbol=symbol, side=side, order_type=order_type,
                     amount=amount, filled=amount, avg_price=price or 0.0,
                     status="EXECUTED", reduce_only=reduce_only, fee=0.0,
                     fee_currency=None, raw=None)

    # close_position fetches position + ticker then calls self._auth.submit_order
    from bitfinex.models import Position
    monkeypatch.setattr(client, "fetch_position", lambda s: Position(
        symbol=s, side="long", amount=0.5, entry_price=100.0,
        unrealized_pnl=0.0, leverage=1.0, raw_symbol="tBTCUST"))
    monkeypatch.setattr(client, "fetch_ticker", lambda s: Ticker(
        symbol=s, bid=100.0, ask=100.0, last=100.0))
    monkeypatch.setattr(client._auth, "submit_order", fake_submit_order)

    result = client.close_position("BTC/USDT")
    assert isinstance(result, Order)
    assert captured["reduce_only"] is True
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

    # fetch_positions returns typed Position objects (new BitfinexClient).
    class _PosClient:
        def fetch_positions(self):
            return [Position(symbol="BTC/USDT", side="long", amount=0.3,
                             entry_price=100.0, unrealized_pnl=1.0,
                             leverage=2.0, raw_symbol="tBTCUST")]

    eng._client = _PosClient()
    positions = eng._get_live_positions()
    assert len(positions) == 1
    # Must map to the config display name, NOT the raw derivative symbol.
    assert positions[0]["symbol"] == "BTC/USDT"
    assert positions[0]["side"] == "buy"
