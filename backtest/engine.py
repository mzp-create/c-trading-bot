#!/usr/bin/env python3
"""Backtest Engine — historical bar-by-bar replay for the Hermes trading bot.

DESIGN GOAL: measure the strategy's edge HONESTLY, using the EXACT same
decision pipeline as live trading, with NO LOOKAHEAD.

How it stays faithful to the live bot
-------------------------------------
The live decision path is (main.py):
    analyze_market -> _flatten_ta -> strategies.get_signals -> _combine_signals
                   -> sizing via risk.calculate_position_size
                   -> SL/TP from regime_detector.get_adapted_params
                   -> exits via risk.update_position (SL/TP/trailing)

This engine instantiates the SAME component objects (TechnicalAnalyzer,
MLPredictor, StrategySelector, RiskManager, MarketRegimeDetector) and reuses
the PRODUCTION ``TradingBot._combine_signals`` method directly (called as an
unbound method on a minimal shim that exposes the attributes it reads). It also
reuses ``TradingBot._flatten_ta`` (-> analysis.utils.flatten_ta). That means the
scoring/weighting logic cannot silently diverge from live — it IS the live code.

NO-LOOKAHEAD GUARANTEE
----------------------
At bar i we feed ONLY ``candles.iloc[: i+1]`` (inclusive of bar i, the bar that
just CLOSED) to every analyzer. The decision for bar i therefore never sees
bar i+1..N. The entry fill is at bar i's CLOSE. Exits are evaluated on
SUBSEQUENT bars (i+1, i+2, ...) using their high/low — see ``_simulate_exits``.
The ML model is trained ONLY on data strictly before the test window (a single
in-sample-free split), so ML predictions during the replay also have no peek at
future bars. See ``_train_ml_walk_forward``.

What is intentionally disabled in backtest (and why)
----------------------------------------------------
* Sentiment: ``sentiment.enabled`` is False in the 5k/long configs anyway, and
  there is no historical sentiment feed — forced OFF for the run.
* LLM reviewer: requires a live API and is non-deterministic; forced OFF so the
  result is reproducible. (Both are layers that only ever REMOVE trades / lower
  confidence, so disabling them is, if anything, GENEROUS to the strategy.)
* Ensemble/RL strategy: ``shadow: true`` in the configs, so it never affects the
  live trade decision either. Kept shadow here (no effect on PnL).
* The live price-divergence guard and correlation guard are live-data safety
  checks, not edge sources; omitted (they would only reduce trade count).
"""

import copy
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

import pandas as pd

log = logging.getLogger("backtest")


# Bitfinex taker fee is ~0.065%/side; we use 0.10%/side to be conservative.
FEE_RATE = 0.001          # 10 bps per side
SLIPPAGE_RATE = 0.0005    # 5 bps slippage per fill (entry and exit)


@dataclass
class Trade:
    symbol: str
    side: str                # 'buy' / 'sell'
    entry_ts: int
    entry_price: float
    amount: float
    exit_ts: Optional[int] = None
    exit_price: Optional[float] = None
    reason: str = ""
    gross_pnl: float = 0.0
    fees: float = 0.0
    pnl: float = 0.0         # net of fees + slippage
    confidence: float = 0.0
    regime: str = ""
    bars_held: int = 0


