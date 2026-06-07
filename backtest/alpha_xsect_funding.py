"""Funding/borrow sensitivity for the cross-sectional momentum edge.

The strategy shorts alts; on margin that costs borrow. The short leg is ~0.5 of
capital held continuously, so annual borrow B drags ~0.5*B/yr. This checks how
much of the +1.33%/mo gross survives realistic borrow costs — the make-or-break
honesty test before calling it a deployable edge.

    .venv/bin/python -m backtest.alpha_xsect_funding
"""
import time
import numpy as np
import pandas as pd
import ccxt

FEE = 0.001
SLIP = 0.0005
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
    return pd.DataFrame(out, columns=["ts","o","h","l","c","v"]).drop_duplicates("ts").set_index("ts")["c"]


def strat(close, lb, k, hold, annual_borrow):
    rets = close.pct_change(); mom = close.pct_change(lb); dates = close.index
    daily_borrow = annual_borrow / 365.0
    out = []; w = pd.Series(0.0, index=close.columns)
    for i in range(lb + 1, len(dates)):
        day = (w * rets.iloc[i]).sum()
        # borrow cost on short notional (sum of negative weights), held daily
        short_notional = -w[w < 0].sum()
        day -= short_notional * daily_borrow
        if (i - (lb + 1)) % hold == 0:
            m = mom.iloc[i-1].dropna()
            if len(m) >= 2*k:
                r = m.sort_values(); nw = pd.Series(0.0, index=close.columns)
                nw[r.index[-k:]] = 0.5/k; nw[r.index[:k]] = -0.5/k
                day -= (nw - w).abs().sum() * (FEE + SLIP); w = nw
        out.append(day)
    return pd.Series(out, index=dates[lb+1:])


def main():
    ex = ccxt.bitfinex({"enableRateLimit": True})
    print("fetching basket...")
    series = {s: c for s in BASKET if (c := fetch_daily(ex, s)) is not None and len(c) > 300}
    close = pd.DataFrame(series).dropna(how="all").ffill().dropna()
    print(f"panel: {close.shape[0]}d x {close.shape[1]} assets\n")
    print("borrow(ann)  monthly%   Sharpe   ann%   (lb90 k2 hold20, fees+slip+borrow)")
    print("-" * 64)
    for b in (0.0, 0.05, 0.10, 0.20, 0.30):
        r = strat(close, 90, 2, 20, b)
        mo = r.mean()*365/12*100; sh = r.mean()/r.std()*np.sqrt(365) if r.std() else 0; ann = r.mean()*365*100
        print(f"  {b*100:4.0f}%      {mo:+6.2f}%   {sh:+5.2f}   {ann:+6.1f}%")
    print("\nReality: Bitfinex margin borrow on alts is often ~15-30%/yr. "
          "If the edge only survives at <10% borrow, it is NOT reliably deployable as a short-leg strategy.")


if __name__ == "__main__":
    main()
