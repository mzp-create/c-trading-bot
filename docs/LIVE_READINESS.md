# Live Readiness & Profit Assessment (2026-06-07)

Goal evaluated: *"ready for live trades with safety guards, achieving $100/day on up to $5,000."*

## TL;DR
- **Safety: substantially improved and tested** — live order/close path validated, multiple critical guard bugs fixed.
- **Profit goal ($100/day = 2%/day): NOT attainable with the current strategy.** A 4.4-month backtest on real Bitfinex history shows **negative edge** (−28% return, profit factor 0.59, ~−0.2%/day).
- **Recommendation: do NOT deploy real capital yet.** Run forward **paper** trading; target break-even/small-positive *first*. If/when paper shows positive expectancy, ramp capital gradually (e.g. $250 → $500 → … ), never 0 → $5,000.

## Edge: measured, not guessed
Built the previously-missing backtest engine (`backtest/engine.py`, run via `python main.py --mode backtest --config config/live_5k.yaml`). It replays the **live decision code** bar-by-bar with no lookahead, fees (0.1%/side) + slippage, real SL/TP/trailing, and risk caps.

| Metric (live_5k, $5k, ~4.4 mo) | Value |
|---|---|
| Total return | −28.1% |
| Profit factor | 0.59 |
| Win rate | 41% |
| Max drawdown | 28% |
| Avg daily PnL | −$10.55 (−0.21%/day) |
| Exit mix | 334 stop-losses vs 38 take-profits |

The strategy (EMA9/21 + RSI trend-follow + XGBoost weighting) gets whipsawed: ~9× more stop-outs than targets. ML accuracy is modest (BTC 0.79, SOL 0.60). **This is a no-edge problem, not a config-tuning problem.** Caveat: one market regime / ~4 months; directionally damning, not a multi-year proof.

### Edge probe — every lever still loses (confirms no edge)
`backtest/probe.py` tested the most promising levers (BTC, ~4.4mo, in-sample):

| Variant | Profit factor | Return | Daily PnL |
|---|---|---|---|
| baseline | 0.36 | −39% | −$14.8 |
| trending-only (regime filter) | 0.49 | −21% | −$7.8 |
| quick-profit (TP<SL) | 0.54 | −27% | −$10.2 |
| slow EMA 21/55 | 0.36 | −39% | (no effect) |

Best lever (trending-only) only *halves* the loss; **all profit factors < 1.0 even in-sample** (the most favorable case). A strategy that can't be profitable in-sample has no extractable edge — it needs **new signal logic**, not tuning. The `backtest.trending_only` flag is real/reusable (cuts losses ~half) but does not create profit.

### Decision (2026-06-07): accept realistic outcome
Owner chose to treat the safety hardening + backtest tooling + honest assessment as the deliverable. **No real capital deployed.** Extended paper run continues as forward evidence. The $100/day target is shelved as not attainable with this strategy; revisiting profit requires a separate new-strategy research effort.

## Safety guards — fixed & tested (154 tests green)
- **Trailing-stop ratchet bug** fixed (stop no longer loosens on a retrace).
- **`max_open_positions`** now actually enforced (was dead config) + no double-position per symbol.
- **Consecutive-loss breaker + cooldown** wired up (was dead code).
- **Daily-loss breaker force-flattens + pauses** on breach (was entry-block only); clears on daily reset.
- **Divergence guard fails closed** on entry when the live ticker is unavailable.
- **ML loading fixed** (instance-scoped model paths + enough training data — models now train).
- Previously validated live: reduceOnly close path, per-instance key isolation, graceful shutdown, stale-candle guard, stop_dual graceful grace period.

## Residual gaps before real capital (NOT yet done)
- **No exchange-side protective stops** — all SL/TP are bot-side; if the process dies, leveraged positions are unprotected. (Biggest structural risk for live margin. Add resting reduceOnly stops or a stall watchdog.)
- **`daily_pnl` resets on restart** — a mid-day crash re-arms the daily-loss budget (reload today's realized PnL from DB on startup).
- **No persistent kill-switch** — `/pause` is in-memory only.
- **Strategy edge** — the core blocker; needs real R&D (signals/features/validation), not tuning.

## Current status
- Extended **paper** run at $5k is live (`instances/live5k/`, pid in `instances/live5k/paper.pid`) gathering forward results. Telegram off. Monitor: `instances/live5k/logs/paper_extended.log`, DB `instances/live5k/data/trading.paper.db`, or `scripts/daily_report.py`.
- Stop it with: `kill $(cat instances/live5k/paper.pid)`.

## Realistic path forward
1. Let paper run accumulate (days–weeks); evaluate forward PnL/PF/drawdown vs the backtest.
2. Treat **positive expectancy in paper** as the gate — not $100/day.
3. Only after that, ramp real capital in small steps with the safety guards above completed.
4. To pursue real profit, the signal logic itself needs research (the backtest engine now makes this measurable).