class BacktestEngine:
    """Replays historical OHLCV through the live decision pipeline."""

    def __init__(self, config: dict):
        # Deep-copy so our forced overrides (sentiment/LLM off) don't mutate the
        # caller's config object.
        self.config = copy.deepcopy(config)

        # Force the layers we cannot/shouldn't run historically OFF.
        self.config.setdefault("sentiment", {})["enabled"] = False
        # LLM: removed by not attaching an llm_reviewer to the shim (see below).

        self.initial_capital = float(
            self.config.get("trading", {}).get("initial_capital", 5000.0))

        # Symbols (enabled only), same resolution as TradingBot.
        raw = self.config.get("trading", {}).get("symbols", [])
        if not raw:
            legacy = self.config.get("trading", {}).get("symbol", "BTC/USDT")
            raw = [{"name": legacy, "symbol": legacy.replace("/", ""),
                    "allocation_pct": 100.0, "enabled": True}]
        self.symbols = [s for s in raw if s.get("enabled", True)]
        for s in self.symbols:
            s["pair_capital"] = self.initial_capital * s.get("allocation_pct", 100.0) / 100.0

        self.max_open = int(self.config.get("trading", {}).get("max_open_positions", 3))
        self.max_daily_loss = abs(float(self.config.get("risk", {}).get("max_daily_loss", 150.0)))

        # Where to cache the long history we fetch (repeatable runs).
        self.data_dir = Path(__file__).parent / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Build the SAME components the live bot builds.
        from analysis.technical import TechnicalAnalyzer
        from analysis.ml_predictor import MLPredictor
        from strategies.selector import StrategySelector
        from risk.manager import RiskManager
        from risk.regime_detector import MarketRegimeDetector

        self.analyzer = TechnicalAnalyzer(self.config)
        self.ml_predictor = MLPredictor(self.config)
        self.trade_direction = self.config.get("trading", {}).get("trade_direction", "both")
        self.strategies = StrategySelector(self.config, trade_direction=self.trade_direction)
        self.risk = RiskManager(self.config)
        self.regime_detector = MarketRegimeDetector(self.config)

        # Backtest knobs (overridable via config["backtest"]).
        bt = self.config.get("backtest", {})
        self.history_bars = int(bt.get("history_bars", 4400))   # ~6 months 1h
        self.train_bars = int(bt.get("train_bars", 1200))       # ML in-sample
        self.timeframe = bt.get("timeframe", "1h")

    # ── shim: reuse the PRODUCTION _combine_signals / _flatten_ta ───────────
    class _ComboShim:
        """Minimal object exposing exactly the attributes TradingBot's
        ``_combine_signals``/``_flatten_ta`` read, so we can call the unbound
        production methods without constructing a full TradingBot (which would
        need exchange keys / network)."""
        def __init__(self, engine):
            self.strategies = engine.strategies
            self.ml_predictor = engine.ml_predictor
            self.sentiment = _NullSentiment()
            self.regime_detector = engine.regime_detector
            self.config = engine.config
            self.daily_pnl = 0.0
            self.log = logging.getLogger("backtest.combine")
            # NOTE: deliberately NO ``llm_reviewer`` attribute → the
            # ``hasattr(self, 'llm_reviewer')`` gate in _combine_signals is
            # False, so the LLM layer is skipped (non-deterministic, no API).

    # ── data loading ───────────────────────────────────────────────────────
    def _cache_path(self, symbol: str, timeframe: str) -> Path:
        safe = symbol.replace("/", "_")
        return self.data_dir / f"{safe}_{timeframe}.csv"

    def fetch_history(self, symbol: str, timeframe: str, bars: int) -> Optional[pd.DataFrame]:
        """Fetch `bars` candles via ccxt, paginating backward (Bitfinex caps a
        single request at ~1500). Cached to backtest/data for repeatability."""
        path = self._cache_path(symbol, timeframe)
        if path.exists():
            df = pd.read_csv(path)
            if "timestamp" in df.columns:
                df = df.set_index("timestamp")
            if len(df) >= bars * 0.9:
                log.info("Loaded cached %d %s %s candles from %s",
                         len(df), symbol, timeframe, path)
                return df

        import ccxt
        ex = ccxt.bitfinex({"enableRateLimit": True})
        tf_ms = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600,
                 "4h": 14400, "1d": 86400}[timeframe] * 1000
        cursor = int((time.time() * 1000) - bars * tf_ms)
        rows: Dict[int, list] = {}
        now_ms = int(time.time() * 1000)
        for _ in range(20):  # safety cap on pages
            batch = ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1500)
            if not batch:
                break
            for r in batch:
                rows[r[0]] = r
            cursor = batch[-1][0] + tf_ms
            log.info("Fetched %s %s: %d total so far", symbol, timeframe, len(rows))
            if batch[-1][0] >= now_ms - tf_ms:
                break
            time.sleep(ex.rateLimit / 1000.0)
        if not rows:
            log.error("No history fetched for %s", symbol)
            return None
        ordered = sorted(rows.values())
        df = pd.DataFrame(ordered, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = df.set_index("timestamp")
        df.to_csv(path)
        log.info("Saved %d %s %s candles to %s", len(df), symbol, timeframe, path)
        return df

    # ── ML: train once on the in-sample prefix (no lookahead) ───────────────
    def _train_ml_walk_forward(self, symbol: str, df_full: pd.DataFrame) -> int:
        """Train the ML model ONLY on the first ``train_bars`` candles. The test
        replay starts AFTER that prefix, so ML never trains on test-window bars.

        Returns the integer index in df_full where the test window begins.
        We give the ML predictor a DatetimeIndex copy (its prepare_features uses
        hour-of-day features when the index is a DatetimeIndex).

        NOTE: this is a single fixed in-sample/out-of-sample split, not rolling
        re-training. Live re-trains every ~24h; a fixed split is the honest,
        conservative choice (the model only gets STALER over the test window,
        never fresher with future data).
        """
        start = self.train_bars
        if len(df_full) <= start + 50:
            start = max(50, len(df_full) // 3)
        train_df = df_full.iloc[:start].copy()
        train_df.index = pd.to_datetime(train_df.index, unit="ms")
        try:
            res = self.ml_predictor.train(train_df, symbol=symbol)
            log.info("[%s] ML train: %s (acc=%.3f)", symbol,
                     res.get("status"), res.get("accuracy", 0.0) or 0.0)
        except Exception as exc:
            log.warning("[%s] ML train failed: %s — ML will return HOLD", symbol, exc)
        return start

    # ── one decision at bar i (NO LOOKAHEAD) ────────────────────────────────
    def _decide(self, shim, symbol: str, window_1h: pd.DataFrame) -> Dict[str, Any]:
        """Reproduce TradingBot.analyze_market for a single trailing window.

        ``window_1h`` is candles[: i+1] — only data up to and including the bar
        that just closed. We approximate the multi-timeframe inputs by using the
        same 1h window for 5m/15m (we backtest on 1h bars; the 5m/15m strategies
        then operate on 1h-derived TA, which is conservative — scalping rarely
        fires). The 1h path (trend_following, grid, ML) is exact.
        """
        from main import TradingBot

        if window_1h is None or len(window_1h) < 50:
            return {"signal": "HOLD", "confidence": 0.0, "current_price": 0.0}

        # Feed a DatetimeIndex copy to TA/ML (ML uses hour features).
        w = window_1h.copy()
        w.index = pd.to_datetime(w.index, unit="ms")

        ta_1h_raw = self.analyzer.analyze(w, "1h")
        ta_5m_raw = self.analyzer.analyze(w, "5m")
        ta_15m_raw = self.analyzer.analyze(w, "15m")

        flatten = TradingBot._flatten_ta
        ta_1h = flatten(shim, ta_1h_raw)
        ta_5m = flatten(shim, ta_5m_raw)
        ta_15m = flatten(shim, ta_15m_raw)

        ml_signal = self.ml_predictor.predict(w, symbol=symbol)

        strategy_signals = self.strategies.get_signals(
            ta_1h, ta_5m, ta_15m, ml_signal, symbol=symbol, df_1h=w)

        # EXACT production combination logic (unbound method on the shim).
        final = TradingBot._combine_signals(
            shim, strategy_signals, ml_signal, ta_1h, symbol=symbol)
        final["symbol"] = symbol
        return final

    # ── exit simulation for an open position over subsequent bars ────────────
    def _check_exit(self, pos: dict, bar: pd.Series) -> Optional[dict]:
        """Check SL/TP/trailing against ONE future bar using intrabar high/low.

        Mirrors risk.update_position semantics: SL and TP are price levels; a
        long is stopped if bar.low <= SL and takes profit if bar.high >= TP. We
        check the STOP first (conservative: if a bar's range spans both, assume
        the adverse side filled first). Trailing is ratcheted via
        risk.get_trailing_stop using the bar CLOSE (same as live, which polls
        with the latest price), then re-applied on later bars.
        """
        side = pos["side"]
        entry = pos["entry_price"]
        amount = pos["amount"]
        sl = pos.get("stop_loss", 0.0)
        tp = pos.get("take_profit", 0.0)

        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])

        # --- Stop-loss (checked first — conservative) ---
        if sl > 0:
            if side == "buy" and low <= sl:
                return {"close_price": sl, "reason": "Stop-loss"}
            if side == "sell" and high >= sl:
                return {"close_price": sl, "reason": "Stop-loss"}

        # --- Take-profit ---
        if tp > 0:
            if side == "buy" and high >= tp:
                return {"close_price": tp, "reason": "Take-profit"}
            if side == "sell" and low <= tp:
                return {"close_price": tp, "reason": "Take-profit"}

        # --- Trailing stop ratchet (uses bar close, same as live polling) ---
        if pos.get("trailing_stop"):
            computed = self.risk.get_trailing_stop(
                entry, close, side, self.config.get("risk", {}))
            old = pos.get("stop_loss", 0.0) or 0.0
            if old > 0:
                new_sl = max(old, computed) if side == "buy" else min(old, computed)
            else:
                new_sl = computed
            pos["stop_loss"] = new_sl
        return None

    # ── main run ─────────────────────────────────────────────────────────────
    def run(self) -> dict:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        # Quiet the very chatty sub-loggers during replay.
        for noisy in ("TechnicalAnalyzer", "MLPredictor",
                      "risk.manager.RiskManager",
                      "risk.regime_detector.MarketRegimeDetector",
                      "strategies.selector.StrategySelector",
                      "backtest.combine"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        log.info("=" * 70)
        log.info("BACKTEST START — capital=$%.0f, symbols=%s, max_open=%d",
                 self.initial_capital, [s["name"] for s in self.symbols], self.max_open)
        log.info("Fees=%.3f%%/side, slippage=%.3f%%/fill, TF=%s",
                 FEE_RATE * 100, SLIPPAGE_RATE * 100, self.timeframe)

        shim = self._ComboShim(self)

        # 1. Load history + train ML per symbol; find common test-start index.
        data: Dict[str, pd.DataFrame] = {}
        test_start: Dict[str, int] = {}
        for s in self.symbols:
            sym = s["name"]
            df = self.fetch_history(sym, self.timeframe, self.history_bars)
            if df is None or len(df) < self.train_bars + 100:
                log.warning("[%s] insufficient history — skipping", sym)
                continue
            data[sym] = df
            test_start[sym] = self._train_ml_walk_forward(sym, df)

        if not data:
            log.error("No usable data. Aborting.")
            return {}

        # Build a UNIFIED timeline of timestamps across symbols (the test window).
        # We iterate per timestamp so multi-symbol position caps are honored.
        all_ts = sorted(set().union(*[
            set(df.index[test_start[sym]:]) for sym, df in data.items()]))
        log.info("Test window: %d timestamps (%s -> %s)",
                 len(all_ts),
                 datetime.utcfromtimestamp(all_ts[0] / 1000),
                 datetime.utcfromtimestamp(all_ts[-1] / 1000))

        # Pre-index each symbol's df by timestamp -> integer loc for fast slicing.
        loc: Dict[str, Dict[int, int]] = {
            sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in data.items()}

        # 2. Replay state.
        equity = self.initial_capital
        realized = 0.0
        open_positions: Dict[str, dict] = {}   # symbol -> position
        trades: List[Trade] = []
        equity_curve: List[tuple] = []
        daily_pnl = 0.0
        cur_day = None
        paused_today = False

        base_sl = float(self.config.get("risk", {}).get("stop_loss_pct", 2.0))
        base_tp = float(self.config.get("risk", {}).get("take_profit_pct", 4.0))
        trailing_on = bool(self.config.get("risk", {}).get("trailing_stop", False))

        for ts in all_ts:
            day = datetime.utcfromtimestamp(ts / 1000).date()
            if day != cur_day:
                cur_day = day
                daily_pnl = 0.0
                paused_today = False
                self.risk.reset()

            # ---- (A) First, process EXITS on this bar for open positions. ----
            # Exits use the CURRENT bar's high/low (the bar at ts). This bar is
            # in the FUTURE relative to the entry decision (entries happen at a
            # prior bar's close), so no lookahead.
            for sym in list(open_positions.keys()):
                if ts not in loc[sym]:
                    continue
                i = loc[sym][ts]
                bar = data[sym].iloc[i]
                pos = open_positions[sym]
                ex = self._check_exit(pos, bar)
                if ex:
                    cp = ex["close_price"]
                    # apply slippage adverse to us on exit
                    if pos["side"] == "buy":
                        fill = cp * (1 - SLIPPAGE_RATE)
                        gross = (fill - pos["entry_price"]) * pos["amount"]
                    else:
                        fill = cp * (1 + SLIPPAGE_RATE)
                        gross = (pos["entry_price"] - fill) * pos["amount"]
                    exit_fee = fill * pos["amount"] * FEE_RATE
                    net = gross - pos["entry_fee"] - exit_fee
                    pos["_trade"].exit_ts = int(ts)
                    pos["_trade"].exit_price = fill
                    pos["_trade"].reason = ex["reason"]
                    pos["_trade"].gross_pnl = round(gross, 4)
                    pos["_trade"].fees = round(pos["entry_fee"] + exit_fee, 4)
                    pos["_trade"].pnl = round(net, 4)
                    pos["_trade"].bars_held = i - pos["entry_i"]
                    trades.append(pos["_trade"])
                    realized += net
                    daily_pnl += net
                    equity = self.initial_capital + realized
                    if net < 0:
                        self.risk.record_loss()
                    else:
                        self.risk.record_win()
                    del open_positions[sym]

            # Daily-loss circuit breaker: flatten + pause for the rest of the day.
            if not paused_today and daily_pnl <= -self.max_daily_loss:
                for sym in list(open_positions.keys()):
                    if ts not in loc[sym]:
                        continue
                    i = loc[sym][ts]
                    close = float(data[sym].iloc[i]["close"])
                    pos = open_positions[sym]
                    if pos["side"] == "buy":
                        fill = close * (1 - SLIPPAGE_RATE)
                        gross = (fill - pos["entry_price"]) * pos["amount"]
                    else:
                        fill = close * (1 + SLIPPAGE_RATE)
                        gross = (pos["entry_price"] - fill) * pos["amount"]
                    exit_fee = fill * pos["amount"] * FEE_RATE
                    net = gross - pos["entry_fee"] - exit_fee
                    pos["_trade"].exit_ts = int(ts)
                    pos["_trade"].exit_price = fill
                    pos["_trade"].reason = "DailyLossBreaker"
                    pos["_trade"].gross_pnl = round(gross, 4)
                    pos["_trade"].fees = round(pos["entry_fee"] + exit_fee, 4)
                    pos["_trade"].pnl = round(net, 4)
                    pos["_trade"].bars_held = i - pos["entry_i"]
                    trades.append(pos["_trade"])
                    realized += net
                    daily_pnl += net
                    del open_positions[sym]
                paused_today = True

            equity = self.initial_capital + realized
            equity_curve.append((int(ts), round(equity, 2)))

            if paused_today:
                continue

            # ---- (B) Then, make ENTRY decisions for each symbol at this bar. ----
            # Risk gate (consecutive losses / cooldown / daily loss) — same as live.
            risk_ok, _ = self.risk.check_trade_allowed(daily_pnl)
            if not risk_ok:
                continue

            shim.daily_pnl = daily_pnl

            for s in self.symbols:
                sym = s["name"]
                if sym not in data or ts not in loc[sym]:
                    continue
                if sym in open_positions:
                    continue                      # never stack on a held symbol
                if len(open_positions) >= self.max_open:
                    break                         # portfolio exposure cap

                i = loc[sym][ts]
                if i < 50:
                    continue
                # NO-LOOKAHEAD: window is candles[: i+1] — bar i just closed.
                window = data[sym].iloc[: i + 1]
                decision = self._decide(shim, sym, window)
                signal = decision.get("signal", "HOLD")
                if signal not in ("BUY", "SELL"):
                    continue

                price = float(data[sym].iloc[i]["close"])
                if price <= 0:
                    continue

                # --- Regime-adapted SL/TP + size mult (same as live) ---
                regime_params = None
                regime_str = "RANGING"
                try:
                    if len(window) > 30:
                        regime = self.regime_detector.detect(window.copy())
                        regime_str = regime.value
                        regime_params = self.regime_detector.get_adapted_params(
                            regime, base_sl, base_tp)
                except Exception:
                    pass
                sl_pct = regime_params["stop_loss_pct"] if regime_params else base_sl
                tp_pct = regime_params["take_profit_pct"] if regime_params else base_tp
                size_mult = regime_params["position_size_mult"] if regime_params else 1.0

                # --- Position sizing (EXACT live call) ---
                pair_capital = s.get("pair_capital", self.initial_capital)
                effective_capital = pair_capital + (daily_pnl / max(len(self.symbols), 1))
                amount = self.risk.calculate_position_size(
                    capital=effective_capital, price=price,
                    confidence=decision["confidence"],
                    signal_type=signal, regime_position_mult=size_mult)
                if amount <= 0:
                    continue
                # exchange min sizes (same as live)
                base = sym.split("/")[0]
                min_size = {"SOL": 0.02, "BTC": 0.0001, "ETH": 0.001}.get(base, 0.0001)
                if amount < min_size:
                    amount = min_size

                side = signal.lower()
                # entry fill with adverse slippage + entry fee
                if side == "buy":
                    entry_fill = price * (1 + SLIPPAGE_RATE)
                    stop_loss = entry_fill * (1 - sl_pct / 100.0)
                    take_profit = entry_fill * (1 + tp_pct / 100.0)
                else:
                    entry_fill = price * (1 - SLIPPAGE_RATE)
                    stop_loss = entry_fill * (1 + sl_pct / 100.0)
                    take_profit = entry_fill * (1 - tp_pct / 100.0)
                entry_fee = entry_fill * amount * FEE_RATE

                tr = Trade(
                    symbol=sym, side=side, entry_ts=int(ts),
                    entry_price=entry_fill, amount=amount,
                    confidence=round(decision["confidence"], 4), regime=regime_str)
                open_positions[sym] = {
                    "side": side, "entry_price": entry_fill, "amount": amount,
                    "stop_loss": stop_loss, "take_profit": take_profit,
                    "trailing_stop": trailing_on, "entry_fee": entry_fee,
                    "entry_i": i, "_trade": tr,
                }

        # 3. Close any still-open positions at the last available bar (mark-out).
        for sym, pos in list(open_positions.items()):
            i = len(data[sym]) - 1
            close = float(data[sym].iloc[i]["close"])
            if pos["side"] == "buy":
                fill = close * (1 - SLIPPAGE_RATE)
                gross = (fill - pos["entry_price"]) * pos["amount"]
            else:
                fill = close * (1 + SLIPPAGE_RATE)
                gross = (pos["entry_price"] - fill) * pos["amount"]
            exit_fee = fill * pos["amount"] * FEE_RATE
            net = gross - pos["entry_fee"] - exit_fee
            pos["_trade"].exit_ts = int(data[sym].index[i])
            pos["_trade"].exit_price = fill
            pos["_trade"].reason = "EndOfData"
            pos["_trade"].gross_pnl = round(gross, 4)
            pos["_trade"].fees = round(pos["entry_fee"] + exit_fee, 4)
            pos["_trade"].pnl = round(net, 4)
            pos["_trade"].bars_held = i - pos["entry_i"]
            trades.append(pos["_trade"])
            realized += net

        return self._report(trades, equity_curve, realized, all_ts)

    # ── reporting ────────────────────────────────────────────────────────────
    def _report(self, trades: List[Trade], equity_curve, realized, all_ts) -> dict:
        n = len(trades)
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        win_rate = len(wins) / n if n else 0.0
        gross_win = sum(t.pnl for t in wins)
        gross_loss = -sum(t.pnl for t in losses)
        avg_win = gross_win / len(wins) if wins else 0.0
        avg_loss = gross_loss / len(losses) if losses else 0.0
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
        total_return_pct = realized / self.initial_capital * 100.0

        # Max drawdown on the equity curve.
        peak = self.initial_capital
        max_dd = 0.0
        for _, eq in equity_curve:
            peak = max(peak, eq)
            dd = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
            max_dd = max(max_dd, dd)

        # Span in days for avg daily PnL.
        span_days = max(1.0, (all_ts[-1] - all_ts[0]) / 1000.0 / 86400.0)
        avg_daily_pnl = realized / span_days
        avg_daily_pct = avg_daily_pnl / self.initial_capital * 100.0

        total_fees = sum(t.fees for t in trades)

        rep = {
            "initial_capital": self.initial_capital,
            "final_equity": round(self.initial_capital + realized, 2),
            "total_return_pct": round(total_return_pct, 3),
            "realized_pnl": round(realized, 2),
            "num_trades": n,
            "win_rate": round(win_rate, 4),
            "wins": len(wins),
            "losses": len(losses),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else "inf",
            "max_drawdown_pct": round(max_dd, 3),
            "total_fees": round(total_fees, 2),
            "span_days": round(span_days, 1),
            "avg_daily_pnl": round(avg_daily_pnl, 2),
            "avg_daily_pct": round(avg_daily_pct, 4),
        }

        out = self.data_dir / "last_run_report.json"
        with open(out, "w") as f:
            json.dump({"summary": rep, "trades": [asdict(t) for t in trades]}, f, indent=2)

        # Pretty print.
        print("\n" + "=" * 70)
        print("BACKTEST RESULTS")
        print("=" * 70)
        print(f"  Period:            {round(rep['span_days'],1)} days "
              f"({datetime.utcfromtimestamp(all_ts[0]/1000).date()} -> "
              f"{datetime.utcfromtimestamp(all_ts[-1]/1000).date()})")
        print(f"  Initial capital:   ${rep['initial_capital']:,.2f}")
        print(f"  Final equity:      ${rep['final_equity']:,.2f}")
        print(f"  Total return:      {rep['total_return_pct']:+.2f}%")
        print(f"  Realized PnL:      ${rep['realized_pnl']:+,.2f}")
        print(f"  Trades:            {rep['num_trades']}  "
              f"(W:{rep['wins']}  L:{rep['losses']})")
        print(f"  Win rate:          {rep['win_rate']*100:.1f}%")
        print(f"  Avg win:           ${rep['avg_win']:+,.2f}")
        print(f"  Avg loss:          ${rep['avg_loss']:+,.2f}")
        print(f"  Profit factor:     {rep['profit_factor']}")
        print(f"  Max drawdown:      {rep['max_drawdown_pct']:.2f}%")
        print(f"  Total fees paid:   ${rep['total_fees']:,.2f}")
        print(f"  Avg daily PnL:     ${rep['avg_daily_pnl']:+,.2f}  "
              f"({rep['avg_daily_pct']:+.3f}%/day)")
        print("=" * 70)
        print(f"  (Full report + trade log: {out})")
        print("=" * 70 + "\n")

        log.info("Backtest complete: %s", rep)
        return rep


class _NullSentiment:
    """Stand-in sentiment object (sentiment is disabled in backtest)."""
    _last_score = 0.0
    _last_label = "Neutral"

    def get_signal_filter(self, coin, signal, confidence):
        return signal, confidence, "disabled"
