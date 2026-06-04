import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bitfinex.models import Position, Wallet, Fill
from state.account_state import AccountState


def _pos(symbol, amount):
    return Position(symbol=symbol, side="long" if amount > 0 else "short",
                    amount=amount, entry_price=100.0, unrealized_pnl=1.0,
                    leverage=2.0, raw_symbol="tBTCUST")


def test_status_defaults_false():
    a = AccountState()
    assert a.connected is False and a.authenticated is False
    a.set_status(connected=True, authenticated=True)
    assert a.connected and a.authenticated


def test_position_snapshot_then_incremental():
    a = AccountState()
    a.apply_position_snapshot([_pos("BTC/USDT", 0.5), _pos("ETH/USDT", -1.0)])
    assert len(a.get_positions()) == 2
    a.apply_position(_pos("BTC/USDT", 0.8))           # upsert
    assert a.get_position("BTC/USDT").amount == 0.8
    a.remove_position("ETH/USDT")                     # close
    assert a.get_position("ETH/USDT") is None
    assert len(a.get_positions()) == 1


def test_wallets_and_fills():
    a = AccountState()
    a.apply_wallet_snapshot([Wallet(currency="USDT", wallet_type="margin",
                                    balance=538.0, available=500.0)])
    a.apply_wallet(Wallet(currency="USDT", wallet_type="margin",
                          balance=540.0, available=502.0))
    assert a.get_wallets()[0].balance == 540.0
    a.apply_fill(Fill(symbol="BTC/USDT", side="sell", amount=0.5, price=110.0,
                      fee=-0.1, fee_currency="USDT", order_id=1, trade_id=7,
                      ts="2026-06-04T00:00:00+00:00"))
    assert a.last_fill("BTC/USDT").fee == -0.1
    assert a.last_fill("ETH/USDT") is None


def test_mark_reconciled():
    a = AccountState()
    assert a.last_reconcile is None
    a.mark_reconciled()
    assert a.last_reconcile is not None
