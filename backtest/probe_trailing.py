"""R:R / exit-logic probe on the candidate_2pct config.

Diagnosis: candidate has win rate ~53% but avg_win < avg_loss (PF 0.72) — winners
are cut short. Prime suspect: tight trailing stop (0.5% distance after a 2% move)
caps winners well below the 6% TP while losers run to the full 3% SL. Test
trailing variants to see if letting winners run flips profit factor > 1.

    .venv/bin/python -m backtest.probe_trailing
"""
import copy
import json
import yaml

from backtest.engine import BacktestEngine

BASE = yaml.safe_load(open("config/candidate_2pct.yaml"))


def _set(c, **risk):
    c["risk"].update(risk)


VARIANTS = {
    "candidate (trail 2%/0.5%)": lambda c: None,
    "trailing_OFF (SL3/TP6)":    lambda c: _set(c, trailing_stop=False),
    "trail_wide (act2/dist2)":   lambda c: _set(c, trailing_stop=True, trailing_stop_activation=2.0, trailing_stop_distance=2.0),
    "trail_high (act4/dist1.5)": lambda c: _set(c, trailing_stop=True, trailing_stop_activation=4.0, trailing_stop_distance=1.5),
    "trailing_OFF TP8":          lambda c: (_set(c, trailing_stop=False, take_profit_pct=8.0)),
}

results = {}
for name, mut in VARIANTS.items():
    cfg = copy.deepcopy(BASE)
    mut(cfg)
    print(f"\n===== {name} =====", flush=True)
    try:
        rep = BacktestEngine(cfg).run()
        scal = {k: v for k, v in rep.items() if isinstance(v, (int, float, str, bool))}
        results[name] = scal
        print(f"[{name}] PF={scal.get('profit_factor'):.3f} ret={scal.get('total_return_pct'):.1f}% "
              f"win={scal.get('win_rate'):.0%} avgW={scal.get('avg_win')} avgL={scal.get('avg_loss')} "
              f"day%={scal.get('avg_daily_pct')}", flush=True)
    except Exception as e:
        results[name] = {"error": repr(e)}
        print(f"[{name}] ERROR {e!r}", flush=True)

json.dump(results, open("backtest/data/probe_trailing.json", "w"), indent=2)

print("\n\n========= TRAILING / R:R PROBE SUMMARY (candidate, full ~4.4mo) =========")
cols = ["profit_factor", "total_return_pct", "win_rate", "avg_win", "avg_loss",
        "max_drawdown_pct", "avg_daily_pct"]
print("variant".ljust(28), " ".join(c[:10].rjust(11) for c in cols))
for name, r in results.items():
    if "error" in r:
        print(name.ljust(28), "ERROR"); continue
    row = [(f"{r.get(c):.3f}" if isinstance(r.get(c), float) else str(r.get(c))).rjust(11) for c in cols]
    print(name.ljust(28), " ".join(row))
print("\n2%/month target = +0.066%/day. PF>1.0 AND positive day% = profitable.")
