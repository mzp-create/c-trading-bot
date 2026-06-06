"""
Strategy Selector — Hermes Crypto Trading Bot.

Manages and combines multiple trading strategies:
  - TrendFollowingStrategy  (1h timeframe, EMA crossovers)
  - ScalpingStrategy        (5m timeframe, MACD/BB/ATR)
  - GridStrategy            (15m timeframe, mean reversion)
  - EnsembleStrategy        (ML + RL ensemble)

Each strategy implements a `generate()` method that returns a signal dict.
"""

import math
import logging
from typing import Dict, List, Any, Optional, Tuple

# Import ensemble strategy
try:
    from strategies.ensemble_bot_integration import EnsembleBotStrategy as EnsembleStrategy
    ENSEMBLE_AVAILABLE = True
except ImportError:
    ENSEMBLE_AVAILABLE = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Strategy Selector
# ---------------------------------------------------------------------------

class StrategySelector:
    """Manages enabled strategies and produces combined signal list.
    
    Supports direction filtering for dual-instance trading:
    - "long": Only allow BUY signals (SELL becomes HOLD)
    - "short": Only allow SELL signals (BUY becomes HOLD)  
    - "both": Allow all signals (default, backward compatible)
    """

    def __init__(self, config: dict, trade_direction: str = "both"):
        self.config = config
        self.strategies_config = config.get("strategies", {})
        self.trade_direction = trade_direction.lower()
        self._log = logging.getLogger(f"{__name__}.StrategySelector")
        self._log.info(f"StrategySelector initialised (direction={trade_direction})")

        # Validate direction
        if self.trade_direction not in ("long", "short", "both"):
            self._log.warning(f"Invalid trade_direction '{trade_direction}', defaulting to 'both'")
            self.trade_direction = "both"

        # Instantiate all supported strategies
        self._strategies: Dict[str, object] = {
            "trend_following": TrendFollowingStrategy(self.strategies_config),
            "scalping": ScalpingStrategy(self.strategies_config),
            "grid": GridStrategy(self.strategies_config),
        }
        
        # Ensemble is per-symbol (each symbol uses its own ML/RL model) and is
        # built lazily on first use — see _get_ensemble. Building one instance
        # for symbols[0] and running it on every symbol would feed the wrong
        # model the wrong data.
        self._ensemble_config = self.strategies_config.get("ensemble", {})
        self._ensembles: Dict[str, Any] = {}
        self._default_symbol = (
            config.get("trading", {}).get("symbols", [{}]) or [{}]
        )[0].get("name", "BTC/USDT")

    def _get_ensemble(self, symbol: str):
        """Lazily build and cache the ensemble for `symbol` so each symbol uses
        its own model. Returns None if ensembles are unavailable or fail to
        load (the bot then runs without the ensemble vote)."""
        if not ENSEMBLE_AVAILABLE:
            return None
        if symbol not in self._ensembles:
            try:
                self._ensembles[symbol] = EnsembleStrategy(
                    symbol=symbol, config=self._ensemble_config)
                self._log.info("✅ Ensemble loaded for %s", symbol)
            except Exception as e:
                self._log.warning("⚠️ Ensemble load failed for %s: %s", symbol, e)
                self._ensembles[symbol] = None
        return self._ensembles[symbol]

    # ── public API ────────────────────────────────────────────────────────

    def get_enabled_strategies(self) -> List[Dict[str, Any]]:
        """Return list of enabled strategy configs with name, weight, params."""
        enabled = []
        cfg = self.strategies_config
        enabled_names = cfg.get("enabled", [])
        for name in enabled_names:
            scfg = cfg.get(name, {})
            if scfg.get("enabled", True):
                enabled.append({
                    "name": name,
                    "weight": float(scfg.get("weight", 0.25)),
                    "shadow": bool(scfg.get("shadow", False)),
                    "params": {k: v for k, v in scfg.items() if k not in ("enabled", "weight")},
                })
        return enabled

    def get_signals(
        self,
        ta_1h: dict,
        ta_5m: dict,
        ta_15m: dict,
        ml_signal: dict,
        symbol: Optional[str] = None,
        df_1h: Optional["pd.DataFrame"] = None,
    ) -> List[Dict[str, Any]]:
        """Return list of signal dicts from each enabled strategy.

        Signals are filtered by trade_direction if set. `symbol`/`df_1h` route
        the real OHLCV window to the per-symbol ensemble; the classic strategies
        consume only the flattened TA dicts.
        """
        signals = []
        symbol = symbol or self._default_symbol
        for entry in self.get_enabled_strategies():
            name = entry["name"]
            shadow = entry.get("shadow", False)
            try:
                if name == "ensemble":
                    strat = self._get_ensemble(symbol)
                    if strat is None:
                        continue
                    signal = strat.generate(ta_1h, ta_5m, ta_15m, ml_signal,
                                            df_1h=df_1h)
                else:
                    strategy = self._strategies.get(name)
                    if strategy is None:
                        self._log.warning("Unknown strategy '%s' — skipping", name)
                        continue
                    signal = strategy.generate(ta_1h, ta_5m, ta_15m, ml_signal)
                signal["name"] = name
                signal["weight"] = entry["weight"]
                signal["shadow"] = shadow
                # Apply direction filtering
                signal = self._filter_by_direction(signal)
                signals.append(signal)
            except Exception as exc:
                self._log.error("Strategy '%s' failed: %s", name, exc)
                signals.append({
                    "name": name,
                    "signal": "HOLD",
                    "confidence": 0.0,
                    "weight": entry["weight"],
                    "shadow": shadow,
                    "reason": f"Error: {exc}",
                    "params": entry["params"],
                })
        return signals

    def _filter_by_direction(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        """Filter signal based on instance trade direction.
        
        - "long": Only allow BUY signals, SELL becomes HOLD
        - "short": Only allow SELL signals, BUY becomes HOLD
        - "both": Allow all signals (no filtering)
        
        Returns modified signal with [BLOCKED:direction] tag in reason.
        """
        if self.trade_direction == "both":
            return signal
        
        sig_type = signal.get("signal", "HOLD")
        if isinstance(sig_type, str):
            sig_type = sig_type.upper()
        else:
            sig_type = "HOLD"
        
        # Long instance: block SELL signals
        if self.trade_direction == "long" and sig_type == "SELL":
            return {
                **signal,
                "signal": "HOLD",
                "reason": signal.get("reason", "") + " | [BLOCKED:long_only]"
            }
        
        # Short instance: block BUY signals
        if self.trade_direction == "short" and sig_type == "BUY":
            return {
                **signal,
                "signal": "HOLD",
                "reason": signal.get("reason", "") + " | [BLOCKED:short_only]"
            }
        
        # Ensure signal key exists
        if "signal" not in signal:
            signal = {**signal, "signal": sig_type}
        
        return signal


# ---------------------------------------------------------------------------
#  Shared helpers
# ---------------------------------------------------------------------------

def _safe_float(value: Any, default: float = 0.0) -> float:
    """Return a float or a default if None/invalid."""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp a value between lo and hi."""
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
#  TrendFollowingStrategy  (1h)
# ---------------------------------------------------------------------------

class TrendFollowingStrategy:
    """EMA-9/21 crossover on 1h timeframe with RSI confirmation.

    Enters on golden cross (EMA9 > EMA21) with RSI > 50.
    Exits on death cross (EMA9 < EMA21) or RSI > 80 (overextended).
    Confidence based on trend strength, volume confirmation, distance from MAs.
    """

    def __init__(self, config: dict):
        self.cfg = config.get("trend_following", {})

    # ── public ────────────────────────────────────────────────────────────

    def generate(
        self,
        ta_1h: dict,
        ta_5m: dict,
        ta_15m: dict,
        ml_signal: dict,
    ) -> Dict[str, Any]:
        if not ta_1h:
            return self._hold("No 1h TA data")

        ema9 = _safe_float(ta_1h.get("ema_9"))
        ema21 = _safe_float(ta_1h.get("ema_21"))
        rsi = _safe_float(ta_1h.get("rsi"))
        price = _safe_float(ta_1h.get("close"))
        volume = _safe_float(ta_1h.get("volume"))
        avg_volume = _safe_float(ta_1h.get("volume_sma", ta_1h.get("volume_avg")))

        if ema9 <= 0 or ema21 <= 0 or price <= 0:
            return self._hold("Indicators not ready")

        golden_cross = ema9 > ema21
        death_cross = ema9 < ema21

        # --- Signal ---
        signal = "HOLD"
        confidence = 0.0
        reasons = []

        if golden_cross and rsi > 50:
            # Potential buy
            if rsi < 80:
                signal = "BUY"
                reasons.append("Golden cross + RSI bullish")
            else:
                reasons.append("Golden cross but RSI overbought")
        elif death_cross or rsi > 80:
            signal = "SELL"
            reasons.append("Death cross or RSI > 80")

        # --- Confidence scoring ---
        if signal != "HOLD":
            # 1. Trend strength: distance between EMAs relative to price
            ema_spread_pct = abs(ema9 - ema21) / price * 100.0
            trend_strength = _clamp(ema_spread_pct / 2.0)  # 0-1, 2% spread = full confidence
            reasons.append(f"spread={ema_spread_pct:.2f}%")

            # 2. Volume confirmation
            vol_ratio = (volume / avg_volume) if avg_volume > 0 else 1.0
            vol_conf = _clamp((vol_ratio - 0.8) / 1.2)  # 0-1
            if vol_ratio > 1.2:
                reasons.append("high_vol")

            # 3. Distance from MAs (trend consistency)
            # If price is above both EMAs → strong uptrend
            if signal == "BUY":
                dist_above_ema21 = (price - ema21) / ema21 * 100.0
                ma_align = 1.0 if price > ema9 > ema21 else 0.5
                reasons.append(f"above_ema21={dist_above_ema21:.2f}%")
            else:
                dist_below_ema21 = (ema21 - price) / ema21 * 100.0
                ma_align = 1.0 if price < ema9 < ema21 else 0.5
                reasons.append(f"below_ema21={dist_below_ema21:.2f}%")

            confidence = _clamp(0.5 * trend_strength + 0.3 * vol_conf + 0.2 * ma_align)

        return {
            "signal": signal,
            "confidence": round(confidence, 4),
            "reason": " | ".join(reasons) if reasons else ("HOLD", ""),
            "params": {
                "ema_fast": self.cfg.get("ema_fast", 9),
                "ema_slow": self.cfg.get("ema_slow", 21),
                "rsi_period": self.cfg.get("rsi_period", 14),
            },
        }

    # ── helpers ───────────────────────────────────────────────────────────

    def _hold(self, reason: str) -> dict:
        return {
            "signal": "HOLD",
            "confidence": 0.0,
            "reason": reason,
            "params": {},
        }


# ---------------------------------------------------------------------------
#  ScalpingStrategy  (5m)
# ---------------------------------------------------------------------------

class ScalpingStrategy:
    """High-frequency scalping on 5m timeframe.

    Uses:
      - MACD histogram turning positive → BUY, negative → SELL
      - Bollinger Band squeeze → prepare for breakout
      - Price breaking above/below BB with volume → directional move
      - ATR for stop placement
    Lower confidence but higher frequency. Targets 0.5-1.5% moves.
    """

    def __init__(self, config: dict):
        self.cfg = config.get("scalping", {})

    def generate(
        self,
        ta_1h: dict,
        ta_5m: dict,
        ta_15m: dict,
        ml_signal: dict,
    ) -> Dict[str, Any]:
        if not ta_5m:
            return self._hold("No 5m TA data")

        price = _safe_float(ta_5m.get("close"))
        macd_hist = _safe_float(ta_5m.get("macd_histogram", ta_5m.get("macd_hist")))
        macd_prev = _safe_float(ta_5m.get("macd_histogram_prev", ta_5m.get("macd_hist_prev")))
        bb_upper = _safe_float(ta_5m.get("bb_upper"))
        bb_lower = _safe_float(ta_5m.get("bb_lower"))
        bb_mid = _safe_float(ta_5m.get("bb_mid", ta_5m.get("bb_ma")))
        bb_width = _safe_float(ta_5m.get("bb_width"))
        bb_prev_width = _safe_float(ta_5m.get("bb_prev_width"))
        atr = _safe_float(ta_5m.get("atr"))
        volume = _safe_float(ta_5m.get("volume"))
        avg_volume = _safe_float(ta_5m.get("volume_sma", ta_5m.get("volume_avg")))

        if price <= 0:
            return self._hold("No price data")

        signal = "HOLD"
        confidence = 0.0
        reasons = []

        # 1. MACD histogram turning positive/negative
        macd_bullish = macd_hist > 0 and macd_prev <= 0
        macd_bearish = macd_hist < 0 and macd_prev >= 0
        macd_positive = macd_hist > 0
        macd_negative = macd_hist < 0

        # 2. Bollinger Band squeeze
        squeeze = False
        if bb_width > 0 and bb_prev_width > 0:
            squeeze = bb_width < bb_prev_width * 0.9  # bands narrowing by 10%+
        if squeeze:
            reasons.append("BB_squeeze")

        # 3. Price breaking BB bands
        bb_breakout_up = bb_upper > 0 and price > bb_upper
        bb_breakout_dn = bb_lower > 0 and price < bb_lower

        vol_surge = avg_volume > 0 and (volume / avg_volume) > 1.5

        # --- Decision logic ---
        # BUY: MACD turns positive, or price breaks above BB with volume
        if macd_bullish or (macd_positive and bb_breakout_up and vol_surge):
            signal = "BUY"
            reasons.append("MACD_bullish" if macd_bullish else "BB_breakout_up")
            if vol_surge:
                reasons.append("vol_surge")

            # Confidence
            vol_conf = _clamp((volume / avg_volume - 1.0) / 2.0) if avg_volume > 0 else 0.3
            macd_conf = _clamp(abs(macd_hist) / (_safe_float(ta_5m.get("macd")) or 0.01) * 0.5) if macd_hist != 0 else 0.0
            confidence = _clamp(0.4 + 0.3 * vol_conf + 0.3 * macd_conf)

        # SELL: MACD turns negative, or price breaks below BB with volume
        elif macd_bearish or (macd_negative and bb_breakout_dn and vol_surge):
            signal = "SELL"
            reasons.append("MACD_bearish" if macd_bearish else "BB_breakout_dn")
            if vol_surge:
                reasons.append("vol_surge")

            vol_conf = _clamp((volume / avg_volume - 1.0) / 2.0) if avg_volume > 0 else 0.3
            macd_conf = _clamp(abs(macd_hist) / (_safe_float(ta_5m.get("macd")) or 0.01) * 0.5) if macd_hist != 0 else 0.0
            confidence = _clamp(0.4 + 0.3 * vol_conf + 0.3 * macd_conf)

        else:
            # HOLD — but check for MACD direction bias
            if macd_positive:
                reasons.append("MACD_pos")
                confidence = 0.2
            elif macd_negative:
                reasons.append("MACD_neg")
                confidence = 0.2
            else:
                reasons.append("no_signal")

        return {
            "signal": signal,
            "confidence": round(confidence, 4),
            "reason": " | ".join(reasons),
            "params": {
                "atr": round(atr, 2) if atr > 0 else "N/A",
                "bb_width": round(bb_width, 4) if bb_width > 0 else "N/A",
                "squeeze": squeeze,
            },
        }

    def _hold(self, reason: str) -> dict:
        return {
            "signal": "HOLD",
            "confidence": 0.0,
            "reason": reason,
            "params": {},
        }


# ---------------------------------------------------------------------------
#  GridStrategy  (15m)
# ---------------------------------------------------------------------------

class GridStrategy:
    """Mean-reversion grid trading on 15m timeframe.

    Places virtual limit orders at support/resistance levels.
    Buys at support, sells at resistance.
    Works best in ranging markets (ADX < 25).
    Disabled during strong trends (ADX > 30).
    """

    def __init__(self, config: dict):
        self.cfg = config.get("grid", {})

    def generate(
        self,
        ta_1h: dict,
        ta_5m: dict,
        ta_15m: dict,
        ml_signal: dict,
    ) -> Dict[str, Any]:
        if not ta_15m:
            return self._hold("No 15m TA data")

        price = _safe_float(ta_15m.get("close"))
        adx = _safe_float(ta_15m.get("adx"))
        rsi = _safe_float(ta_15m.get("rsi"))
        bb_upper = _safe_float(ta_15m.get("bb_upper"))
        bb_lower = _safe_float(ta_15m.get("bb_lower"))
        bb_mid = _safe_float(ta_15m.get("bb_mid", ta_15m.get("bb_ma")))
        atr = _safe_float(ta_15m.get("atr"))
        volume = _safe_float(ta_15m.get("volume"))
        avg_volume = _safe_float(ta_15m.get("volume_sma", ta_15m.get("volume_avg")))

        if price <= 0:
            return self._hold("No price data")

        signal = "HOLD"
        confidence = 0.0
        reasons = []
        grid_levels = int(self.cfg.get("grid_levels", 10))
        grid_spread_pct = float(self.cfg.get("grid_spread_pct", 0.5))

        # --- Market regime check ---
        if adx > 0:
            if adx > 30:
                reasons.append(f"trending(ADX={adx:.1f})")
                # Strong trend — grid disabled
                return {
                    "signal": "HOLD",
                    "confidence": 0.0,
                    "reason": " | ".join(reasons + ["Grid disabled in strong trend"]),
                    "params": {"adx": round(adx, 1), "regime": "trending"},
                }
            elif adx < 25:
                reasons.append(f"ranging(ADX={adx:.1f})")
            else:
                reasons.append(f"transitional(ADX={adx:.1f})")

        # --- Support / Resistance levels via BB ---
        if bb_upper <= 0 or bb_lower <= 0 or bb_mid <= 0:
            return self._hold("BB indicators not ready")

        # Grid lines between BB lower and BB upper
        bb_range = bb_upper - bb_lower
        if bb_range <= 0:
            return self._hold("Invalid BB range")

        grid_step = bb_range / (grid_levels + 1)

        # Support: price near lower BB or below mid-BB with oversold RSI
        near_support = price <= bb_lower * 1.005  # within 0.5% of lower BB
        near_resistance = price >= bb_upper * 0.995  # within 0.5% of upper BB
        oversold = 0 < rsi < 35
        overbought = rsi > 65

        # --- Signal ---
        if near_support or (oversold and price < bb_mid):
            signal = "BUY"
            reasons.append("near_support" if near_support else "oversold_below_mid")
            # Confidence based on distance from mid-BB and oversold strength
            dist_from_mid = (bb_mid - price) / bb_range
            rsi_conf = _clamp((35 - rsi) / 35) if oversold else 0.3
            confidence = _clamp(0.4 * dist_from_mid + 0.3 * rsi_conf + 0.3)

        elif near_resistance or (overbought and price > bb_mid):
            signal = "SELL"
            reasons.append("near_resistance" if near_resistance else "overbought_above_mid")
            dist_from_mid = (price - bb_mid) / bb_range
            rsi_conf = _clamp((rsi - 65) / 35) if overbought else 0.3
            confidence = _clamp(0.4 * dist_from_mid + 0.3 * rsi_conf + 0.3)

        else:
            # In the middle zone — range-bound
            reasons.append("mid_range")
            # Suggest mean reversion direction
            if price < bb_mid:
                confidence = 0.15
                reasons.append("tilt_buy")
            else:
                confidence = 0.15
                reasons.append("tilt_sell")

        # Scale down confidence if ADX suggests trend is gaining strength
        if adx > 0 and adx >= 25:
            confidence *= 0.5
            reasons.append("adx_warning")

        # Build grid level info for params
        grid_lines = []
        for i in range(1, grid_levels + 1):
            level_price = bb_lower + i * grid_step
            grid_lines.append(round(level_price, 2))

        return {
            "signal": signal,
            "confidence": round(confidence, 4),
            "reason": " | ".join(reasons),
            "params": {
                "adx": round(adx, 1) if adx > 0 else "N/A",
                "grid_levels": grid_levels,
                "grid_spread_pct": grid_spread_pct,
                "support_level": round(bb_lower, 2),
                "resistance_level": round(bb_upper, 2),
                "grid_lines": grid_lines[:5],  # include first 5 for reference
            },
        }

    def _hold(self, reason: str) -> dict:
        return {
            "signal": "HOLD",
            "confidence": 0.0,
            "reason": reason,
            "params": {},
        }
