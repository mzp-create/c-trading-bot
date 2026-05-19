#!/usr/bin/env python3
"""Dry run: fetch live data, detect regime, run strategies, report."""
import sys, yaml, os
sys.path.insert(0, os.path.dirname(__file__) or ".")
from main import load_config
from market_data.collector import MarketDataCollector
from analysis.technical import TechnicalAnalyzer
from analysis.ml_predictor import MLPredictor
from strategies.selector import StrategySelector
from risk.regime_detector import MarketRegimeDetector, MarketRegime
from risk.manager import RiskManager
from analysis.utils import flatten_ta

config = load_config("config/default.yaml")

symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

collector = MarketDataCollector(config)
analyzer = TechnicalAnalyzer(config)
ml = MLPredictor(config)
strategies = StrategySelector(config)
regime = MarketRegimeDetector(config)
risk = RiskManager(config)

print("DRY RUN — Market Analysis + Regime Detection")
print("=" * 50)

for sym in symbols:
    print(f"\n--- {sym} ---")

    df_1h = collector.get_ohlcv(sym, timeframe="1h", limit=200)
    df_5m = collector.get_ohlcv(sym, timeframe="5m", limit=200)

    if df_1h is None or df_5m is None:
        print("  No data available")
        continue
    # Price
    last_price = float(df_1h["close"].iloc[-1])
    change = (df_1h["close"].iloc[-1] - df_1h["close"].iloc[-2]) / df_1h["close"].iloc[-2] * 100
    print(f"  Price: ${last_price:.2f} ({change:+.2f}%)")

    r = regime.detect(df_1h)
    params = regime.get_adapted_params(r, 2.0, 4.0)
    emoji = {"TRENDING": "\U0001f4c8", "RANGING": "\U0001f4ca", "VOLATILE": "\U0001f32a\ufe0f"}.get(r.value, "?")
    print(f"  Regime: {emoji} {r.value}  SL={params['stop_loss_pct']}% TP={params['take_profit_pct']}% Size={params['position_size_mult']}x")

    ta_1h = analyzer.analyze(df_1h, "1h")
    ta_5m = analyzer.analyze(df_5m, "5m")

    # Normalize TA keys: flatten nested dicts into flat keys (shared utility)
    ta_1h_flat = flatten_ta(ta_1h)
    ta_5m_flat = flatten_ta(ta_5m)
    ml_signal = ml.predict(df_1h, symbol=sym)

    signals = strategies.get_signals(ta_1h_flat, ta_5m_flat, {}, ml_signal)
    print(f"  Strategies:")
    for sig in signals:
        name = sig.get("name", "?")
        s = sig.get("signal", "HOLD")
        c = sig.get("confidence", 0.0)
        rsn = sig.get("reason", "")
        em = "\U0001f7e2" if s == "BUY" else "\U0001f534" if s == "SELL" else "\u26aa"
        print(f"    {em} {name}: {s} (conf: {c:.2f})  {rsn[:60]}")

    print(f"  ML: {ml_signal.get('signal', 'HOLD')} (conf: {ml_signal.get('confidence', 0.0):.2f})")

    # Vote tally
    buy_score, sell_score, total_w = 0.0, 0.0, 0.0
    enabled = strategies.get_enabled_strategies()
    weights = {s["name"]: s.get("weight", 0.25) for s in enabled}
    for sig in signals:
        w = weights.get(sig.get("name"), 0.25)
        total_w += w
        if sig["signal"] == "BUY":
            buy_score += w * sig["confidence"]
        elif sig["signal"] == "SELL":
            sell_score += w * sig["confidence"]

    total_w += 0.3
    if ml_signal["signal"] == "BUY":
        buy_score += 0.3 * ml_signal["confidence"]
    elif ml_signal["signal"] == "SELL":
        sell_score += 0.3 * ml_signal["confidence"]

    if total_w > 0:
        buy_score /= total_w
        sell_score /= total_w

    pair_capital = config["trading"]["initial_capital"] / len(symbols)
    pos_size = risk.calculate_position_size(
        capital=pair_capital,
        price=last_price,
        confidence=max(buy_score, sell_score),
        signal_type="BUY" if buy_score > sell_score else "SELL",
        regime_position_mult=params["position_size_mult"],
    )

    threshold = 0.55
    if buy_score > sell_score and buy_score > threshold:
        verdict = "BUY"
    elif sell_score > buy_score and sell_score > threshold:
        verdict = "SELL"
    else:
        verdict = "HOLD"

    print()
    if verdict == "BUY":
        print(f"  => BUY  (conf: {buy_score:.2f})  size: {pos_size:.6f} (${pos_size * last_price:.2f})")
    elif verdict == "SELL":
        print(f"  => SELL (conf: {sell_score:.2f})  size: {pos_size:.6f}")
    else:
        print(f"  => HOLD  (buy: {buy_score:.2f} sell: {sell_score:.2f})")
