#!/usr/bin/env python3
"""Validate fast_sim against the real BacktestEngine baseline.

Runs fast_sim with parameters mirroring default.yaml on the FULL test window and
prints the metrics next to the engine's known baseline so we can confirm the
fast path reproduces the slow path (trade count, PF, return should match closely
— small diffs only from combine float rounding / leftover handling)."""
from backtest.fast_sim import load_cache, simulate, SimParams

cache = load_cache("backtest/data/precomp_default.pkl")
p = SimParams(
    confidence_threshold=0.20,
    regime_filter=None,
    sl_pct=2.0, tp_pct=4.0,
    trailing=True, trailing_activation=2.0, trailing_distance=0.5,
    use_regime_mult=True,
    max_open=6,
    max_risk_per_trade=0.02, leverage=2.0, max_single_pct=0.20,
    min_position_value_usd=15.0,
    split_lo=0.0, split_hi=1.0,
    max_daily_loss=16.0,   # match config/default.yaml risk.max_daily_loss exactly
)
res = simulate(cache, p)
print("FAST SIM (default params, full window, capital=%.1f, maxDailyLoss=16):"
      % cache["initial_capital"])
for k, v in res.items():
    print(f"  {k:16s} {v}")
print()
print("Compare against an engine run at the SAME settings (config/default.yaml,")
print("native capital 526.8, max_daily_loss 16). See engine_baseline_native.txt.")
print("return_pct, profit_factor, trades, win_rate should match within rounding.")
