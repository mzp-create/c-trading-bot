"""Ledger-based P&L / fee reconciliation.

On Bitfinex margin/derivatives the per-trade ``fee`` field is 0 and realized
P&L is booked as wallet-ledger entries ("Position closed @ X"), not on the
trade or order objects. Exchange-side closes (a catastrophe stop firing, or a
phantom position reconciled away) never reach the bot's own close path, so the
bot's trade log under-counts losses and overstates P&L.

The ledger is therefore the source of truth:
  * "Position closed @ <price> (TRADE)"  -> realized P&L  (``amount``)
  * "Trading fees for <...> (<pct>%)"    -> fees          (``amount``, negative)
  * ``balance`` on each entry            -> true wallet equity after the entry

These functions are pure (no network/DB) so they unit-test directly; the
fetch+persist orchestration lives in execution/ledger_reconciler.py.
"""
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

POSITION_CLOSE = "position_close"
TRADING_FEE = "trading_fee"
FUNDING = "funding"
SETTLEMENT = "settlement"
OTHER = "other"

_PRICE_RE = re.compile(r"@\s*([0-9][0-9,]*\.?[0-9]*)")


def classify(description: str) -> str:
    d = (description or "").lower()
    if "position closed" in d:
        return POSITION_CLOSE
    if "trading fee" in d:
        return TRADING_FEE
    if "funding" in d:
        return FUNDING
    if "settlement" in d:
        return SETTLEMENT
    return OTHER


def parse_price(description: str) -> Optional[float]:
    """Extract the price after '@' from a ledger description (None if absent)."""
    m = _PRICE_RE.search(description or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def symbol_from_price(price: Optional[float], refs: Dict[str, float],
                      tol: float = 0.25) -> Optional[str]:
    """Best-effort attribute a close price to a symbol by nearest reference
    price within ``tol`` (fractional distance). Returns None if nothing is close
    enough — we never wild-guess. Reliable here because BTC/ETH/SOL price ranges
    don't overlap."""
    if price is None or not refs:
        return None
    best, best_ratio = None, tol
    for sym, ref in refs.items():
        if not ref:
            continue
        ratio = abs(price - ref) / ref
        if ratio < best_ratio:
            best, best_ratio = sym, ratio
    return best


@dataclass
class LedgerEntry:
    id: int
    mts: int
    currency: Optional[str]
    amount: float
    balance: Optional[float]
    description: str
    kind: str
    price: Optional[float]
    symbol: Optional[str] = None


def from_raw(o: Any) -> LedgerEntry:
    """Build a LedgerEntry from a bfxapi ledger object (duck-typed attrs)."""
    desc = getattr(o, "description", "") or ""
    bal = getattr(o, "balance", None)
    return LedgerEntry(
        id=int(getattr(o, "id")),
        mts=int(getattr(o, "mts", 0) or 0),
        currency=getattr(o, "currency", None),
        amount=float(getattr(o, "amount", 0.0) or 0.0),
        balance=(float(bal) if bal is not None else None),
        description=desc,
        kind=classify(desc),
        price=parse_price(desc),
    )


def summarize(entries: List[LedgerEntry]) -> Dict[str, Any]:
    """Authoritative totals from ledger entries: realized P&L, fees, funding,
    net, and the true wallet balance (from the latest entry that carries one)."""
    closes = [e for e in entries if e.kind == POSITION_CLOSE]
    fees = [e for e in entries if e.kind == TRADING_FEE]
    funding = [e for e in entries if e.kind == FUNDING]
    realized = sum(e.amount for e in closes)
    fee_total = sum(e.amount for e in fees)
    funding_total = sum(e.amount for e in funding)
    balance = None
    with_bal = [e for e in entries if e.balance is not None]
    if with_bal:
        balance = max(with_bal, key=lambda e: e.mts).balance
    return {
        "realized_pnl": realized,
        "fees": fee_total,
        "funding": funding_total,
        "net_pnl": realized + fee_total + funding_total,
        "current_balance": balance,
        "n_closes": len(closes),
        "n_fees": len(fees),
    }
