"""Harden the cross-sectional momentum candidate: bigger basket, SLIPPAGE added,
and WALK-FORWARD (consecutive time folds) instead of one split — to check the
lb90 edge is temporally robust, not cherry-picked.

    .venv/bin/python -m backtest.alpha_xsect_validate
"""
import time
import numpy as np
import pandas as pd
import ccxt

FEE = 0.001
SLIP = 0.0005  # per leg per rebalance (added vs the first pass)
# wider liquid basket
BASKET = ["BTC/USD", "ETH/USD", "SOL/USD", "LTC/USD", "XRP/USD", "DOGE/USD",
          "ADA/USD", "AVAX/USD", "LINK/USD", "DOT/USD", "XLM/USD", "EOS/USD",
          "ATOM/USD", "UNI/USD", "AAVE/USD"]


def fetch_daily(ex, sym, years=3):
    since = ex.milliseconds() - int(years * 365 * 24 * 3600 * 1000)
    out = []
    while True:
        try:
            rows = ex.fetch_ohlcv(sym, "1D", since=since, limit=1000)
        except Exception:
            return None
        if not rows:
            break
        out += rows
        since = rows[-1][0] + 86400000
        if len(rows) < 1000 or since > ex.milliseconds():
            break
        time.sleep(ex.rateLimit / 1000)
    if not out:
        return None
    df = pd.DataFrame(out, columns=["ts", "o", "h", "l", "c", "v"]).drop_duplicates("ts").set_index("ts")
    return df["c"]


def strat_returns(close, lookback, k, hold):
    rets = close.pct_change()
    mom = close.pct_change(lookback)
    dates = close.index
    out = []
    w = pd.Series(0.0, index=close.columns)
    for i in range(lookback + 1, len(dates)):
        day = (w * rets.iloc[i]).sum()
        if (i - (lookback + 1)) % hold == 0:
            m = mom.iloc[i - 1].dropna()
            if len(m) >= 2 * k:
                r = m.sort_values()
                nw = pd.Series(0.0, index=close.columns)
                nw[r.index[-k:]] = 0.5 / k
                nw[r.index[:k]] = -0.5 / k
                turn = (nw - w).abs().sum()
                day -= turn * (FEE + SLIP)
                w = nw
        out.append(day)
    return pd.Series(out, index=dates[lookback + 1:])


def fold_stats(r):
    if len(r) == 0 or r.std() == 0:
        return (0.0, 0.0)
    return (r.mean() * 365 / 12 * 100, r.mean() / r.std() * np.sqrt(365))  # monthly%, Sharpe


def main():
    ex = ccxt.bitfinex({"enableRateLimit": True})
    print("fetching wider basket (3yr daily)...")
    series = {}
    for s in BASKET:
        c = fetch_daily(ex, s)
        if c is not None and len(c) > 300:
            series[s] = c
    print(f"  got {len(series)} assets: {list(series)}")
    close = pd.DataFrame(series).dropna(how="all").ffill().dropna()
    print(f"panel: {close.shape[0]} days x {close.shape[1]} assets "
          f"({pd.to_datetime(close.index[0],unit='ms').date()} -> "
          f"{pd.to_datetime(close.index[-1],unit='ms').date()})  (fees+slippage modeled)")

    NFOLDS = 4
    for (lb, k, hold) in [(90, 1, 10), (90, 1, 20), (90, 2, 20), (60, 1, 20)]:
        r = strat_returns(close, lb, k, hold)
        folds = np.array_split(r, NFOLDS)
        line = f"lb{lb} k{k} hold{hold:>2}: "
        full_mo, full_sh = fold_stats(r)
        parts = []
        pos_folds = 0
        for j, f in enumerate(folds):
            mo, sh = fold_stats(f)
            parts.append(f"F{j+1} {mo:+5.2f}%/{sh:+4.2f}")
            if mo > 0:
                pos_folds += 1
        print(f"{line}FULL {full_mo:+5.2f}%/mo Sh{full_sh:+4.2f} | " + " ".join(parts)
              + f" | {pos_folds}/{NFOLDS} folds +")
    print("\nRobust = positive across most folds (not one lucky window). "
          "Sharpe ~0.5 and ~0.5-1%/mo would be a real-but-modest market-neutral edge.")


if __name__ == "__main__":
    main()
