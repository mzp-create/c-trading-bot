"""Cash-and-carry validation on BINANCE funding data (Bitfinex history unavailable).
Validates the CONCEPT (does crypto funding carry yield steady positive return?) —
informs whether the edge is real and worth a venue change. Long spot / short perp
delta-neutral: realized carry ~= Σ fundingRate received by the short, minus fees.

    .venv/bin/python -m backtest.alpha_carry_binance
"""
import os, time
import numpy as np, pandas as pd, ccxt

PERPS = ["BTC/USDT:USDT","ETH/USDT:USDT","SOL/USDT:USDT","XRP/USDT:USDT",
         "DOGE/USDT:USDT","AVAX/USDT:USDT","LINK/USDT:USDT","ADA/USDT:USDT"]
CACHE = "backtest/data/binance_funding.csv"
FEE_DRAG_ANNUAL = 0.03  # generous: periodic roll/rebalance of the hedge


def fetch(ex, sym, years=2):
    since = ex.milliseconds() - int(years*365*24*3600*1000); out=[]; s=since
    for _ in range(12):
        try: rows = ex.fetch_funding_rate_history(sym, since=s, limit=1000)
        except Exception: time.sleep(2); continue
        if not rows: break
        out += rows; s = rows[-1]["timestamp"]+1
        if len(rows) < 1000 or s > ex.milliseconds(): break
        time.sleep(ex.rateLimit/1000)
    if not out: return None
    return pd.Series({r["timestamp"]: r["fundingRate"] for r in out
                      if r.get("fundingRate") is not None}).sort_index()


def main():
    if os.path.exists(CACHE):
        df = pd.read_csv(CACHE, index_col=0); df.index = df.index.astype("int64")
        print(f"loaded cached {df.shape}")
    else:
        ex = ccxt.binanceusdm({"enableRateLimit": True})
        cols = {}
        for sym in PERPS:
            fr = fetch(ex, sym)
            if fr is not None and len(fr) > 300:
                cols[sym] = fr; print(f"  {sym}: {len(fr)} periods")
        df = pd.DataFrame(cols).sort_index(); df.to_csv(CACHE); print(f"cached -> {CACHE}")
    if df.shape[1] < 3:
        print("insufficient data"); return
    per_year = (365*24*3600*1000)/np.median(np.diff(df.index.to_numpy()))
    print(f"{df.shape} | ~{per_year:.0f} periods/yr | "
          f"{pd.to_datetime(df.index[0],unit='ms').date()} -> {pd.to_datetime(df.index[-1],unit='ms').date()}")
    fee = FEE_DRAG_ANNUAL/per_year
    basket = df.mean(axis=1, skipna=True).dropna()
    net = basket - fee
    split = int(len(net)*0.70)
    print("\n  window  monthly%  annual%  %periods+  Sharpe")
    for lab, r in [("FULL", net), ("IS", net.iloc[:split]), ("OOS", net.iloc[split:])]:
        ann = r.mean()*per_year*100; sh = r.mean()/r.std()*np.sqrt(per_year) if r.std() else 0
        print(f"  {lab:5}  {ann/12:+6.2f}%  {ann:+6.1f}%   {(r>0).mean()*100:4.0f}%    {sh:5.1f}")
    print("\n  per-symbol gross annualized funding:")
    for c in df.columns:
        s=df[c].dropna(); print(f"    {c:16} {s.mean()*per_year*100:+6.1f}%/yr  ({(s>0).mean()*100:.0f}% positive)")
    print("\nNote: delta-neutral -> low price risk; return IS the carry net of fees. "
          "Compare monthly% vs the 2%/mo target. Account is on Bitfinex (no funding "
          "history there) — positive result here = consider venue/data for deployment.")


if __name__ == "__main__":
    main()
