"""Ledger reconciler — make the exchange ledger the source of truth for
realized P&L and fees, and backfill exchange-side closes the bot never recorded.

Why this exists (2026-06-15/18 investigation): on Bitfinex margin the per-trade
``fee`` is 0 and realized P&L is booked as wallet-ledger entries. Exchange-side
closes (a catastrophe stop firing, a phantom position reconciled away) never
reach the bot's own close path, so the trade log under-counted losses and
overstated P&L (~4x: bot +$11.86 vs exchange +$2.79). This pulls the ledger,
persists it idempotently, and (optionally) backfills missed closing trades.

``reconcile`` takes the REST client and repo as params so it unit-tests with
stubs (no network/DB).
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from bitfinex import ledger
from persistence.models import TradeRecord

log = logging.getLogger(__name__)


def _iso(mts: int) -> str:
    return datetime.fromtimestamp(mts / 1000, tz=timezone.utc).isoformat()


def reconcile(rest, repo, *, currencies: Optional[List[str]] = None,
              since_mts: Optional[int] = None,
              symbol_refs: Optional[Dict[str, float]] = None,
              backfill: bool = True) -> dict:
    """Fetch ledger entries, persist them (idempotent), optionally backfill
    missed closes into the trade log, and return the authoritative summary.

    Parameters
    ----------
    rest : object with get_ledgers(currency, start, limit) -> List[LedgerEntry]
    repo : TradingRepository (or compatible)
    currencies : ledger currencies to pull (default ["UST", "USD"])
    since_mts : only process entries newer than this (default: repo watermark)
    symbol_refs : {symbol: reference_price} for attributing a close to a symbol
    backfill : insert reconciled trades for unrecorded exchange-side closes
    """
    currencies = currencies or ["UST", "USD"]
    symbol_refs = symbol_refs or {}
    if since_mts is None:
        since_mts = repo.last_ledger_mts()

    new_entries = 0
    backfilled = 0
    for cur in currencies:
        try:
            entries = rest.get_ledgers(cur, start=since_mts or None)
        except Exception as exc:
            log.warning("Ledger fetch failed for %s: %s", cur, exc)
            continue
        for e in entries:
            if e.kind == ledger.POSITION_CLOSE and e.symbol is None:
                e.symbol = ledger.symbol_from_price(e.price, symbol_refs)
            is_new = repo.upsert_ledger_entry(e, ts=_iso(e.mts))
            if is_new:
                new_entries += 1
            if (backfill and e.kind == ledger.POSITION_CLOSE
                    and e.symbol is not None):
                if not repo.close_recorded_near(e.symbol, e.price, e.mts):
                    repo.record_trade(TradeRecord(
                        ts=_iso(e.mts), symbol=e.symbol, side="",
                        entry_price=0.0, close_price=e.price or 0.0,
                        amount=0.0, pnl=e.amount, fee=0.0,
                        reason="exchange_close_reconciled"))
                    backfilled += 1
                    log.info("Backfilled missed %s close @ %s pnl=%.4f",
                             e.symbol, e.price, e.amount)

    summary = repo.ledger_summary()
    summary["new_entries"] = new_entries
    summary["backfilled"] = backfilled
    return summary
