#!/usr/bin/env python3
"""Fast portfolio simulator over a precomputed decision cache (precompute.py).

Replays the EXACT engine.py portfolio/exit accounting (fees, slippage, sizing,
SL/TP/trailing, daily-loss breaker, consecutive-loss gate) but lets us sweep the
cheap levers WITHOUT recomputing TA/ML/regime:

  - confidence_threshold   (main.py:318 hardcodes 0.20)
  - regime_filter          : set of regimes in which entries are ALLOWED
                             (NEW lever — engine has none; entries always allowed)
  - sl_pct / tp_pct        : base SL/TP %
  - trailing               : trailing stop on/off
  - regime_sl/tp/size mult : regime-adapted multipliers (as in default.yaml)
  - symbols                : subset to trade
  - max_open               : portfolio cap
  - max_daily_loss
  - date split             : is_frac / oos_frac to slice the test window

Fidelity to engine.py is validated separately (validate_against_engine).
"""
import pickle
from dataclasses import dataclass, field
from datetime import datetime

FEE_RATE = 0.001
SLIPPAGE_RATE = 0.0005

# Default regime multipliers (mirror default.yaml regime_detector.regime_params).
DEFAULT_REGIME_MULT = {
    "TRENDING": {"sl": 1.2, "tp": 1.5, "size": 1.0},
    "RANGING":  {"sl": 0.8, "tp": 0.7, "size": 0.8},
    "VOLATILE": {"sl": 1.5, "tp": 1.0, "size": 0.5},
}


def _combine(strat_votes, ml_signal, ml_conf, ml_acc, weights,
             confidence_threshold):
    """Faithful re-implementation of TradingBot._combine_signals (main.py:252)."""
    buy_score = 0.0
    sell_score = 0.0
    total_weight = 0.0
    for name, sig, conf in strat_votes:
        w = weights.get(name, 0.25)
        total_weight += w
        if sig == "BUY":
            buy_score += w * conf
        elif sig == "SELL":
            sell_score += w * conf

    ml_weight = 0.35
    try:
        if ml_acc > 0.5:
            ml_weight = min(0.5, max(0.2, ml_acc - 0.35))
        if ml_conf > 0.70:
            ml_weight = min(0.5, ml_weight + 0.10)
    except Exception:
        pass
    if ml_signal == "BUY":
        buy_score += ml_weight * ml_conf
    elif ml_signal == "SELL":
        sell_score += ml_weight * ml_conf
    total_weight += ml_weight

    if total_weight > 0:
        buy_score /= total_weight
        sell_score /= total_weight

    if buy_score > sell_score and buy_score > confidence_threshold:
        return "BUY", buy_score
    if sell_score > buy_score and sell_score > confidence_threshold:
        return "SELL", sell_score
    return "HOLD", max(buy_score, sell_score)


@dataclass
class SimParams:
    confidence_threshold: float = 0.20
    regime_filter: set = None          # None = allow all regimes
    sl_pct: float = 2.0
    tp_pct: float = 4.0
    trailing: bool = True
    trailing_activation: float = 2.0
    trailing_distance: float = 0.5
    use_regime_mult: bool = True
    regime_mult: dict = field(default_factory=lambda: DEFAULT_REGIME_MULT)
    symbols: set = None                # None = all
    max_open: int = 6
    max_daily_loss: float = None       # None = derived from capital fraction
    max_risk_per_trade: float = 0.02
    leverage: float = 2.0
    max_single_pct: float = 0.20
    min_position_value_usd: float = 15.0
    trade_direction: str = "both"      # both/long/short
    # date split over the unified timeline (fractions 0..1)
    split_lo: float = 0.0
    split_hi: float = 1.0


def _size(capital, price, confidence, sl_pct_base, size_mult, p: SimParams):
    if capital <= 0 or price <= 0:
        return 0.0
    risk_amount = capital * p.max_risk_per_trade
    sl = abs(sl_pct_base) or 2.0
    notional = risk_amount / (sl / 100.0)
    conf = max(0.0, min(1.0, confidence))
    notional *= (0.5 + conf)
    notional *= max(0.1, min(2.0, size_mult))
    lev = max(1.0, p.leverage)
    notional = min(notional, capital * lev * p.max_single_pct)
    if notional / lev > capital:
        return 0.0
    amount = notional / price
    if amount < 0.00001:
        return 0.0
    if amount * price < p.min_position_value_usd:
        return 0.0
    return round(amount, 8)


