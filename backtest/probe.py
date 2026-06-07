"""Quick edge probe: backtest a handful of strategy variants and compare.
BTC-only (for speed). Honest, in-sample over the cached ~4.4mo history (NOT a
full OOS sweep) — a fast directional read on whether any lever flips edge positive.

    .venv/bin/python -m backtest.probe
"""
import copy
import json
import yaml

from backtest.engine import BacktestEngine

BASE = yaml.safe_load(open("config/live_5k.yaml"))
# BTC-only for speed + signal clarity.
BASE["trading"]["symbols"] = [{"name": "BTC/USDT", "symbol": "tBTCUST",
                               "allocation_pct": 100.0, "enabled": True}]


def _slow_ema(c):
    c["strategies"]["trend_following"]["ema_fast"] = 21
    c["strategies"]["trend_following"]["ema_slow"] = 55


def _quick_profit(c):
    c["risk"]["stop_loss_pct"] = 2.0
    c["risk"]["take_profit_pct"] = 1.5


def _trending(c):
    c.setdefault("backtest", {})["trending_only"] = True


VARIANTS = {
    "baseline":            lambda c: None,
    "trending_only":       _trending,
    "slow_ema_21_55":      _slow_ema,
    "quick_profit_tp1.5":  _quick_profit,
    "trending+slow_ema":   lambda c: (_slow_ema(c), _trending(c)),
}

results = {}
for name, mut in VARIANTS.items():
    cfg = copy.deepcopy(BASE)
    mut(cfg)
    print(f"\n===== RUNNING VARIANT: {name} =====", flush=True)
    try:
        rep = BacktestEngine(cfg).run()
        # keep only scalar summary fields
        scal = {k: v for k, v in rep.items()
                if isinstance(v, (int, float, str, bool))}
        results[name] = scal
        print(f"[{name}] {json.dumps(scal)}", flush=True)
    except Exception as e:
        results[name] = {"error": repr(e)}
        print(f"[{name}] ERROR {e!r}", flush=True)

json.dump(results, open("backtest/data/probe_results.json", "w"), indent=2)

print("\n\n================ PROBE SUMMARY (BTC-only, in-sample) ================", flush=True)
cols = ["total_return_pct", "profit_factor", "win_rate", "num_trades",
        "max_drawdown_pct", "avg_daily_pnl"]
print("variant".ljust(22), " ".join(c.rjust(14) for c in cols))
for name, r in results.items():
    if "error" in r:
        print(name.ljust(22), "ERROR:", r["error"][:60]); continue
    row = []
    for c in cols:
        v = r.get(c, "")
        row.append((f"{v:.2f}" if isinstance(v, float) else str(v)).rjust(14))
    print(name.ljust(22), " ".join(row))
print("\nNOTE: in-sample over ~4.4mo BTC; directional read only, overfitting risk.")
