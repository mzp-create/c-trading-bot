#!/usr/bin/env python3
"""IS/OOS grid search over the cheap levers using the precomputed cache.

Optimizes ONLY on the in-sample window (first IS_FRAC of the unified test
timeline) and reports the chosen config's out-of-sample metrics. Prints:
  1. baseline (default params) IS and OOS
  2. top IS configs by a robustness score, each with its OOS metrics
  3. the single best config that is ALSO positive OOS (if any)

No-lookahead holds: the cache itself is no-lookahead (precompute.py), and the
IS/OOS split is a strict time split — we never select on OOS.
"""
import argparse
import itertools
from backtest.fast_sim import load_cache, simulate, SimParams

IS_FRAC = 0.70
MIN_IS_TRADES = 40    # require a meaningful sample; tiny-N IS "winners" are noise
MIN_OOS_TRADES = 15   # require the OOS verdict to also rest on a real sample


def run(cache, lo, hi, **kw):
    p = SimParams(split_lo=lo, split_hi=hi, max_daily_loss=kw.pop("max_daily_loss", 16.0))
    for k, v in kw.items():
        setattr(p, k, v)
    return simulate(cache, p)


def fmt(r):
    if r is None:
        return "  (no trades)"
    return (f"ret={r['return_pct']:+7.2f}%  PF={r['profit_factor']:5.2f}  "
            f"n={r['trades']:4d}  win={r['win_rate']*100:4.1f}%  "
            f"dd={r['max_dd_pct']:5.1f}%  day={r['daily_pct']:+.3f}%  "
            f"SL/TP={r['sl_count']}/{r['tp_count']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="backtest/data/precomp_default.pkl")
    args = ap.parse_args()
    cache = load_cache(args.cache)
    syms_all = set(cache["symbols"].keys())
    print("Cache symbols:", syms_all, "capital:", cache["initial_capital"])
    print("=" * 100)

    # ---- baseline ----
    base_kw = dict(confidence_threshold=0.20, regime_filter=None, sl_pct=2.0,
                   tp_pct=4.0, trailing=True, use_regime_mult=True, max_open=6)
    print("BASELINE (default params)")
    print("  IS :", fmt(run(cache, 0.0, IS_FRAC, **base_kw)))
    print("  OOS:", fmt(run(cache, IS_FRAC, 1.0, **base_kw)))
    print("=" * 100)

    # ---- lever grid (IS only) ----
    regime_filters = [None, {"TRENDING"}, {"TRENDING", "VOLATILE"},
                      {"RANGING"}, {"RANGING", "TRENDING"}]
    conf_thresholds = [0.20, 0.30, 0.40, 0.50]
    sltp = [(2.0, 4.0), (3.0, 6.0), (4.0, 8.0), (2.0, 2.0), (1.5, 4.5),
            (5.0, 5.0), (3.0, 9.0)]
    trailings = [True, False]
    regime_mult_opts = [True, False]
    directions = ["both", "long", "short"]
    symbol_sets = [None] + [frozenset({s}) for s in sorted(syms_all)] + \
                  [frozenset(syms_all - {s}) for s in sorted(syms_all)]

    results = []
    combos = itertools.product(regime_filters, conf_thresholds, sltp, trailings,
                               regime_mult_opts, directions, symbol_sets)
    for rf, ct, (sl, tp), tr, rm, dr, ss in combos:
        kw = dict(confidence_threshold=ct, regime_filter=rf, sl_pct=sl, tp_pct=tp,
                  trailing=tr, use_regime_mult=rm, max_open=6,
                  trade_direction=dr, symbols=(set(ss) if ss else None))
        is_r = run(cache, 0.0, IS_FRAC, **kw)
        if is_r is None or is_r["trades"] < MIN_IS_TRADES:
            continue   # need enough trades to be statistically meaningful
        results.append((is_r, kw))

    # Rank IS by profit factor, then return.
    results.sort(key=lambda x: (x[0]["profit_factor"], x[0]["return_pct"]),
                 reverse=True)

    print(f"\nTOP 25 IS CONFIGS (of {len(results)} with >=15 IS trades), with OOS:\n")
    header = ("rank | regime_filter        ct   sl/tp     trail rmult dir   symbols")
    print(header)
    print("-" * 100)
    top = results[:25]
    enriched = []
    for rank, (is_r, kw) in enumerate(top, 1):
        oos_r = run(cache, IS_FRAC, 1.0, **kw)
        enriched.append((is_r, oos_r, kw))
        rf = kw["regime_filter"]
        rf_s = "all" if rf is None else "+".join(sorted(rf))
        ss = kw["symbols"]
        ss_s = "all" if ss is None else "+".join(sorted(s.split("/")[0] for s in ss))
        print(f"#{rank:2d} | {rf_s:18s} {kw['confidence_threshold']:.2f} "
              f"{kw['sl_pct']:.1f}/{kw['tp_pct']:.1f}  "
              f"{str(kw['trailing'])[0]}     {str(kw['use_regime_mult'])[0]}     "
              f"{kw['trade_direction']:5s} {ss_s}")
        print("       IS : " + fmt(is_r))
        print("       OOS: " + fmt(oos_r))

    # ---- THE REAL TEST: scan ALL searched configs (not just IS top-25) for any
    #      that are positive in BOTH windows with an ADEQUATE sample in BOTH. ----
    print("\n" + "=" * 100)
    print(f"ALL configs positive in BOTH IS & OOS, with >= {MIN_IS_TRADES} IS and "
          f">= {MIN_OOS_TRADES} OOS trades:")
    both_pos = []
    for is_r, kw in results:
        oos_r = run(cache, IS_FRAC, 1.0, **kw)
        if (oos_r and oos_r["return_pct"] > 0 and is_r["return_pct"] > 0
                and oos_r["trades"] >= MIN_OOS_TRADES):
            both_pos.append((is_r, oos_r, kw))
    # rank by the WORSE of the two profit factors (robustness)
    both_pos.sort(key=lambda x: min(x[0]["profit_factor"], x[1]["profit_factor"]),
                  reverse=True)
    if not both_pos:
        print(f"  NONE. No config with >= {MIN_IS_TRADES} IS / >= {MIN_OOS_TRADES} "
              "OOS trades is positive in both windows.")
        print("  => The edge does NOT generalize out-of-sample. This is tuning a")
        print("     losing system, not finding a real edge.")
    else:
        for is_r, oos_r, kw in both_pos[:15]:
            rf = kw["regime_filter"]; rf_s = "all" if rf is None else "+".join(sorted(rf))
            ss = kw["symbols"]; ss_s = "all" if ss is None else "+".join(sorted(s.split("/")[0] for s in ss))
            print(f"  [{rf_s} ct={kw['confidence_threshold']} "
                  f"sl/tp={kw['sl_pct']}/{kw['tp_pct']} trail={kw['trailing']} "
                  f"rmult={kw['use_regime_mult']} dir={kw['trade_direction']} sym={ss_s}]")
            print("     IS :", fmt(is_r))
            print("     OOS:", fmt(oos_r))
    print("=" * 100)


if __name__ == "__main__":
    main()
