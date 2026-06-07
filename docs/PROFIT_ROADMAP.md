# Profit Roadmap — how to actually reach edge (2026-06-07)

The safety/engineering half of the goal is done. The profit half is blocked by one
thing: **the current strategy has no positive expectancy.** This is the action plan
to fix *that* — the only path to real, repeatable profit. It is research, not
tuning; outcomes are uncertain. Honest target: aim for **positive expectancy →
~2%/month**, not "$100/day" (≈2%/day is not realistic for this asset class).

## What's already proven (don't re-do)
- Trend (EMA/RSI), mean-reversion (RSI/BB), and ~9,300 parameter configs: **all lose
  or break even**. Best tuned result ≈ PF 1.02 (statistical noise). Exit/R:R tuning
  reshuffles but doesn't create edge.
- Tooling exists and is reusable: `backtest/engine.py` (faithful, no-lookahead),
  `backtest/search.py` + `fast_sim.py` + `precompute.py` (IS/OOS sweep),
  `backtest/probe*.py`, `alpha_meanrev.py`, `alpha_breakout.py` (parked).

## Root limitations to fix first
1. **Data:** only ~4.4 months of 1h history, and it's a single regime (a crash).
   Any edge finding is unreliable. **Fetch multi-year OHLCV across bull/bear/chop**
   before trusting any backtest. This alone may change conclusions.
2. **Validation discipline:** walk-forward + out-of-sample across *different* regimes;
   realistic fees + funding/borrow on margin; reject anything that's only IS-positive.

## Candidate alpha families (test rigorously, IS/OOS, in rough priority)
1. **Market-neutral funding/basis** — long spot vs short perp to harvest funding, or
   basis carry. This is the *most realistic* path to steady small returns (~2%/month
   class) and is largely direction-agnostic, unlike trend-following. Needs funding-rate
   + perp data. **Highest expected value.**
2. **Cross-sectional momentum** — rank the 3+ assets, long strongest / short weakest;
   captures relative strength, less exposed to whole-market beta.
3. **Breakout/volatility-targeting** — Donchian breakouts with vol-expansion filter
   (`alpha_breakout.py` drafted) + position sizing inversely to volatility.
4. **Regime-conditional ML** — train separate models per regime on *better* features
   (order-flow, funding, cross-asset), not just lagged TA; the current XGBoost on
   lagged indicators showed ~0.6 accuracy = no real signal.
5. **Microstructure / order-book** (if data available) — short-horizon imbalance.

## Process (each candidate)
1. Implement as an isolated signal; backtest on multi-year data, IS/OOS, costs modeled.
2. Promote only if **positive OOS across ≥2 distinct regimes**, PF comfortably > 1
   (not ~1.0 noise), acceptable drawdown.
3. Size with fractional-Kelly / vol-targeting once edge is established.
4. Forward paper-trade the promoted strategy; gate live capital on sustained paper
   results; ramp capital in steps ($250 → $500 → …), never 0 → full.

## Residual safety items before scaling real capital (independent of edge)
- **Exchange-side resting stops** (currently bot-side only — a dead process leaves
  leveraged positions unprotected). Highest-priority safety gap.
- **Reload daily realized PnL on restart** (breaker currently re-arms after a crash).
- **Persistent kill-switch.**

## Bottom line
"$100/day on $5k" is not attainable with this or any simple rule-based crypto
strategy. A realistic, achievable aim is **positive expectancy → ~1–2%/month**, and
reaching even that requires the research above — with no guarantee. The engineering,
safety, and measurement foundation is in place to pursue it honestly when resumed.
