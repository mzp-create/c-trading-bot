"""Tests for ledger-based P&L/fee reconciliation (the exchange ledger is the
source of truth — see bitfinex/ledger.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bitfinex import ledger
from bitfinex.ledger import LedgerEntry


# --- classify ---------------------------------------------------------------

def test_classify():
    assert ledger.classify("Position closed @ 72.3 (TRADE) on wallet margin") == ledger.POSITION_CLOSE
    assert ledger.classify("Trading fees for 0.1 BTC (BTCUST) @ 57824.0 on BFX (0.2%)") == ledger.TRADING_FEE
    assert ledger.classify("Margin Funding Charge on wallet margin") == ledger.FUNDING
    assert ledger.classify("Settlement @ 1.002 on wallet margin") == ledger.SETTLEMENT
    assert ledger.classify("Deposit on wallet exchange") == ledger.OTHER
    assert ledger.classify("") == ledger.OTHER


# --- parse_price ------------------------------------------------------------

def test_parse_price():
    assert ledger.parse_price("Position closed @ 72.3 (TRADE) on wallet margin") == 72.3
    assert ledger.parse_price("Position closed @ 1747.9 (TRADE)") == 1747.9
    assert ledger.parse_price("Trading fees for 0.1 BTC (BTCUST) @ 57824.0 on BFX") == 57824.0
    assert ledger.parse_price("Margin Funding Charge on wallet margin") is None


# --- symbol_from_price (ranges don't overlap: BTC~65k ETH~1750 SOL~73) ------

def test_symbol_from_price():
    refs = {"BTC/USDT": 65000.0, "ETH/USDT": 1750.0, "SOL/USDT": 73.0}
    assert ledger.symbol_from_price(66520.0, refs) == "BTC/USDT"
    assert ledger.symbol_from_price(1811.1, refs) == "ETH/USDT"
    assert ledger.symbol_from_price(72.3, refs) == "SOL/USDT"
    # nothing close enough -> None (don't guess wildly)
    assert ledger.symbol_from_price(5.0, refs) is None
    assert ledger.symbol_from_price(None, refs) is None


# --- from_raw ---------------------------------------------------------------

class _Raw:
    def __init__(self, **k): self.__dict__.update(k)


def test_from_raw_maps_and_classifies():
    e = ledger.from_raw(_Raw(id=10446171008, mts=1781719543000, currency="UST",
                             amount=-0.51584026, balance=519.67352843,
                             description="Position closed @ 72.3 (TRADE) on wallet margin"))
    assert e.id == 10446171008
    assert e.kind == ledger.POSITION_CLOSE
    assert e.price == 72.3
    assert e.amount == -0.51584026
    assert e.balance == 519.67352843


# --- summarize (the headline truth: realized pnl, fees, equity) --------------

def test_summarize():
    entries = [
        LedgerEntry(1, 100, "UST", +0.45, 520.7, "Position closed @ 74.1", ledger.POSITION_CLOSE, 74.1),
        LedgerEntry(2, 200, "UST", -0.52, 520.2, "Position closed @ 1747.9", ledger.POSITION_CLOSE, 1747.9),
        LedgerEntry(3, 300, "UST", -0.51, 519.7, "Position closed @ 72.3", ledger.POSITION_CLOSE, 72.3),
        LedgerEntry(4, 400, "UST", -0.07, 519.6, "Margin Funding Charge", ledger.FUNDING, None),
        LedgerEntry(5, 500, "UST", -1.20, 518.4, "Trading fees for 0.1 BTC @ 6", ledger.TRADING_FEE, 6.0),
    ]
    s = ledger.summarize(entries)
    assert round(s["realized_pnl"], 2) == round(0.45 - 0.52 - 0.51, 2)
    assert round(s["fees"], 2) == -1.20
    assert round(s["funding"], 2) == -0.07
    assert round(s["net_pnl"], 2) == round(0.45 - 0.52 - 0.51 - 1.20 - 0.07, 2)
    # current_balance = balance of the latest (max mts) entry
    assert s["current_balance"] == 518.4
    assert s["n_closes"] == 3 and s["n_fees"] == 1


def test_summarize_empty():
    s = ledger.summarize([])
    assert s["realized_pnl"] == 0 and s["fees"] == 0 and s["current_balance"] is None
