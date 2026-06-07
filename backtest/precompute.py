#!/usr/bin/env python3
"""Precompute per-bar raw strategy/ML/regime components for fast config search.

WHY: TA + ML + per-strategy signals + regime depend ONLY on the trailing window
candles[: i+1] — NOT on SL/TP, confidence threshold, strategy weights, regime
entry filter, sizing, or max_open. So we compute the EXPENSIVE part ONCE per
(timeframe, EMA-config, ML on/off) and cache it. A separate fast portfolio
simulator (fast_sim.py) then sweeps the cheap levers in milliseconds.

NO-LOOKAHEAD: identical to BacktestEngine — at bar i we feed candles[: i+1] to
every analyzer, and the ML model is trained ONLY on the first `train_bars` of
each symbol's history (the prefix strictly before the test window). The cache
stores, per (symbol, bar): each enabled non-shadow strategy's (signal,conf,
weight), the ML (signal,conf,accuracy), the regime string, and the bar OHLC +
timestamp. The combine math is replicated faithfully in fast_sim.py.

Usage:
  python -m backtest.precompute --config config/default.yaml --out backtest/data/precomp_default.pkl
Optional overrides: --ema-fast --ema-slow --timeframe --train-bars --no-ml
"""
import argparse
import copy
import logging
import pickle
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")   # silence sklearn joblib chatter (huge log spam)

import pandas as pd

# Bounded analysis window. The LIVE bot only ever feeds limit=200 bars to the
# analyzers (main.py analyze_market: get_ohlcv(..., limit=200)). The slow
# BacktestEngine instead feeds the full growing prefix, which (a) is O(n^2) in
# time+memory over the replay and (b) is NOT what live does. We feed the last
# WINDOW_BARS bars: this MATCHES live behaviour, bounds per-bar cost to a
# constant, and preserves no-lookahead (we only ever drop the OLDEST bars, never
# future ones). 250 > EMA200 period so EMA200 is fully converged.
WINDOW_BARS = 250

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for noisy in ("TechnicalAnalyzer", "MLPredictor", "risk.manager.RiskManager",
              "risk.regime_detector.MarketRegimeDetector",
              "strategies.selector.StrategySelector"):
    logging.getLogger(noisy).setLevel(logging.ERROR)
