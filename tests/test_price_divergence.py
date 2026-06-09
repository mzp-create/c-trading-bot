"""Regression tests for the OHLCV/ticker price-divergence entry guard
(TradingBot._price_diverges_from_live) — the 2026-06-05 stale-price incident."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import main as main_mod


class _Collector:
    """get_current_price returns a fixed value, None, or raises if given an
    Exception instance."""

    def __init__(self, price):
        self._price = price

    def get_current_price(self, symbol):
        if isinstance(self._price, Exception):
            raise self._price
        return self._price


class _Log:
    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass


def _bot(live_price):
    # Build a bare TradingBot without running its heavy __init__.
    bot = object.__new__(main_mod.TradingBot)
    bot.collector = _Collector(live_price)
    bot.log = _Log()
    return bot


def test_aborts_when_live_far_from_entry():
    # live 110 vs entry 100 = ~9.1% > 2% tolerance -> diverges
    assert _bot(110.0)._price_diverges_from_live("BTC/USDT", 100.0) is True


def test_ok_within_tolerance():
    # live 101 vs entry 100 = 1% < 2% -> fine
    assert _bot(101.0)._price_diverges_from_live("BTC/USDT", 100.0) is False


def test_fails_closed_when_ticker_unavailable():
    # No live price -> fail CLOSED on entry (block) so we never open against a
    # possibly-stale OHLCV price. Returns True (= diverges/blocked), no crash.
    assert _bot(None)._price_diverges_from_live("BTC/USDT", 100.0) is True


def test_fails_closed_on_ticker_exception():
    assert _bot(RuntimeError("boom"))._price_diverges_from_live("BTC/USDT", 100.0) is True


def test_tolerance_boundary_just_over():
    # Just over 2% (live such that gap > tol) -> diverges.
    # entry 100, live 97.5 -> |97.5-100|/97.5 = 2.56% > 2%
    assert _bot(97.5)._price_diverges_from_live("BTC/USDT", 100.0) is True


# --- Entry-anchor: SL/TP must be anchored to the LIVE ticker, not the stale
# OHLCV candle close (the 2026-06-09 order #9 bug: candle close lagged the live
# market by 1.8% during a fast move, so the recorded entry — and the SL/TP band
# derived from it — sat off-centre vs the live price the stop monitor checks). ---


def test_entry_anchor_uses_live_price_within_tolerance():
    # OHLCV close lags at 66.857; live ticker 65.65 (~1.8%, within 2% tol).
    # The anchor used for the order/SL/TP MUST be the live price, not the candle.
    assert _bot(65.65)._entry_anchor_price("SOL/USDT", 66.857) == 65.65


def test_entry_anchor_blocks_on_divergence():
    # >2% apart -> no safe anchor -> block entry (None).
    assert _bot(110.0)._entry_anchor_price("BTC/USDT", 100.0) is None


def test_entry_anchor_blocks_when_ticker_unavailable():
    # No live ticker -> cannot anchor safely -> block (None), no crash.
    assert _bot(None)._entry_anchor_price("BTC/USDT", 100.0) is None


def test_entry_anchor_blocks_on_ticker_exception():
    assert _bot(RuntimeError("boom"))._entry_anchor_price("BTC/USDT", 100.0) is None
