"""bfxapi REST wrapper → typed models. Owns all authenticated calls + public
ticker. Accepts an injected bfx client for testing."""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from bitfinex import symbols
from bitfinex.models import Order, Position, Wallet, Fill, Ticker
from bitfinex.errors import OrderRejected, AckUnparseable

log = logging.getLogger(__name__)

REDUCE_ONLY = 1024  # Bitfinex flag bitmask (no enum in bfxapi)

# Bitfinex currency ticker -> display currency.
_CCY_TO_DISPLAY = {"UST": "USDT"}


class BfxRest:
    def __init__(self, api_key: str, api_secret: str, client=None):
        if client is None:
            from bfxapi import Client, REST_HOST
            client = Client(rest_host=REST_HOST, api_key=api_key,
                            api_secret=api_secret)
        self._client = client

    # ── orders ───────────────────────────────────────────────────────────
    def submit_order(self, symbol: str, side: str, amount: float, *,
                     order_type: str = "market", price: Optional[float] = None,
                     reduce_only: bool = False) -> Order:
        bfx_symbol = symbols.to_bitfinex(symbol)
        signed = abs(amount) if side.lower() == "buy" else -abs(amount)
        bfx_type = {"market": "MARKET", "limit": "LIMIT",
                    "stop": "STOP"}.get(order_type.lower(), "LIMIT")
        flags = REDUCE_ONLY if reduce_only else 0
        try:
            notif = self._client.rest.auth.submit_order(
                type=bfx_type, symbol=bfx_symbol, amount=f"{signed:.8f}",
                price=f"{price or 0}", flags=flags)
        except Exception as exc:  # transport/parse — outcome UNKNOWN
            raise AckUnparseable(
                f"submit_order raised, outcome unknown: {exc}") from exc
        if getattr(notif, "status", None) == "ERROR":
            raise OrderRejected(getattr(notif, "text", "order rejected"))
        return self._order_from_bfx(notif.data, symbol, side, order_type,
                                    abs(amount), reduce_only)

    def cancel_order(self, order_id: int) -> Order:
        try:
            notif = self._client.rest.auth.cancel_order(id=order_id)
        except Exception as exc:
            raise AckUnparseable(f"cancel_order raised: {exc}") from exc
        if getattr(notif, "status", None) == "ERROR":
            raise OrderRejected(getattr(notif, "text", "cancel rejected"))
        o = notif.data
        return self._order_from_bfx(o, symbols.to_display(o.symbol),
                                    "buy" if o.amount_orig >= 0 else "sell",
                                    "market", abs(o.amount_orig or 0.0), False)

    def get_open_orders(self) -> List[Order]:
        out = []
        for o in self._client.rest.auth.get_orders():
            amt = float(o.amount_orig or 0.0)
            out.append(Order(
                id=o.id, symbol=symbols.to_display(o.symbol),
                side="buy" if amt >= 0 else "sell",
                order_type=(o.order_type or "").lower().replace(" ", "_") or "stop",
                amount=abs(amt), filled=0.0,
                avg_price=(getattr(o, "price", None) or None),
                status=(o.order_status or "").upper(),
                reduce_only=bool((getattr(o, "flags", 0) or 0) & REDUCE_ONLY),
                fee=0.0, fee_currency=None, raw=o))
        return out

    def _order_from_bfx(self, o, display_symbol, side, order_type, amount,
                        reduce_only) -> Order:
        status = (o.order_status or "").upper()
        return Order(
            id=o.id, symbol=display_symbol, side=side, order_type=order_type,
            amount=amount, filled=abs(o.amount_orig or 0.0) - abs(o.amount or 0.0),
            avg_price=(o.price_avg or None), status=status,
            reduce_only=reduce_only, fee=0.0, fee_currency=None, raw=o)

    # ── reads ────────────────────────────────────────────────────────────
    def get_positions(self) -> List[Position]:
        out = []
        for p in self._client.rest.auth.get_positions():
            amt = float(p.amount)
            out.append(Position(
                symbol=symbols.to_display(p.symbol),
                side="long" if amt > 0 else "short",
                amount=amt, entry_price=float(p.base_price or 0.0),
                unrealized_pnl=float(getattr(p, "pl", 0.0) or 0.0),
                leverage=float(getattr(p, "leverage", 0.0) or 0.0),
                raw_symbol=p.symbol))
        return out

    def get_wallets(self) -> List[Wallet]:
        return [Wallet(currency=_CCY_TO_DISPLAY.get(w.currency, w.currency),
                       wallet_type=w.wallet_type,
                       balance=float(w.balance or 0.0),
                       available=float(getattr(w, "available_balance", 0.0) or 0.0))
                for w in self._client.rest.auth.get_wallets()]

    def get_ticker(self, symbol: str) -> Ticker:
        t = self._client.rest.public.get_t_ticker(symbols.to_bitfinex(symbol))
        return Ticker(symbol=symbol, bid=float(t.bid), ask=float(t.ask),
                      last=float(t.last_price))

    def get_trades(self, symbol: Optional[str] = None, since: Optional[int] = None,
                   limit: Optional[int] = None) -> List[Fill]:
        bfx_symbol = symbols.to_bitfinex(symbol) if symbol else None
        rows = self._client.rest.auth.get_trades_history(
            symbol=bfx_symbol, start=str(since) if since else None, limit=limit)
        out = []
        for t in rows:
            amt = float(t.exec_amount)
            out.append(Fill(
                symbol=symbols.to_display(t.symbol),
                side="buy" if amt > 0 else "sell", amount=abs(amt),
                price=float(t.exec_price), fee=float(t.fee or 0.0),
                fee_currency=_CCY_TO_DISPLAY.get(getattr(t, "fee_currency", None),
                                                 getattr(t, "fee_currency", None)),
                order_id=getattr(t, "order_id", None), trade_id=t.id,
                ts=datetime.fromtimestamp((t.mts_create or 0) / 1000,
                                          tz=timezone.utc).isoformat()))
        return out