log = logging.getLogger("precompute")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/default.yaml")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ema-fast", type=int)
    ap.add_argument("--ema-slow", type=int)
    ap.add_argument("--timeframe", default=None)
    ap.add_argument("--train-bars", type=int, default=None)
    ap.add_argument("--no-ml", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from main import load_config, TradingBot
    from backtest.engine import BacktestEngine

    cfg = load_config(args.config)
    if args.ema_fast:
        cfg.setdefault("strategies", {}).setdefault("trend_following", {})["ema_fast"] = args.ema_fast
    if args.ema_slow:
        cfg.setdefault("strategies", {}).setdefault("trend_following", {})["ema_slow"] = args.ema_slow
    if args.no_ml:
        cfg.setdefault("ml", {})["enabled"] = False

    # The ensemble strategy is shadow:true in all configs — it is logged but
    # NEVER affects the trade decision (see TradingBot._combine_signals: shadow
    # votes are skipped). Running it per-bar costs ~55% of precompute time for
    # zero effect on PnL, so we drop it from the precompute pipeline. (The fast
    # simulator only sums non-shadow votes anyway.)
    strat_cfg = cfg.setdefault("strategies", {})
    strat_cfg["enabled"] = [s for s in strat_cfg.get("enabled", []) if s != "ensemble"]
    if "ensemble" in strat_cfg:
        strat_cfg["ensemble"]["enabled"] = False
    if args.timeframe:
        cfg.setdefault("backtest", {})["timeframe"] = args.timeframe
    if args.train_bars:
        cfg.setdefault("backtest", {})["train_bars"] = args.train_bars

    eng = BacktestEngine(cfg)
    shim = eng._ComboShim(eng)

    # Load data + train ML per symbol (same as engine).
    data, test_start = {}, {}
    for s in eng.symbols:
        sym = s["name"]
        df = eng.fetch_history(sym, eng.timeframe, eng.history_bars)
        if df is None or len(df) < eng.train_bars + 100:
            log.warning("[%s] insufficient history — skipping", sym)
            continue
        data[sym] = df
        test_start[sym] = eng._train_ml_walk_forward(sym, df)

    enabled = [e for e in eng.strategies.get_enabled_strategies() if not e.get("shadow")]
    strat_names = [e["name"] for e in enabled]
    weights = {e["name"]: e["weight"] for e in enabled}
    log.info("Non-shadow strategies: %s weights=%s", strat_names, weights)

    out = {
        "symbols": {},
        "weights": weights,
        "strat_names": strat_names,
        "timeframe": eng.timeframe,
        "initial_capital": eng.initial_capital,
        "allocations": {s["name"]: s.get("allocation_pct", 100.0) for s in eng.symbols},
    }

    from main import TradingBot
    for sym, df in data.items():
        rows = []
        start = test_start[sym]
        n = len(df)
        log.info("[%s] precomputing bars %d..%d", sym, start, n)
        for i in range(start, n):
            if i < 50:
                continue
            # Bounded trailing window (matches live limit=200; no-lookahead: only
            # drops oldest bars). regime gets the same bounded window.
            lo = max(0, i + 1 - WINDOW_BARS)
            window = df.iloc[lo: i + 1]
            w = window.copy()
            w.index = pd.to_datetime(w.index, unit="ms")

            ta_1h_raw = eng.analyzer.analyze(w, "1h")
            ta_5m_raw = eng.analyzer.analyze(w, "5m")
            ta_15m_raw = eng.analyzer.analyze(w, "15m")
            flatten = TradingBot._flatten_ta
            ta_1h = flatten(shim, ta_1h_raw)
            ta_5m = flatten(shim, ta_5m_raw)
            ta_15m = flatten(shim, ta_15m_raw)

            ml_signal = eng.ml_predictor.predict(w, symbol=sym)
            ss = eng.strategies.get_signals(ta_1h, ta_5m, ta_15m, ml_signal,
                                            symbol=sym, df_1h=w)
            # Keep only non-shadow strategy votes (signal, conf, weight).
            strat_votes = []
            for sig in ss:
                if sig.get("shadow"):
                    continue
                strat_votes.append((sig["name"], sig.get("signal", "HOLD"),
                                    float(sig.get("confidence", 0.0) or 0.0)))
            ml_acc = eng.ml_predictor._training_accuracies.get(sym, 0.55)

            # Regime (same call the engine uses for SL/TP adaptation).
            regime_str = "RANGING"
            try:
                if len(window) > 30:
                    regime_str = eng.regime_detector.detect(window.copy()).value
            except Exception:
                pass

            bar = df.iloc[i]
            rows.append({
                "ts": int(df.index[i]),
                "i": i,
                "close": float(bar["close"]),
                "high": float(bar["high"]),
                "low": float(bar["low"]),
                "strat_votes": strat_votes,
                "ml_signal": ml_signal.get("signal", "HOLD"),
                "ml_conf": float(ml_signal.get("confidence", 0.0) or 0.0),
                "ml_acc": float(ml_acc),
                "regime": regime_str,
            })
        # also store the full OHLC arrays for exit simulation on every bar
        out["symbols"][sym] = {
            "rows": rows,
            "ohlc": df[["high", "low", "close"]].reset_index().values.tolist(),
            "index_map": {int(ts): k for k, ts in enumerate(df.index)},
        }
        log.info("[%s] %d decision bars cached", sym, len(rows))

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "wb") as f:
        pickle.dump(out, f)
    log.info("Wrote %s", args.out)


if __name__ == "__main__":
    main()