def simulate(cache, p: SimParams):
    weights = cache["weights"]
    capital = cache["initial_capital"]
    allocations = cache["allocations"]
    syms_all = list(cache["symbols"].keys())
    syms = [s for s in syms_all if (p.symbols is None or s in p.symbols)]

    # Per-symbol pair capital (same as engine: allocation_pct of initial capital).
    pair_capital = {s: capital * allocations.get(s, 100.0) / 100.0 for s in syms}

    # Build decision rows keyed by ts for fast portfolio iteration.
    rows_by_sym = {s: {r["ts"]: r for r in cache["symbols"][s]["rows"]} for s in syms}
    ohlc = {s: cache["symbols"][s]["ohlc"] for s in syms}   # [ts,high,low,close]
    idx_map = {s: cache["symbols"][s]["index_map"] for s in syms}

    all_ts = sorted(set().union(*[set(rows_by_sym[s].keys()) for s in syms]))
    if not all_ts:
        return None
    lo = int(len(all_ts) * p.split_lo)
    hi = int(len(all_ts) * p.split_hi)
    ts_window = all_ts[lo:hi]
    if not ts_window:
        return None

    if p.max_daily_loss is None:
        p.max_daily_loss = abs(capital * 0.03)

    realized = 0.0
    open_pos = {}
    trades = []
    equity_curve = []
    daily_pnl = 0.0
    cur_day = None
    paused = False
    consec_losses = 0
    max_consec = max(3, int(10.0 / 3.0))  # default max_drawdown_pct=10 -> 3

    def close(pos, cp, reason, ts, i):
        nonlocal realized, daily_pnl, consec_losses
        if pos["side"] == "buy":
            fill = cp * (1 - SLIPPAGE_RATE)
            gross = (fill - pos["entry_price"]) * pos["amount"]
        else:
            fill = cp * (1 + SLIPPAGE_RATE)
            gross = (pos["entry_price"] - fill) * pos["amount"]
        exit_fee = fill * pos["amount"] * FEE_RATE
        net = gross - pos["entry_fee"] - exit_fee
        trades.append({"symbol": pos["symbol"], "side": pos["side"], "pnl": net,
                       "reason": reason, "regime": pos["regime"],
                       "bars_held": i - pos["entry_i"]})
        realized += net
        daily_pnl += net
        if net < 0:
            consec_losses += 1
        else:
            consec_losses = 0

    for ts in ts_window:
        day = datetime.utcfromtimestamp(ts / 1000).date()
        if day != cur_day:
            cur_day = day
            daily_pnl = 0.0
            paused = False
            consec_losses = 0   # engine calls risk.reset() each day

        # (A) exits
        for s in list(open_pos.keys()):
            if ts not in idx_map[s]:
                continue
            i = idx_map[s][ts]
            _, high, low, cl = ohlc[s][i]
            pos = open_pos[s]
            sl = pos["stop_loss"]; tp = pos["take_profit"]
            done = None
            if sl > 0:
                if pos["side"] == "buy" and low <= sl:
                    done = (sl, "Stop-loss")
                elif pos["side"] == "sell" and high >= sl:
                    done = (sl, "Stop-loss")
            if done is None and tp > 0:
                if pos["side"] == "buy" and high >= tp:
                    done = (tp, "Take-profit")
                elif pos["side"] == "sell" and low <= tp:
                    done = (tp, "Take-profit")
            if done:
                close(pos, done[0], done[1], ts, i)
                del open_pos[s]
            elif pos["trailing"]:
                # trailing ratchet on bar close
                entry = pos["entry_price"]; side = pos["side"]
                if side == "buy":
                    pnl_pct = (cl - entry) / entry * 100.0
                    if pnl_pct >= p.trailing_activation:
                        computed = round(cl - cl * (p.trailing_distance / 100.0), 8)
                    else:
                        computed = round(entry * (1 - p.sl_pct / 100.0), 8)
                    pos["stop_loss"] = max(pos["stop_loss"], computed)
                else:
                    pnl_pct = (entry - cl) / entry * 100.0
                    if pnl_pct >= p.trailing_activation:
                        computed = round(cl + cl * (p.trailing_distance / 100.0), 8)
                    else:
                        computed = round(entry * (1 + p.sl_pct / 100.0), 8)
                    pos["stop_loss"] = min(pos["stop_loss"], computed)

        # daily loss breaker
        if not paused and daily_pnl <= -p.max_daily_loss:
            for s in list(open_pos.keys()):
                if ts not in idx_map[s]:
                    continue
                i = idx_map[s][ts]
                _, _, _, cl = ohlc[s][i]
                close(open_pos[s], cl, "DailyLossBreaker", ts, i)
                del open_pos[s]
            paused = True

        equity_curve.append(capital + realized)
        if paused:
            continue
        if consec_losses >= max_consec:
            continue

        # (B) entries
        for s in syms:
            if ts not in rows_by_sym[s]:
                continue
            if s in open_pos:
                continue
            if len(open_pos) >= p.max_open:
                break
            r = rows_by_sym[s][ts]
            sig, conf = _combine(r["strat_votes"], r["ml_signal"], r["ml_conf"],
                                 r["ml_acc"], weights, p.confidence_threshold)
            if sig not in ("BUY", "SELL"):
                continue
            if p.trade_direction == "long" and sig == "SELL":
                continue
            if p.trade_direction == "short" and sig == "BUY":
                continue
            regime = r["regime"]
            if p.regime_filter is not None and regime not in p.regime_filter:
                continue
            price = r["close"]
            if price <= 0:
                continue
            # regime-adapted SL/TP/size
            if p.use_regime_mult:
                m = p.regime_mult.get(regime, p.regime_mult["RANGING"])
                sl_pct = round(p.sl_pct * m["sl"], 2)
                tp_pct = round(p.tp_pct * m["tp"], 2)
                size_mult = m["size"]
            else:
                sl_pct, tp_pct, size_mult = p.sl_pct, p.tp_pct, 1.0
            eff_capital = pair_capital[s] + (daily_pnl / max(len(syms), 1))
            amount = _size(eff_capital, price, conf, p.sl_pct, size_mult, p)
            if amount <= 0:
                continue
            base = s.split("/")[0]
            min_size = {"SOL": 0.02, "BTC": 0.0001, "ETH": 0.001}.get(base, 0.0001)
            if amount < min_size:
                amount = min_size
            side = sig.lower()
            if side == "buy":
                entry_fill = price * (1 + SLIPPAGE_RATE)
                stop_loss = entry_fill * (1 - sl_pct / 100.0)
                take_profit = entry_fill * (1 + tp_pct / 100.0)
            else:
                entry_fill = price * (1 - SLIPPAGE_RATE)
                stop_loss = entry_fill * (1 + sl_pct / 100.0)
                take_profit = entry_fill * (1 - tp_pct / 100.0)
            entry_fee = entry_fill * amount * FEE_RATE
            i = idx_map[s][ts]
            open_pos[s] = {
                "symbol": s, "side": side, "entry_price": entry_fill,
                "amount": amount, "stop_loss": stop_loss,
                "take_profit": take_profit, "trailing": p.trailing,
                "entry_fee": entry_fee, "entry_i": i, "regime": regime,
            }

    # close leftovers at last bar of window
    last_ts = ts_window[-1]
    for s, pos in list(open_pos.items()):
        i = idx_map[s].get(last_ts)
        if i is None:
            i = len(ohlc[s]) - 1
        _, _, _, cl = ohlc[s][i]
        close(pos, cl, "EndOfWindow", last_ts, i)

    return _report(trades, equity_curve, realized, capital, ts_window)


def _report(trades, equity_curve, realized, capital, ts_window):
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gw = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in losses)
    pf = (gw / gl) if gl > 0 else float("inf")
    peak = capital; max_dd = 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    span = max(1.0, (ts_window[-1] - ts_window[0]) / 1000.0 / 86400.0)
    sl_count = sum(1 for t in trades if t["reason"] == "Stop-loss")
    tp_count = sum(1 for t in trades if t["reason"] == "Take-profit")
    return {
        "return_pct": round(realized / capital * 100.0, 3),
        "realized": round(realized, 2),
        "trades": n,
        "win_rate": round(len(wins) / n, 4) if n else 0.0,
        "wins": len(wins), "losses": len(losses),
        "profit_factor": round(pf, 3) if pf != float("inf") else 999.0,
        "max_dd_pct": round(max_dd, 3),
        "span_days": round(span, 1),
        "daily_pnl": round(realized / span, 2),
        "daily_pct": round(realized / span / capital * 100.0, 4),
        "sl_count": sl_count, "tp_count": tp_count,
    }


def load_cache(path):
    with open(path, "rb") as f:
        return pickle.load(f)
