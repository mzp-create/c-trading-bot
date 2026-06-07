"""
Market Regime Detector — Hermes Crypto Trading Bot.

Classifies current market conditions into three regimes:

  TRENDING  — Strong directional movement.  Favour trend-following strategies.
  RANGING   — Price oscillates between support/resistance.  Favour mean reversion.
  VOLATILE  — High volatility, wide swings.  Reduce position sizes, widen stops.

Inspired by: "Building a Self-Learning Trading Bot" (Javier Santiago Gastón
de Iriarte Cabrera) — regime-aware risk management that adapts SL/TP and
position sizing based on live market conditions.

Regime is determined by three axes:
  1. Volatility ratio (current vs historical)
  2. ADX (Average Directional Index) — trend strength
  3. Trend consistency (directional movement alignment)
"""

import math
import logging
from enum import Enum
from typing import Tuple, Dict, Any, Optional, List

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Regime Enum
# ---------------------------------------------------------------------------

class MarketRegime(str, Enum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"

    def __str__(self) -> str:
        return self.value


# ---------------------------------------------------------------------------
#  Regime Detector
# ---------------------------------------------------------------------------

class MarketRegimeDetector:
    """Classifies live market regime and returns adapted risk parameters.

    Parameters
    ----------
    config : dict
        Bot configuration dict (``regime_detector`` section used).
    """

    def __init__(self, config: dict):
        rc = config.get("regime_detector", {})
        self._log = logging.getLogger(f"{__name__}.MarketRegimeDetector")
        self._config = config  # kept for correlation data fetching
        self._ohlcv = None     # lazily-built keyless public OHLCV fetcher

        # Detection thresholds
        self._adx_trending_threshold = float(rc.get("adx_trending_threshold", 25))
        self._adx_ranging_threshold = float(rc.get("adx_ranging_threshold", 20))
        self._volatility_ratio_threshold = float(
            rc.get("volatility_ratio_threshold", 1.5)
        )
        self._lookback = int(rc.get("lookback", 50))
        self._trend_lookback = int(rc.get("trend_lookback", 14))

        # Regime-adapted multipliers
        # TRENDING: hold longer, let winners run
        # RANGING: smaller targets, tighter stops
        # VOLATILE: reduce size, widen stops
        self._regime_params: Dict[str, Dict[str, float]] = {
            "TRENDING": {
                "stop_loss_mult": 1.2,       # Wider stops (let trend breathe)
                "take_profit_mult": 1.5,     # Bigger targets (let winners run)
                "position_size_mult": 1.0,   # Full size
            },
            "RANGING": {
                "stop_loss_mult": 0.8,       # Tighter stops
                "take_profit_mult": 0.7,     # Smaller targets
                "position_size_mult": 0.8,   # Reduced size
            },
            "VOLATILE": {
                "stop_loss_mult": 1.5,       # Much wider stops
                "take_profit_mult": 1.0,     # Keep targets normal
                "position_size_mult": 0.5,   # Half size
            },
        }

        # Allow config overrides
        user_overrides = rc.get("regime_params", {})
        for regime, params in user_overrides.items():
            if regime in self._regime_params:
                self._regime_params[regime].update(params)

        self._last_regime: Optional[MarketRegime] = None
        self._log.info(
            "MarketRegimeDetector initialised (ADX trend>=%s, vol>=%s)",
            self._adx_trending_threshold,
            self._volatility_ratio_threshold,
        )

    # ── public API ────────────────────────────────────────────────────────

    def detect(self, df) -> MarketRegime:
        """Classify the current market regime from OHLCV data.

        Uses three axes:
          1. Volatility ratio (recent std vs full std) — catches VOLATILE
          2. Linear regression slope + R² on recent prices — trend strength
          3. Trend consistency (consecutive same-direction moves)

        Parameters
        ----------
        df : pandas.DataFrame
            OHLCV data with at least 'Close' column.

        Returns
        -------
        MarketRegime
            TRENDING, RANGING, or VOLATILE.
        """
        import numpy as np

        # Normalize column names (uppercase internally)
        df.columns = [c.lower() for c in df.columns]
        close = df["close"].values

        n = len(close)
        if n < self._lookback + 14:
            self._log.warning(
                "Not enough data for regime detection (need %d, have %d)",
                self._lookback + 14, n,
            )
            return MarketRegime.RANGING  # safe default

        # 1. Volatility ratio
        recent_returns = self._pct_change(close[-self._lookback:])
        full_returns = self._pct_change(close)
        recent_vol = float(np.std(recent_returns)) if len(recent_returns) > 1 else 0.0
        full_vol = float(np.std(full_returns)) if len(full_returns) > 1 else 0.0
        vol_ratio = (recent_vol / full_vol) if full_vol > 0 else 1.0

        # 2. Linear regression trend strength (slope + R²)
        slope_norm, r_squared = self._linear_trend_strength(
            close[-self._lookback:]
        )

        # 3. Trend consistency
        trend_consistency = self._calculate_trend_consistency(
            close[-self._trend_lookback:]
        )

        # --- Decision logic ---
        is_volatile = vol_ratio > self._volatility_ratio_threshold
        is_strong_trend = r_squared > 0.6 and abs(slope_norm) > 0.0005
        is_weak_trend = r_squared < 0.3 and abs(slope_norm) < 0.0003

        if is_volatile:
            regime = MarketRegime.VOLATILE
        elif is_strong_trend:
            regime = MarketRegime.TRENDING
        elif is_weak_trend:
            regime = MarketRegime.RANGING
        else:
            # Grey zone — use trend consistency + price change as tiebreaker
            price_change_pct = abs(close[-1] - close[-self._trend_lookback]) / close[-self._trend_lookback] * 100
            if price_change_pct > 3.0 or (r_squared > 0.4 and abs(slope_norm) > 0.0004):
                regime = MarketRegime.TRENDING
            else:
                regime = MarketRegime.RANGING

        self._last_regime = regime

        self._log.info(
            "Regime: %s | vol_ratio=%.2f trend_R²=%.2f slope_norm=%.4f "
            "consistency=%.2f",
            regime.value,
            vol_ratio,
            r_squared,
            slope_norm,
            trend_consistency,
        )

        return regime

    def get_adapted_params(
        self,
        regime: MarketRegime,
        base_stop_loss_pct: float,
        base_take_profit_pct: float,
    ) -> Dict[str, float]:
        """Return regime-adjusted risk parameters.

        Parameters
        ----------
        regime : MarketRegime
            Current market regime.
        base_stop_loss_pct : float
            Base stop-loss percentage (e.g. 2.0 for 2%).
        base_take_profit_pct : float
            Base take-profit percentage (e.g. 4.0 for 4%).

        Returns
        -------
        dict
            ``{'stop_loss_pct': float, 'take_profit_pct': float,
                'position_size_mult': float}``
        """
        rp = self._regime_params.get(regime.value, self._regime_params["RANGING"])
        return {
            "stop_loss_pct": round(base_stop_loss_pct * rp["stop_loss_mult"], 2),
            "take_profit_pct": round(base_take_profit_pct * rp["take_profit_mult"], 2),
            "position_size_mult": rp["position_size_mult"],
        }

    def check_correlation(
        self,
        new_asset: str,
        open_positions: List[Dict[str, Any]],
        max_correlation: float = 0.7,
    ) -> Tuple[bool, float]:
        """Check if opening a position on ``new_asset`` would be too correlated
        with existing positions.

        Parameters
        ----------
        new_asset : str
            Symbol name (e.g. ``'BTC/USDT'``).
        open_positions : list of dict
            Each dict must have a ``'symbol'`` key matching exchange symbol or name.
        max_correlation : float
            Maximum allowed pairwise correlation (default 0.7).

        Returns
        -------
        (allowed, highest_correlation)
        """
        if not open_positions:
            return True, 0.0

        import pandas as pd

        # Build list of assets to check
        existing_assets = list(
            dict.fromkeys(
                p.get("symbol", p.get("asset", "")) for p in open_positions
            )
        )
        existing_assets = [a for a in existing_assets if a and a != new_asset]
        if not existing_assets:
            return True, 0.0

        returns_dict = {}
        try:
            for asset in [new_asset] + existing_assets:
                df = self._get_recent_returns(asset, lookback=max(30, self._lookback))
                if df is not None and len(df) > 10:
                    returns_dict[asset] = df["Close"].pct_change().tail(30)
        except Exception as exc:
            self._log.warning("Correlation check failed: %s", exc)
            return True, 0.0

        if len(returns_dict) < 2:
            return True, 0.0

        corr_df = pd.DataFrame(returns_dict).corr()
        new_corrs = corr_df[new_asset].drop(new_asset)

        if new_corrs.empty:
            return True, 0.0

        max_corr = float(new_corrs.abs().max())
        allowed = max_corr < max_correlation

        if not allowed:
            self._log.warning(
                "Correlation limit: %s vs existing max=%.2f (limit=%.2f)",
                new_asset, max_corr, max_correlation,
            )

        return allowed, round(max_corr, 4)

    def get_last_regime(self) -> Optional[MarketRegime]:
        """Return the most recently detected regime."""
        return self._last_regime

    @property
    def current_regime(self) -> Optional[MarketRegime]:
        """Property alias for get_last_regime()."""
        return self._last_regime

    # ── helpers ───────────────────────────────────────────────────────────

    def _pct_change(self, arr) -> list:
        import numpy as np
        if len(arr) < 2:
            return [0.0]
        arr = np.array(arr, dtype=float)
        return list((arr[1:] - arr[:-1]) / arr[:-1])

    def _linear_trend_strength(self, prices) -> tuple:
        """Compute normalized linear regression slope and R².

        Returns
        -------
        (slope_norm, r_squared)
            slope_norm: slope normalized by mean price (approx % change per bar)
            r_squared: goodness of fit (0 to 1)
        """
        import numpy as np
        n = len(prices)
        if n < 5:
            return 0.0, 0.0

        x = np.arange(n, dtype=float)
        y = np.array(prices, dtype=float)

        x_mean = x.mean()
        y_mean = y.mean()

        # Linear regression
        num = np.sum((x - x_mean) * (y - y_mean))
        den = np.sum((x - x_mean) ** 2)
        slope = num / den if den > 0 else 0.0

        # R²
        y_pred = y_mean + slope * (x - x_mean)
        ss_res = np.sum((y - y_pred) ** 2)
        ss_tot = np.sum((y - y_mean) ** 2)
        r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

        # Normalize slope by mean price
        mean_price = y_mean if y_mean > 0 else 1.0
        slope_norm = slope / mean_price

        return round(slope_norm, 6), round(r_squared, 4)

    def _calculate_trend_consistency(self, close_segment) -> float:
        """Score how consistently price moves in one direction.

        Returns 0.0 (random walk) to 1.0 (perfectly directional).
        """
        import numpy as np
        if len(close_segment) < 3:
            return 0.5

        close_segment = np.array(close_segment, dtype=float)
        changes = np.sign(np.diff(close_segment))
        # Count consecutive same-direction moves
        same_dir = np.sum(changes[1:] == changes[:-1])
        total = len(changes) - 1
        return same_dir / total if total > 0 else 0.5

    def _get_recent_returns(self, asset, lookback=30):
        """Fetch recent OHLCV for correlation analysis.

        Uses the keyless public OhlcvFetcher (candles need no auth). A live
        MarketDataCollector must NEVER be built here: it spins up an
        authenticated WsFeed on the shared API key, and constructing one per
        correlation check leaked hundreds of WS connections whose colliding
        nonces caused the 2026-06-07 `10114 nonce: small` storm.
        """
        try:
            if self._ohlcv is None:
                from bitfinex.ohlcv import OhlcvFetcher
                self._ohlcv = OhlcvFetcher()
            return self._ohlcv.fetch_ohlcv(asset, timeframe="1h", limit=lookback)
        except Exception:
            return None
