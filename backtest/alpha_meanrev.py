"""New-alpha experiment: mean-reversion (opposite of the losing trend-follower).

Hypothesis: the EMA/RSI trend strategy loses by getting whipsawed; a
mean-reversion signal (buy oversold + below lower Bollinger, sell overbought +
above upper band) might extract the whipsaw. Tested rigorously IS/OOS on the
cached 1h history, fees 0.1%/side + 0.05% slippage, real SL/TP exits.

This is an isolated signal-edge test (sizing-agnostic: profit factor + per-trade
returns), NOT a wired-in strategy. Honest verdict at the end.

    .venv/bin/python -m backtest.alpha_meanrev
"""
import glob
import numpy as np
import pandas as pd

FEE = 0.001          # per side
SLIP = 0.0005        # per fill
RSI_N = 14
BB_N, BB_K = 20, 2.0


def rsi(close, n=RSI_N):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def simulate(df, lo, hi, sl_pct, tp_pct, max_hold, allow_long, allow_short):
    c = df["close"].values
    h = df["high"].values
    l = df["low"].values
    r = rsi(df["close"]).values
    ma = df["close"].rolling(BB_N).mean()
    sd = df["close"].rolling(BB_N).std()
    upper = (ma + BB_K * sd).values
    lower = (ma - BB_K * sd).values

    trades = []
    pos = None  # dict: side, entry, sl, tp, i
    start = BB_N + 2
    for i in range(start, len(df)):
        if pos is None:
            # entries (decide on bar i close, fill at i close)
            if allow_long and r[i] < lo and c[i] < lower[i]:
                e = c[i] * (1 + SLIP)
                pos = {"side": "long", "entry": e, "i": i,
                       "sl": e * (1 - sl_pct / 100), "tp": e * (1 + tp_pct / 100)}
            elif allow_short and r[i] > hi and c[i] > upper[i]:
                e = c[i] * (1 - SLIP)
                pos = {"side": "short", "entry": e, "i": i,
                       "sl": e * (1 + sl_pct / 100), "tp": e * (1 - tp_pct / 100)}
        else:
            exit_price = None
            if pos["side"] == "long":
                if l[i] <= pos["sl"]:
                    exit_price = pos["sl"]
                elif h[i] >= pos["tp"]:
                    exit_price = pos["tp"]
                elif r[i] >= 50 or (i - pos["i"]) >= max_hold:
                    exit_price = c[i]
                if exit_price is not None:
                    fill = exit_price * (1 - SLIP)
                    ret = (fill - pos["entry"]) / pos["entry"] - 2 * FEE
                    trades.append(ret); pos = None
            else:  # short
                if h[i] >= pos["sl"]:
                    exit_price = pos["sl"]
                elif l[i] <= pos["tp"]:
                    exit_price = pos["tp"]
                elif r[i] <= 50 or (i - pos["i"]) >= max_hold:
                    exit_price = c[i]
                if exit_price is not None:
                    fill = exit_price * (1 + SLIP)
                    ret = (pos["entry"] - fill) / pos["entry"] - 2 * FEE
                    trades.append(ret); pos = None
    return np.array(trades)


def metrics(trades):
    if len(trades) == 0:
        return dict(n=0, pf=float("nan"), win=float("nan"), avg=0.0)
    wins = trades[trades > 0].sum()
    losses = -trades[trades < 0].sum()
    pf = wins / losses if losses > 0 else float("inf")
    return dict(n=len(trades), pf=pf, win=(trades > 0).mean(), avg=trades.mean())


def main():
    files = sorted(glob.glob("backtest/data/*_1h.csv"))
    dfs = {}
    for f in files:
        sym = f.split("/")[-1].replace("_1h.csv", "")
        d = pd.read_csv(f)
        if "timestamp" in d.columns:
            d = d.set_index("timestamp")
        dfs[sym] = d
    print(f"symbols: {list(dfs)}  bars: {[len(d) for d in dfs.values()]}")

    # parameter grid (small, sensible)
    grid = [
        # lo, hi, sl, tp, hold, label
        (30, 70, 2.0, 2.0, 24, "rsi30/70 SL2/TP2"),
        (25, 75, 3.0, 3.0, 48, "rsi25/75 SL3/TP3"),
        (20, 80, 4.0, 3.0, 48, "rsi20/80 SL4/TP3"),
        (30, 70, 3.0, 1.5, 24, "rsi30/70 SL3/TP1.5"),
    ]
    print(f"\n{'config':24} {'dir':5} | {'IS n':>5} {'IS pf':>6} {'IS win':>6} | "
          f"{'OOS n':>5} {'OOS pf':>6} {'OOS win':>7} {'OOSavg%':>7}")
    print("-" * 88)
    any_pos = False
    for (lo, hi, sl, tp, hold, label) in grid:
        for dirn, (al, ash) in [("both", (True, True)), ("long", (True, False)),
                                ("short", (False, True))]:
            is_tr, oos_tr = [], []
            for d in dfs.values():
                split = int(len(d) * 0.70)
                is_tr.append(simulate(d.iloc[:split], lo, hi, sl, tp, hold, al, ash))
                oos_tr.append(simulate(d.iloc[split:], lo, hi, sl, tp, hold, al, ash))
            ism = metrics(np.concatenate(is_tr) if is_tr else np.array([]))
            oosm = metrics(np.concatenate(oos_tr) if oos_tr else np.array([]))
            flag = ""
            if oosm["n"] >= 20 and oosm["pf"] > 1.05 and dirn == "both":
                flag = "  <-- positive OOS (both-dir)"; any_pos = True
            print(f"{label:24} {dirn:5} | {ism['n']:>5} {ism['pf']:>6.2f} {ism['win']:>6.0%} | "
                  f"{oosm['n']:>5} {oosm['pf']:>6.2f} {oosm['win']:>7.0%} "
                  f"{oosm['avg']*100:>7.3f}{flag}")
    print("\nVERDICT:", "FOUND a both-direction positive-OOS mean-reversion config (investigate)."
          if any_pos else
          "No both-direction positive-OOS edge from mean-reversion either.")


if __name__ == "__main__":
    main()
