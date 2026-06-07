"""New-alpha experiment #2: Donchian breakout + volatility-expansion filter.

The third distinct signal family (after trend-cross and mean-reversion):
momentum/breakout. BUY when price breaks the N-bar high WITH volatility
expanding (ATR > its average); SHORT on the N-bar low breakdown. Rigorous IS/OOS
on cached 1h history, fees 0.1%/side + slippage, SL/TP/time exits.

    .venv/bin/python -m backtest.alpha_breakout
"""
import glob
import numpy as np
import pandas as pd

FEE = 0.001
SLIP = 0.0005


def atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def simulate(df, N, sl_pct, tp_pct, max_hold, vol_filter, allow_long, allow_short):
    c = df["close"].values
    h = df["high"].values
    l = df["low"].values
    up = df["high"].rolling(N).max().shift(1).values    # prior N-bar high
    dn = df["low"].rolling(N).min().shift(1).values     # prior N-bar low
    a = atr(df).values
    a_avg = pd.Series(a).rolling(50).mean().values
    trades = []
    pos = None
    start = N + 51
    for i in range(start, len(df)):
        vol_ok = (not vol_filter) or (a[i] > a_avg[i])
        if pos is None:
            if allow_long and c[i] > up[i] and vol_ok:
                e = c[i] * (1 + SLIP)
                pos = {"side": "long", "entry": e, "i": i,
                       "sl": e * (1 - sl_pct / 100), "tp": e * (1 + tp_pct / 100)}
            elif allow_short and c[i] < dn[i] and vol_ok:
                e = c[i] * (1 - SLIP)
                pos = {"side": "short", "entry": e, "i": i,
                       "sl": e * (1 + sl_pct / 100), "tp": e * (1 - tp_pct / 100)}
        else:
            xp = None
            if pos["side"] == "long":
                if l[i] <= pos["sl"]: xp = pos["sl"]
                elif h[i] >= pos["tp"]: xp = pos["tp"]
                elif (i - pos["i"]) >= max_hold: xp = c[i]
                if xp is not None:
                    fill = xp * (1 - SLIP)
                    trades.append((fill - pos["entry"]) / pos["entry"] - 2 * FEE); pos = None
            else:
                if h[i] >= pos["sl"]: xp = pos["sl"]
                elif l[i] <= pos["tp"]: xp = pos["tp"]
                elif (i - pos["i"]) >= max_hold: xp = c[i]
                if xp is not None:
                    fill = xp * (1 + SLIP)
                    trades.append((pos["entry"] - fill) / pos["entry"] - 2 * FEE); pos = None
    return np.array(trades)


def metrics(t):
    if len(t) == 0:
        return dict(n=0, pf=float("nan"), win=float("nan"), avg=0.0)
    w = t[t > 0].sum(); loss = -t[t < 0].sum()
    return dict(n=len(t), pf=(w / loss if loss > 0 else float("inf")),
                win=(t > 0).mean(), avg=t.mean())


def main():
    dfs = {}
    for f in sorted(glob.glob("backtest/data/*_1h.csv")):
        d = pd.read_csv(f)
        if "timestamp" in d.columns:
            d = d.set_index("timestamp")
        dfs[f.split("/")[-1].replace("_1h.csv", "")] = d
    print(f"symbols: {list(dfs)} bars: {[len(d) for d in dfs.values()]}")
    grid = [
        (20, 3, 6, 48, True,  "Donch20 SL3/TP6 volfilt"),
        (50, 4, 8, 72, True,  "Donch50 SL4/TP8 volfilt"),
        (20, 3, 6, 48, False, "Donch20 SL3/TP6 nofilt"),
        (50, 5, 10, 96, True, "Donch50 SL5/TP10 volfilt"),
    ]
    print(f"\n{'config':28} {'dir':5} | {'IS n':>5} {'IS pf':>6} | "
          f"{'OOS n':>5} {'OOS pf':>6} {'OOS win':>7} {'OOSday%':>8}")
    print("-" * 84)
    any_pos = False
    for (N, sl, tp, hold, vf, label) in grid:
        for dirn, (al, ash) in [("both", (True, True)), ("long", (True, False)),
                                ("short", (False, True))]:
            ist, oost = [], []
            for d in dfs.values():
                sp = int(len(d) * 0.70)
                ist.append(simulate(d.iloc[:sp], N, sl, tp, hold, vf, al, ash))
                oost.append(simulate(d.iloc[sp:], N, sl, tp, hold, vf, al, ash))
            im = metrics(np.concatenate(ist)); om = metrics(np.concatenate(oost))
            # ~daily%: OOS avg per-trade * trades / OOS days (~40d) -- rough
            oos_day = om["avg"] * om["n"] / 40 * 100 if om["n"] else 0.0
            flag = ""
            if dirn == "both" and om["n"] >= 20 and om["pf"] > 1.1 and im["pf"] > 1.0:
                flag = "  <-- positive IS+OOS both-dir"; any_pos = True
            print(f"{label:28} {dirn:5} | {im['n']:>5} {im['pf']:>6.2f} | "
                  f"{om['n']:>5} {om['pf']:>6.2f} {om['win']:>7.0%} {oos_day:>8.3f}{flag}")
    print("\nVERDICT:", "Breakout shows a both-dir IS+OOS positive config (investigate)."
          if any_pos else
          "Breakout/momentum also has NO both-direction IS+OOS edge.")


if __name__ == "__main__":
    main()
