"""Can the cross-sectional momentum edge reach ~2%/month via leverage, and at
what drawdown? Leverage scales return AND drawdown linearly (Sharpe unchanged),
so this shows the honest risk needed to hit the target.

    .venv/bin/python -m backtest.alpha_xsect_leverage
"""
import time
import numpy as np
import pandas as pd
import ccxt

FEE = 0.001; SLIP = 0.0005
BASKET = ["BTC/USD","ETH/USD","SOL/USD","LTC/USD","XRP/USD","DOGE/USD","ADA/USD",
          "AVAX/USD","LINK/USD","DOT/USD","XLM/USD","EOS/USD","ATOM/USD","UNI/USD","AAVE/USD"]


def fetch_daily(ex, sym, years=3):
    since0 = ex.milliseconds() - int(years*365*24*3600*1000); out=[]; since=since0
    while True:
        rows=None
        for attempt in range(4):  # retry on rate-limit/transient
            try: rows = ex.fetch_ohlcv(sym,"1D",since=since,limit=1000); break
            except Exception: time.sleep(2*(attempt+1))
        if not rows: break
        out += rows; since = rows[-1][0]+86400000
        if len(rows) < 1000 or since > ex.milliseconds(): break
        time.sleep(max(0.5, ex.rateLimit/1000))
    return pd.DataFrame(out,columns=["ts","o","h","l","c","v"]).drop_duplicates("ts").set_index("ts")["c"] if out else None


CACHE = "backtest/data/xsect_panel_3y.csv"
def load_panel():
    import os
    if os.path.exists(CACHE):
        df = pd.read_csv(CACHE, index_col=0); df.index = df.index.astype("int64"); return df
    return None


def strat(close, lb, k, hold, annual_borrow):
    rets=close.pct_change(); mom=close.pct_change(lb); dates=close.index
    db=annual_borrow/365.0; out=[]; w=pd.Series(0.0,index=close.columns)
    for i in range(lb+1,len(dates)):
        day=(w*rets.iloc[i]).sum() - (-w[w<0].sum())*db
        if (i-(lb+1))%hold==0:
            m=mom.iloc[i-1].dropna()
            if len(m)>=2*k:
                r=m.sort_values(); nw=pd.Series(0.0,index=close.columns)
                nw[r.index[-k:]]=0.5/k; nw[r.index[:k]]=-0.5/k
                day-=(nw-w).abs().sum()*(FEE+SLIP); w=nw
        out.append(day)
    return pd.Series(out,index=dates[lb+1:])


def max_dd(r, lev=1.0):
    eq=(1+r*lev).cumprod(); peak=eq.cummax(); return ((eq-peak)/peak).min()


def main():
    close = load_panel()
    if close is None:
        ex=ccxt.bitfinex({"enableRateLimit":True})
        print("fetching (with retries) + caching...")
        series={s:c for s in BASKET if (c:=fetch_daily(ex,s)) is not None and len(c)>300}
        close=pd.DataFrame(series).dropna(how="all").ffill().dropna()
        if close.shape[1] >= 4:
            close.to_csv(CACHE); print(f"cached panel -> {CACHE}")
    else:
        print(f"loaded cached panel {close.shape}")
    if close.shape[1] < 4:
        print("ERROR: insufficient assets fetched (rate-limited); rerun shortly."); return
    r=strat(close,90,2,20,0.20)  # realistic 20% borrow
    mo=r.mean()*365/12; sh=r.mean()/r.std()*np.sqrt(365)
    dd1=max_dd(r,1.0)
    print(f"\nBase (lb90 k2 hold20, 20% borrow, {close.shape[1]} assets, {close.shape[0]}d):")
    print(f"  unlevered: {mo*100:+.2f}%/mo  Sharpe {sh:.2f}  maxDD {dd1*100:.1f}%")
    print(f"\n  leverage  monthly%   maxDD%   ann%")
    for L in (1,2,3,4):
        print(f"   {L}x       {mo*100*L:+6.2f}%   {max_dd(r,L)*100:6.1f}%  {mo*365/12*0 + r.mean()*365*100*L:+6.1f}%")
    need = 0.02 / mo if mo>0 else float('inf')
    print(f"\n  -> leverage needed for +2.00%/mo: {need:.1f}x  => implied maxDD ~{max_dd(r,need)*100:.0f}%")
    print("  Honest: Sharpe is fixed; leverage buys return with proportional drawdown.")


if __name__ == "__main__":
    main()
