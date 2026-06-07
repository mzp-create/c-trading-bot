"""New-alpha research (resumed): cross-sectional momentum, market-neutral.

Distinct from everything tested so far (which were directional, single-asset, and
on 4.4mo of one crash regime). Idea: across a basket, each rebalance go LONG the
top-k by trailing momentum and SHORT the bottom-k — dollar-neutral, so it harvests
relative strength rather than market beta. Multi-year DAILY data (several regimes),
fees modeled, IS/OOS split.

    .venv/bin/python -m backtest.alpha_xsect_momentum
"""
import time
import numpy as np
import pandas as pd
import ccxt

FEE = 0.001  # per side per rebalance leg
BASKET = ["BTC/USD", "ETH/USD", "SOL/USD", "LTC/USD", "XRP/USD", "DOGE/USD", "ADA/USD"]


def fetch_daily(ex, sym, years=3):
    since = ex.milliseconds() - int(years * 365 * 24 * 3600 * 1000)
    out = []
    while True:
        try:
            rows = ex.fetch_ohlcv(sym, "1D", since=since, limit=1000)
        except Exception as e:
            print(f"  {sym}: fetch error {e}"); break
        if not rows:
            break
        out += rows
        since = rows[-1][0] + 86400000
        if len(rows) < 1000 or since > ex.milliseconds():
            break
        time.sleep(ex.rateLimit / 1000)
    if not out:
        return None
    df = pd.DataFrame(out, columns=["ts", "o", "h", "l", "c", "v"]).drop_duplicates("ts")
    df = df.set_index("ts")
    return df["c"]


def backtest_xsect(close_df, lookback, k, hold, fee=FEE):
    """Daily close panel -> long top-k / short bottom-k by trailing `lookback`
    return, rebalanced every `hold` days, dollar-neutral. Returns daily strat
    returns (net of fees on rebalance turnover)."""
    rets = close_df.pct_change()
    mom = close_df.pct_change(lookback)
    dates = close_df.index
    strat = []
    weights = pd.Series(0.0, index=close_df.columns)
    for i in range(lookback + 1, len(dates)):
        # apply yesterday's weights to today's return
        day_ret = (weights * rets.iloc[i]).sum()
        # rebalance every `hold` days
        if (i - (lookback + 1)) % hold == 0:
            m = mom.iloc[i - 1].dropna()
            if len(m) >= 2 * k:
                ranked = m.sort_values()
                shorts = ranked.index[:k]; longs = ranked.index[-k:]
                new_w = pd.Series(0.0, index=close_df.columns)
                new_w[longs] = 0.5 / k; new_w[shorts] = -0.5 / k
                turnover = (new_w - weights).abs().sum()
                day_ret -= turnover * fee
                weights = new_w
        strat.append(day_ret)
    return pd.Series(strat, index=dates[lookback + 1:])


def stats(r, label):
    if len(r) == 0 or r.std() == 0:
        return f"{label}: no data"
    ann = r.mean() * 365
    sharpe = r.mean() / r.std() * np.sqrt(365)
    cum = (1 + r).prod() - 1
    mo = ann / 12
    return (f"{label}: ann {ann*100:6.1f}% | monthly {mo*100:6.2f}% | "
            f"Sharpe {sharpe:5.2f} | cum {cum*100:7.1f}% | n={len(r)}")


def main():
    ex = ccxt.bitfinex({"enableRateLimit": True})
    print("fetching multi-year daily history...")
    series = {}
    for s in BASKET:
        c = fetch_daily(ex, s, years=3)
        if c is not None and len(c) > 200:
            series[s] = c; print(f"  {s}: {len(c)} days")
    if len(series) < 4:
        print("Not enough assets fetched; aborting."); return
    close = pd.DataFrame(series).dropna(how="all").ffill().dropna()
    print(f"panel: {close.shape[0]} days x {close.shape[1]} assets "
          f"({pd.to_datetime(close.index[0],unit='ms').date()} -> "
          f"{pd.to_datetime(close.index[-1],unit='ms').date()})")
    split = int(len(close) * 0.70)
    print("\nlookback/k/hold      IS (in-sample)                              OOS (out-of-sample)")
    print("-" * 100)
    any_pos = False
    for lookback in (20, 30, 60, 90):
        for k in (1, 2):
            for hold in (5, 10, 20):
                if 2 * k > close.shape[1]:
                    continue
                full = backtest_xsect(close, lookback, k, hold)
                is_r = full.iloc[:split]; oos_r = full.iloc[split:]
                oos_ann = oos_r.mean() * 365 if len(oos_r) else 0
                oos_sh = (oos_r.mean()/oos_r.std()*np.sqrt(365)) if len(oos_r) and oos_r.std() else 0
                flag = ""
                if oos_ann > 0 and oos_sh > 0.5 and is_r.mean() > 0:
                    flag = "  <-- positive IS+OOS"; any_pos = True
                print(f"lb{lookback:>2} k{k} hold{hold:>2} | "
                      f"{stats(is_r,'IS')[4:]:48} | OOS mo {oos_ann/12*100:6.2f}% Sh {oos_sh:5.2f}{flag}")
    print("\nVERDICT:", "Cross-sectional momentum shows IS+OOS positive config(s) — investigate."
          if any_pos else "No robust IS+OOS positive cross-sectional momentum edge found.")


if __name__ == "__main__":
    main()
