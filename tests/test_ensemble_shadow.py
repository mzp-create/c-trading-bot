"""Tests for the shadow-mode ensemble wiring (Phase C):
- shadow signals never affect the trade decision
- the ensemble receives the real 1h OHLCV and its non-entry actions normalize
  to HOLD.
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

import main as main_mod
import strategies.ensemble_bot_integration as ebi
import strategies.selector as selector_mod


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _Strats:
    def __init__(self, enabled):
        self._enabled = enabled

    def get_enabled_strategies(self):
        return self._enabled


class _MLP:
    _training_accuracies = {}


def _bot(enabled):
    b = object.__new__(main_mod.TradingBot)
    b.strategies = _Strats(enabled)
    b.ml_predictor = _MLP()
    b.log = _Log()
    b.config = {}          # sentiment disabled -> stays out of the BUY path
    b.daily_pnl = 0.0
    return b


def _df(n=60):
    return pd.DataFrame({c: [1.0] * n for c in
                         ("open", "high", "low", "close", "volume")})


# ── shadow exclusion ─────────────────────────────────────────────────────────

def test_shadow_signal_excluded_from_decision():
    enabled = [
        {"name": "trend_following", "weight": 0.4, "shadow": False, "params": {}},
        {"name": "ensemble", "weight": 0.6, "shadow": True, "params": {}},
    ]
    sigs = [
        {"name": "trend_following", "signal": "HOLD", "confidence": 0.0, "shadow": False},
        {"name": "ensemble", "signal": "BUY", "confidence": 0.99, "shadow": True},
    ]
    ml = {"signal": "HOLD", "confidence": 0.0, "symbol": ""}
    final = _bot(enabled)._combine_signals(sigs, ml, {"current_price": 100}, symbol="BTC/USDT")
    # A high-confidence SHADOW BUY must not produce a BUY decision.
    assert final["signal"] == "HOLD"


def test_same_signal_non_shadow_drives_decision():
    """Control: the identical BUY, non-shadow, *does* drive a BUY — proving the
    shadow flag (not something else) is what suppressed the decision above."""
    enabled = [{"name": "trend_following", "weight": 0.6, "shadow": False, "params": {}}]
    sigs = [{"name": "trend_following", "signal": "BUY", "confidence": 0.99, "shadow": False}]
    ml = {"signal": "HOLD", "confidence": 0.0, "symbol": ""}
    final = _bot(enabled)._combine_signals(sigs, ml, {"current_price": 100}, symbol="BTC/USDT")
    assert final["signal"] == "BUY"


# ── ensemble.generate wiring / normalization ─────────────────────────────────

def test_generate_uses_real_df_and_normalizes_close_to_hold():
    strat = object.__new__(ebi.EnsembleBotStrategy)
    strat.symbol = "BTC/USDT"
    strat.log = _Log()
    captured = {}

    def fake_analyze(current_price, position, market_data):
        captured["rows"] = len(market_data)
        return {"signal": "CLOSE", "confidence": 0.7, "reason": "x", "metadata": {}}

    strat.analyze = fake_analyze
    out = strat.generate({"close": 100}, {}, {}, {}, df_1h=_df(60))
    assert captured["rows"] == 60          # real df used, not the 1-row stub
    assert out["signal"] == "HOLD"         # CLOSE (an exit) normalized to HOLD


def test_generate_passes_buy_through():
    strat = object.__new__(ebi.EnsembleBotStrategy)
    strat.symbol = "BTC/USDT"
    strat.log = _Log()
    strat.analyze = lambda current_price, position, market_data: {
        "signal": "BUY", "confidence": 0.6, "reason": "x", "metadata": {}}
    out = strat.generate({"close": 100}, {}, {}, {}, df_1h=_df(60))
    assert out["signal"] == "BUY" and out["confidence"] == 0.6


# ── selector threads df_1h to the per-symbol ensemble ────────────────────────

def test_get_signals_threads_df_to_ensemble(monkeypatch):
    cfg = {"strategies": {"enabled": ["ensemble"],
                          "ensemble": {"enabled": True, "shadow": True}},
           "trading": {"symbols": [{"name": "BTC/USDT"}]}}
    sel = selector_mod.StrategySelector(cfg, trade_direction="both")
    captured = {}

    class _FakeEns:
        def generate(self, ta_1h, ta_5m, ta_15m, ml_signal, df_1h=None):
            captured["df_1h"] = df_1h
            return {"signal": "BUY", "confidence": 0.5, "reason": "x", "params": {}}

    monkeypatch.setattr(sel, "_get_ensemble", lambda symbol: _FakeEns())
    df = _df(60)
    sigs = sel.get_signals({"close": 1}, {}, {}, {"signal": "HOLD"},
                           symbol="BTC/USDT", df_1h=df)
    assert captured["df_1h"] is df
    ens = [s for s in sigs if s["name"] == "ensemble"][0]
    assert ens["shadow"] is True
