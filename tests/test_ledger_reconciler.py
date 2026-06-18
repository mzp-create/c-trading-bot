"""Tests for the ledger reconciler orchestration (fetch -> persist -> backfill).
Uses a stub REST client and a fake repo so no network/DB is touched."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bitfinex.ledger import LedgerEntry, POSITION_CLOSE, TRADING_FEE
from execution.ledger_reconciler import reconcile


class _StubRest:
    def __init__(self, by_currency):
        self._by = by_currency
        self.calls = []

    def get_ledgers(self, currency, start=None, limit=2500):
        self.calls.append((currency, start))
        return list(self._by.get(currency, []))


class _FakeRepo:
    def __init__(self, recorded_closes=()):
        self.instance = "long"
        self.mode = "live"
        self.upserted = []
        self.backfilled = []
        # set of (symbol) we pretend already have a recorded closing trade
        self._recorded = set(recorded_closes)
        self._seen_ids = set()

    def upsert_ledger_entry(self, entry, *, ts):
        new = entry.id not in self._seen_ids
        self._seen_ids.add(entry.id)
        self.upserted.append(entry)
        return new

    def last_ledger_mts(self):
        return 0

    def close_recorded_near(self, symbol, close_price, mts, **k):
        return symbol in self._recorded

    def record_trade(self, trade):
        self.backfilled.append(trade)
        return len(self.backfilled)

    def ledger_summary(self):
        closes = [e for e in self.upserted if e.kind == POSITION_CLOSE]
        fees = [e for e in self.upserted if e.kind == TRADING_FEE]
        return {"realized_pnl": sum(e.amount for e in closes),
                "fees": sum(e.amount for e in fees), "funding": 0.0,
                "net_pnl": sum(e.amount for e in closes) + sum(e.amount for e in fees),
                "current_balance": 519.0,
                "n_closes": len(closes), "n_fees": len(fees)}


def _entries():
    return [
        LedgerEntry(1, 100, "UST", +0.45, 520.7, "Position closed @ 74.1", POSITION_CLOSE, 74.1),
        LedgerEntry(2, 200, "UST", -0.52, 520.2, "Position closed @ 1747.9", POSITION_CLOSE, 1747.9),
        LedgerEntry(3, 300, "UST", -0.07, 520.1, "Margin Funding Charge", "funding", None),
    ]


REFS = {"BTC/USDT": 65000.0, "ETH/USDT": 1750.0, "SOL/USDT": 73.0}


def test_reconcile_persists_and_attributes_symbol():
    rest = _StubRest({"UST": _entries()})
    repo = _FakeRepo()
    summary = reconcile(rest, repo, currencies=["UST"], symbol_refs=REFS,
                        backfill=False)
    # all entries upserted
    assert len(repo.upserted) == 3
    # symbol attributed by price
    by_id = {e.id: e for e in repo.upserted}
    assert by_id[1].symbol == "SOL/USDT"
    assert by_id[2].symbol == "ETH/USDT"
    assert round(summary["realized_pnl"], 2) == -0.07  # 0.45 - 0.52
    assert summary["new_entries"] == 3


def test_reconcile_backfills_only_unrecorded_closes():
    rest = _StubRest({"UST": _entries()})
    # pretend the SOL close was already recorded by the bot, ETH was NOT
    repo = _FakeRepo(recorded_closes={"SOL/USDT"})
    reconcile(rest, repo, currencies=["UST"], symbol_refs=REFS, backfill=True)
    # only the ETH close (unrecorded) is backfilled into the trade log
    assert len(repo.backfilled) == 1
    t = repo.backfilled[0]
    assert t.symbol == "ETH/USDT"
    assert t.pnl == -0.52
    assert "reconcile" in t.reason


def test_reconcile_no_backfill_when_symbol_unknown():
    # a close whose price matches nothing -> cannot attribute -> never backfilled
    entries = [LedgerEntry(9, 100, "UST", -1.0, 500.0, "Position closed @ 5.0",
                           POSITION_CLOSE, 5.0)]
    rest = _StubRest({"UST": entries})
    repo = _FakeRepo()
    reconcile(rest, repo, currencies=["UST"], symbol_refs=REFS, backfill=True)
    assert repo.backfilled == []


def test_client_get_ledgers_paper_returns_empty():
    from bitfinex import BitfinexClient
    cfg = {"trading": {"initial_capital": 100.0}, "exchange": {"rate_limit": 0.0}}
    client = BitfinexClient(cfg, mode="paper")
    assert client.get_ledgers("UST") == []


def test_reconcile_real_repo_idempotent(tmp_path):
    """End-to-end against a real sqlite repo: upsert is idempotent on ledger id
    and the summary reflects the authoritative ledger totals."""
    from persistence.repository import TradingRepository
    repo = TradingRepository(str(tmp_path / "t.db"), instance="long", mode="live")
    rest = _StubRest({"UST": _entries()})

    s1 = reconcile(rest, repo, currencies=["UST"], symbol_refs=REFS, backfill=False)
    assert s1["new_entries"] == 3
    assert round(s1["realized_pnl"], 2) == -0.07
    assert s1["current_balance"] == 520.1  # latest mts entry's balance

    # Re-running must not double-count (idempotent on exchange ledger id).
    s2 = reconcile(rest, repo, currencies=["UST"], symbol_refs=REFS, backfill=False)
    assert s2["new_entries"] == 0
    assert round(s2["realized_pnl"], 2) == -0.07
