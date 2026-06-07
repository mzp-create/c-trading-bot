"""Market-neutral funding/basis carry (cash-and-carry): long spot + short perp,
delta-neutral, harvest the perp funding rate. The most realistic low-drawdown
crypto edge. Uses Bitfinex perp funding-rate history via ccxt.

A continuously-held delta-neutral short-perp position RECEIVES fundingRate each
period when funding>0 (pays when <0). Realized carry ≈ Σ fundingRate (minus a
small fee drag). Delta-neutral, so price risk is hedged; main risk is funding
turning negative. IS/OOS to check stability.

    .venv/bin/python -m backtest.alpha_funding_carry
"""
import os
import time
import numpy as np
import pandas as pd
import ccxt

PERPS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT"]
CACHE = "backtest/data/funding_hist.csv"
FEE_DRAG_ANNUAL = 0.02  # ~2%/yr for periodic rebalancing/roll of the hedge


def fetch_funding(ex, sym, years=1):
    since = ex.milliseconds() - int(years*365*24*3600*1000)
    out = []
    while True:
        rows = None
        for a in range(4):
            try: rows = ex.fetch_funding_rate_history(sym, since=since, limit=500); break
            except Exception: time.sleep(2*(a+1))
        if not rows: break
        out += rows
        last = rows[-1]["timestamp"]
        if last is None or len(rows) < 500: break
        since = last + 1
        if since > ex.milliseconds(): break
        time.sleep(max(0.5, ex.rateLimit/1000))
    if not out: return None
    s = pd.Series({r["timestamp"]: r["fundingRate"] for r in out if r.get("fundingRate") is not None})
    return s.sort_index()


def load_or_fetch():
    if os.path.exists(CACHE):
        df = pd.read_csv(CACHE, index_col=0); df.index = df.index.astype("int64")
        print(f"loaded cached funding {df.shape}"); return df
    ex = ccxt.bitfinex({"enableRateLimit": True})
    print("fetching funding histories (with retries)...")
    cols = {}
    for s in PERPS:
        fr = fetch_funding(ex, s)
        if fr is not None and len(fr) > 200:
            cols[s] = fr; print(f"  {s}: {len(fr)} funding periods")
        time.sleep(3)  # spacing to avoid rate-limit
    if not cols: return None
    df = pd.DataFrame(cols).sort_index()
    df.to_csv(CACHE); print(f"cached -> {CACHE}")
    return df


def main():
    df = load_or_fetch()
    if df is None or df.shape[1] < 3:
        print("ERROR: insufficient funding data (rate-limited?); rerun."); return
    # infer periods/year from median spacing
    ts = df.index.to_numpy()
    spacing_ms = np.median(np.diff(ts))
    per_year = (365*24*3600*1000)/spacing_ms
    print(f"periods x symbols: {df.shape}  (~{per_year:.0f} funding periods/yr, "
          f"{spacing_ms/3600000:.1f}h spacing)")
    fee_per_period = FEE_DRAG_ANNUAL / per_year

    # equal-weight basket carry: receive mean funding across held perps each period
    basket = df.mean(axis=1, skipna=True).dropna()
    net = basket - fee_per_period          # net carry per period (delta-neutral)
    split = int(len(net)*0.70)
    for label, r in [("FULL", net), ("IS", net.iloc[:split]), ("OOS", net.iloc[split:])]:
        if len(r)==0: continue
        ann = r.mean()*per_year*100; mo = ann/12
        # carry vol is tiny (delta-neutral) -> Sharpe high but it's funding risk not price
        sh = r.mean()/r.std()*np.sqrt(per_year) if r.std() else float('nan')
        pos = (r>0).mean()*100
        print(f"  {label:4}: {mo:+5.2f}%/mo  {ann:+6.1f}%/yr  periods+={pos:4.0f}%  n={len(r)}")
    print("\n  per-symbol annualized funding (gross):")
    for c in df.columns:
        s = df[c].dropna()
        print(f"    {c:16} {s.mean()*per_year*100:+6.1f}%/yr  (held {len(s)} periods, {(s>0).mean()*100:.0f}% positive)")
    print("\nNote: delta-neutral, so price-direction risk is hedged. Returns ARE the "
          "funding carry minus fees. Risk = funding flipping negative + execution/roll. "
          "This is the realistic ~steady-yield path; compare vs the 2%/mo target.")


if __name__ == "__main__":
    main()
