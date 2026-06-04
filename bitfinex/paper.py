"""Paper-mode broker: simulates auth methods, returns the SAME typed models as
the live path so callers are mode-agnostic. Public data (OHLCV/ticker) is NOT
here — it is always real/public via the client."""

from typing import List, Optional

from bitfinex.models import Order, Position, Wallet, Fill


class PaperBroker:
    def __init__(self, initial_capital: float = 1000.0, quote: str = "USDT"):
        self._capital = float(initial_capital)
        self._quote = quote
        self._positions: list[Position] = []
        self._next_id = 1

    def submit_order(self, symbol: str, side: str, amount: float, *,
                     order_type: str = "market", price: Optional[float] = None,
                     reduce_only: bool = False) -> Order:
        fill_price = float(price or 0.0)
        oid = self._next_id
        self._next_id += 1
        if reduce_only:
            self._positions = [p for p in self._positions if p.symbol != symbol]
        else:
            self._positions = [p for p in self._positions if p.symbol != symbol]
            signed = abs(amount) if side == "buy" else -abs(amount)
            self._positions.append(Position(
                symbol=symbol, side="long" if signed > 0 else "short",
                amount=signed, entry_price=fill_price, unrealized_pnl=0.0,
                leverage=1.0, raw_symbol=symbol))
        return Order(id=oid, symbol=symbol, side=side, order_type=order_type,
                     amount=abs(amount), filled=abs(amount), avg_price=fill_price,
                     status="EXECUTED", reduce_only=reduce_only, fee=0.0,
                     fee_currency=None, raw=None)

    def cancel_order(self, order_id: int) -> Order:
        return Order(id=order_id, symbol="", side="buy", order_type="market",
                     amount=0.0, filled=0.0, avg_price=None, status="CANCELED",
                     reduce_only=False, fee=0.0, fee_currency=None, raw=None)

    def get_positions(self) -> List[Position]:
        return list(self._positions)

    def get_wallets(self) -> List[Wallet]:
        return [Wallet(currency=self._quote, wallet_type="margin",
                       balance=self._capital, available=self._capital)]

    def get_trades(self, symbol: Optional[str] = None, since=None,
                   limit=None) -> List[Fill]:
        return []
