"""Background WebSocket feed: pushes ticker + auth updates into MarketState /
AccountState, and submits orders over WS with a sync cid-correlated bridge.

Runs a daemon thread with its own asyncio loop. Handler methods are sync and
directly unit-testable. On reconnect (re-auth) it REST-reconciles, because
bfxapi snapshots fire only once per connection. Reuses bitfinex.rest mapping.
"""

import asyncio
import itertools
import logging
import threading
import time
from datetime import datetime, timezone
from typing import List, Optional

from bitfinex import symbols
from bitfinex.models import Order, Position, Ticker, Wallet, Fill
from bitfinex.errors import OrderRejected, AckUnparseable
from bitfinex.rest import REDUCE_ONLY, _CCY_TO_DISPLAY

log = logging.getLogger(__name__)


def _iso(mts: Optional[int]) -> str:
    return datetime.fromtimestamp((mts or 0) / 1000, tz=timezone.utc).isoformat()


def _ccy(c):
    return _CCY_TO_DISPLAY.get(c, c)


class WsFeed:
    def __init__(self, api_key: str, api_secret: str, symbols_list: List[str],
                 market_state, account_state, rest, *,
                 ticker_staleness: float = 15.0,
                 order_confirm_timeout: float = 10.0,
                 reconcile_interval: float = 300.0,
                 wss_host: Optional[str] = None, bfx=None):
        self._api_key = api_key
        self._api_secret = api_secret
        self._symbols = list(symbols_list)
        self._market = market_state
        self._account = account_state
        self._rest = rest
        self.ticker_staleness = ticker_staleness
        self._order_confirm_timeout = order_confirm_timeout
        self._reconcile_interval = reconcile_interval
        self._wss_host = wss_host
        self._bfx = bfx                         # injectable; built in start()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._cid = itertools.count(1)
        self._pending: dict[int, tuple] = {}    # cid -> (Event, holder)
        self._pending_lock = threading.Lock()
        self._auth_count = 0

    # ── mapping ──────────────────────────────────────────────────────────
    def _pos(self, p) -> Position:
        amt = float(p.amount)
        return Position(symbol=symbols.to_display(p.symbol),
                        side="long" if amt > 0 else "short", amount=amt,
                        entry_price=float(getattr(p, "base_price", 0.0) or 0.0),
                        unrealized_pnl=float(getattr(p, "pl", 0.0) or 0.0),
                        leverage=float(getattr(p, "leverage", 0.0) or 0.0),
                        raw_symbol=p.symbol)

    def _wallet(self, w) -> Wallet:
        return Wallet(currency=_ccy(w.currency), wallet_type=w.wallet_type,
                      balance=float(w.balance or 0.0),
                      available=float(getattr(w, "available_balance", 0.0) or 0.0))

    def _fill(self, t) -> Fill:
        amt = float(t.exec_amount)
        return Fill(symbol=symbols.to_display(t.symbol),
                    side="buy" if amt > 0 else "sell", amount=abs(amt),
                    price=float(t.exec_price), fee=float(t.fee or 0.0),
                    fee_currency=_ccy(getattr(t, "fee_currency", None)),
                    order_id=getattr(t, "order_id", None), trade_id=t.id,
                    ts=_iso(getattr(t, "mts_create", 0)))

    def _order(self, o) -> Order:
        status = (getattr(o, "order_status", "") or "").upper()
        amt = float(getattr(o, "amount_orig", 0.0) or 0.0)
        return Order(id=o.id, symbol=symbols.to_display(o.symbol),
                     side="buy" if amt >= 0 else "sell",
                     order_type="market", amount=abs(amt), filled=abs(amt),
                     avg_price=(getattr(o, "price_avg", None) or None),
                     status=status, reduce_only=False, fee=0.0,
                     fee_currency=None, raw=o)

    # ── handlers (sync; unit-tested directly) ────────────────────────────
    def _on_open(self):
        self._account.set_status(connected=True,
                                 authenticated=self._account.authenticated)

    def _on_authenticated(self, data=None):
        self._auth_count += 1
        self._account.set_status(connected=True, authenticated=True)
        if self._auth_count > 1:        # reconnect: snapshot won't refire
            self._reconcile()

    def _on_disconnected(self, *a):
        self._account.set_status(connected=False, authenticated=False)

    def _on_ticker(self, sub, ticker):
        disp = symbols.to_display(sub["symbol"])
        self._market.update_ticker(Ticker(symbol=disp, bid=float(ticker.bid),
                                          ask=float(ticker.ask),
                                          last=float(ticker.last_price)))

    def _on_position_snapshot(self, positions):
        self._account.apply_position_snapshot([self._pos(p) for p in positions])

    def _on_position(self, p):
        self._account.apply_position(self._pos(p))

    def _on_position_close(self, p):
        self._account.remove_position(symbols.to_display(p.symbol))

    def _on_wallet_snapshot(self, wallets):
        self._account.apply_wallet_snapshot([self._wallet(w) for w in wallets])

    def _on_wallet(self, w):
        self._account.apply_wallet(self._wallet(w))

    def _on_fill(self, t):
        self._account.apply_fill(self._fill(t))

    def _reconcile(self):
        try:
            self._account.apply_position_snapshot(self._rest.get_positions())
            self._account.apply_wallet_snapshot(self._rest.get_wallets())
            self._account.mark_reconciled()
            log.info("WS feed reconciled via REST")
        except Exception as exc:
            log.error("WS reconcile failed: %s", exc)

    # ── health ───────────────────────────────────────────────────────────
    def is_healthy(self) -> bool:
        return self._account.connected and self._account.authenticated

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="bfx-wss",
                                        daemon=True)
        self._thread.start()

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        if self._bfx is None:
            from bfxapi import Client
            kwargs = {"api_key": self._api_key, "api_secret": self._api_secret}
            if self._wss_host:
                kwargs["wss_host"] = self._wss_host
            self._bfx = Client(**kwargs)
        self._register_handlers()
        try:
            loop.run_until_complete(self._bfx.wss.start())
        except Exception as exc:                # never crash the process
            log.error("WS feed loop exited: %s", exc)
        finally:
            loop.close()

    def _register_handlers(self):
        wss = self._bfx.wss

        @wss.on("open")
        async def _open():
            self._safe(self._on_open)
            for s in self._symbols:
                await wss.subscribe("ticker", symbol=symbols.to_bitfinex(s))

        wss.on("authenticated", lambda data=None: self._safe(self._on_authenticated, data))
        wss.on("disconnected", lambda *a: self._safe(self._on_disconnected))
        wss.on("t_ticker_update", lambda sub, tk: self._safe(self._on_ticker, sub, tk))
        wss.on("position_snapshot", lambda ps: self._safe(self._on_position_snapshot, ps))
        wss.on("position_new", lambda p: self._safe(self._on_position, p))
        wss.on("position_update", lambda p: self._safe(self._on_position, p))
        wss.on("position_close", lambda p: self._safe(self._on_position_close, p))
        wss.on("wallet_snapshot", lambda ws: self._safe(self._on_wallet_snapshot, ws))
        wss.on("wallet_update", lambda w: self._safe(self._on_wallet, w))
        wss.on("trade_execution", lambda t: self._safe(self._on_fill, t))
        wss.on("trade_execution_update", lambda t: self._safe(self._on_fill, t))
        wss.on("on-req-notification", lambda n: self._safe(self._on_req_notification, n))
        wss.on("oc-req-notification", lambda n: self._safe(self._on_req_notification, n))

    def _safe(self, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:                # a bad event never kills the loop
            log.error("WS handler %s failed: %s", getattr(fn, "__name__", fn), exc)

    def stop(self) -> None:
        loop, thread, bfx = self._loop, self._thread, self._bfx
        if loop is None or thread is None:
            return
        async def _close():
            try:
                await bfx.wss.close()
            except Exception:
                pass
        try:
            fut = asyncio.run_coroutine_threadsafe(_close(), loop)
            fut.result(timeout=5)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        self._thread = None

    # ── order bridge (Task 4 adds submit/cancel + _on_req_notification) ──
    def _on_req_notification(self, notif):
        raise NotImplementedError  # implemented in Task 4
