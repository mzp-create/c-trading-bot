"""Paper-mode broker: simulates auth methods with slippage/fee/wallet tracking,
returning the SAME typed models as the live path so the engine is mode-agnostic.
PnL is computed by the engine (from position entry + the close order avg_price);
this broker only produces realistic fills and tracks balances/positions.
Open + reduce-only-close semantics, matching the live flow.
"""

from typing import List, Optional

from bitfinex.models import Order, Position, Wallet, Fill
from bitfinex.errors import OrderRejected

# Mirror execution/engine.py's paper constants.
TAKER_FEE = 0.001    # 0.1%
MAKER_FEE = 0.0      # 0.0%
SLIPPAGE = 0.0005    # 0.05% on market fills


class PaperBroker:
    def __init__(self, initial_capital: float = 1000.0, quote: str = "USDT",
                 base_symbols: Optional[List[str]] = None):
        self._quote = quote
        # spot-style wallets: currency -> {"balance", "available"}
        self._wallets = {quote: {"balance": float(initial_capital),
                                 "available": float(initial_capital)}}
        for sym in (base_symbols or []):
            base = sym.split("/")[0]
            self._wallets.setdefault(base, {"balance": 0.0, "available": 0.0})
        self._positions: dict[str, Position] = {}   # symbol -> Position
        self._fills: dict[str, Fill] = {}
        self._next_id = 1

    # ── orders ───────────────────────────────────────────────────────────
    def submit_order(self, symbol: str, side: str, amount: float, *,
                     order_type: str = "market", price: Optional[float] = None,
                     reduce_only: bool = False) -> Order:
        base, quote = symbol.split("/")
        ref = float(price or 0.0)
        if order_type == "market":
            fill_price = ref * (1 + SLIPPAGE) if side == "buy" else ref * (1 - SLIPPAGE)
        else:
            fill_price = ref
        fill_price = round(fill_price, 8)
        fee_rate = TAKER_FEE if order_type == "market" else MAKER_FEE
        fee = fill_price * amount * fee_rate
        cost = fill_price * amount
        oid = self._next_id
        self._next_id += 1

        self._wallets.setdefault(base, {"balance": 0.0, "available": 0.0})
        self._wallets.setdefault(quote, {"balance": 0.0, "available": 0.0})

        if reduce_only:
            # Close/reduce: opposite-side wallet move, drop the position.
            pos = self._positions.get(symbol)
            if pos is not None and pos.side == "long":
                self._wallets[base]["available"] -= amount
                self._wallets[base]["balance"] -= amount
                self._wallets[quote]["available"] += cost - fee
                self._wallets[quote]["balance"] += cost - fee
            elif pos is not None and pos.side == "short":
                self._wallets[quote]["available"] += cost - fee
                self._wallets[quote]["balance"] += cost - fee
                self._wallets[base]["available"] += amount
                self._wallets[base]["balance"] += amount
            self._positions.pop(symbol, None)
        else:
            # Open: balance check, wallet move, create position.
            if side == "buy":
                need = cost + fee
                if need > self._wallets[quote]["available"] + 1e-12:
                    raise OrderRejected(
                        f"Insufficient {quote}: need {need:.2f}, "
                        f"have {self._wallets[quote]['available']:.2f}")
                self._wallets[quote]["available"] -= need
                self._wallets[quote]["balance"] -= need
                self._wallets[base]["available"] += amount
                self._wallets[base]["balance"] += amount
            else:  # sell to open (spot-style, mirrors current paper sim)
                if amount > self._wallets[base]["available"] + 1e-12:
                    raise OrderRejected(
                        f"Insufficient {base}: need {amount:.6f}, "
                        f"have {self._wallets[base]['available']:.6f}")
                self._wallets[base]["available"] -= amount
                self._wallets[base]["balance"] -= amount
                self._wallets[quote]["available"] += cost - fee
                self._wallets[quote]["balance"] += cost - fee
            self._positions[symbol] = Position(
                symbol=symbol, side="long" if side == "buy" else "short",
                amount=abs(amount) if side == "buy" else -abs(amount),
                entry_price=fill_price, unrealized_pnl=0.0, leverage=1.0,
                raw_symbol=symbol)

        self._fills[symbol] = Fill(symbol=symbol, side=side, amount=abs(amount),
                                   price=fill_price, fee=fee, fee_currency=quote,
                                   order_id=oid, trade_id=oid, ts="")
        return Order(id=oid, symbol=symbol, side=side, order_type=order_type,
                     amount=abs(amount), filled=abs(amount), avg_price=fill_price,
                     status="EXECUTED", reduce_only=reduce_only, fee=fee,
                     fee_currency=quote, raw=None)

    def cancel_order(self, order_id: int) -> Order:
        return Order(id=order_id, symbol="", side="buy", order_type="market",
                     amount=0.0, filled=0.0, avg_price=None, status="CANCELED",
                     reduce_only=False, fee=0.0, fee_currency=None, raw=None)

    # ── reads ────────────────────────────────────────────────────────────
    def get_positions(self) -> List[Position]:
        return list(self._positions.values())

    def get_wallets(self) -> List[Wallet]:
        return [Wallet(currency=c, wallet_type="margin",
                       balance=w["balance"], available=w["available"])
                for c, w in self._wallets.items()]

    def get_trades(self, symbol: Optional[str] = None, since=None,
                   limit=None) -> List[Fill]:
        if symbol:
            f = self._fills.get(symbol)
            return [f] if f else []
        return list(self._fills.values())
