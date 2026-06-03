"""
Risk Manager — Hermes Crypto Trading Bot.

Handles:
  - Trade allowance checks (max daily loss, drawdown, consecutive losses, cooldown)
  - Dynamic Kelly-based position sizing
  - Stop-loss / take-profit / trailing stop updates
  - Volatility-adaptive polling interval
  - Kelly criterion computation
"""

import math
import time
import logging
from typing import Tuple, Optional, Dict, Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Risk Manager
# ---------------------------------------------------------------------------

class RiskManager:
    """Central risk management for the trading bot."""

    def __init__(self, config: dict):
        self.config = config
        self.risk_config = config.get("risk", {})
        self.trading_config = config.get("trading", {})

        self._log = logging.getLogger(f"{__name__}.RiskManager")
        self._log.info("RiskManager initialised")

        # Internal state
        self._last_loss_timestamp: float = 0.0
        self._consecutive_losses: int = 0

    # ── public API ────────────────────────────────────────────────────────

    def check_trade_allowed(self, daily_pnl: float) -> Tuple[bool, str]:
        """Check if trading is currently allowed.

        Returns
        -------
        (allowed, reason)
            allowed: True if a trade may be opened.
            reason:  Human-readable explanation.
        """
        # 1. Max daily loss
        max_daily_loss = abs(float(self.risk_config.get("max_daily_loss", 20.0)))
        if daily_pnl <= -max_daily_loss:
            return (
                False,
                f"Max daily loss reached: ${daily_pnl:.2f} (limit: ${max_daily_loss:.2f})",
            )

        # 2. Max drawdown (compared to initial capital)
        initial_capital = float(self.trading_config.get("initial_capital", 100.0))
        max_drawdown_pct = float(
            self.risk_config.get("max_drawdown_pct", 15.0)
        )
        drawdown_pct = (
            abs(daily_pnl) / initial_capital * 100.0 if initial_capital > 0 else 0.0
        )
        if drawdown_pct >= max_drawdown_pct:
            return (
                False,
                f"Max drawdown reached: {drawdown_pct:.1f}% (limit: {max_drawdown_pct:.1f}%)",
            )

        # 3. Consecutive losses (adaptive threshold based on max_drawdown)
        max_consecutive_losses = max(
            3, int(max_drawdown_pct / 3.0)
        )  # e.g. 15% → 5
        if self._consecutive_losses >= max_consecutive_losses:
            return (
                False,
                f"Too many consecutive losses: {self._consecutive_losses} "
                f"(limit: {max_consecutive_losses})",
            )

        # 4. Cooldown after a loss
        cooldown_seconds = float(
            self.risk_config.get("cooldown_after_loss", 300)
        )
        if self._consecutive_losses > 0:
            elapsed = time.time() - self._last_loss_timestamp
            if elapsed < cooldown_seconds:
                remaining = int(cooldown_seconds - elapsed)
                return (
                    False,
                    f"Cooling down after loss: {remaining}s remaining",
                )

        return (True, "Trade allowed")

    def calculate_position_size(
        self,
        capital: float,
        price: float,
        confidence: float,
        signal_type: str,
        regime_position_mult: float = 1.0,
    ) -> float:
        """Risk-based position sizing with an explicit, leverage-aware cap.

        SIZING MODEL (single, consistent model — read this before changing it)
        ----------------------------------------------------------------------
        We use **fixed-fractional risk sizing**, NOT "fraction of capital as
        notional". The two ideas are different and were previously conflated,
        which made ``leverage`` a misleading no-op:

          * ``max_risk_per_trade`` is the fraction of *capital* we are willing
            to LOSE on this trade if the stop-loss is hit. It is the loss
            budget, not the position size.
          * ``stop_loss_pct`` is how far (in %) price must move against us to
            hit the stop. Combined, these fix the position notional:

                risk_amount = capital * max_risk_per_trade        # $ we can lose
                notional    = risk_amount / (stop_loss_pct / 100) # $ exposure

            Example: capital=$263, risk=2%, stop=2%  →  risk_amount=$5.27,
            notional = 5.27 / 0.02 = $263. Hitting the 2% stop loses ~$5.27,
            i.e. exactly the 2% risk budget. This is the whole point.

          * ``leverage`` is the MAX margin multiplier. It does NOT inflate the
            risk-based notional above; instead it raises the hard ceiling so
            that leverage genuinely changes how large a position is *allowed*
            to be (the old code clamped to a hardcoded 20% that ignored
            leverage, so 5x and 10x produced identical sizes):

                max_notional = capital * leverage * max_single_pct

            With max_single_pct=0.20 and leverage=5 the cap is 100% of capital;
            leverage=10 → 200%. Changing leverage now visibly changes the cap
            (and therefore the returned size whenever the risk-based notional
            would otherwise exceed it).

        SAFETY GUARDS
        -------------
          * Confidence (0.5x–1.5x) and regime multipliers scale the notional.
          * The notional is clamped to ``max_notional`` (leverage-aware cap).
          * MARGIN/LIQUIDATION GUARD: a leveraged position only locks up
            ``notional / leverage`` as margin. If that required margin exceeds
            the available capital we skip the trade (return 0) — this is the
            check that was missing and that exposes the account to liquidation.

        Parameters
        ----------
        capital : float
            Available capital allocated to this pair/instance (quote currency).
        price : float
            Current asset price.
        confidence : float
            Signal confidence (0.0 – 1.0).
        signal_type : str
            'BUY' or 'SELL' (not used for sizing magnitude; kept for API).
        regime_position_mult : float
            Regime-adjusted size multiplier (0.5 for volatile, 1.0 for trending).

        Returns the **base currency amount** (e.g. BTC) to trade.
        """
        if capital <= 0 or price <= 0:
            return 0.0

        # --- Inputs ---------------------------------------------------------
        max_risk_pct = float(self.trading_config.get("max_risk_per_trade", 0.02))
        # Leverage = max margin multiplier. Default 1.0 (spot / no leverage).
        leverage = max(1.0, float(self.risk_config.get("leverage", 1.0)))
        # Stop-loss distance drives the risk→notional conversion. Guard against
        # a zero/garbage value that would blow the notional up to infinity.
        stop_loss_pct = abs(float(self.risk_config.get("stop_loss_pct", 2.0)))
        if stop_loss_pct <= 0:
            stop_loss_pct = 2.0

        # --- 1. Risk-based notional ----------------------------------------
        # Size so that hitting the stop loses ~ (capital * max_risk_per_trade).
        risk_amount = capital * max_risk_pct
        notional = risk_amount / (stop_loss_pct / 100.0)

        # --- 2. Confidence multiplier (0.5x at conf=0 → 1.5x at conf=1) -----
        confidence = max(0.0, min(1.0, confidence))
        conf_mult = 0.5 + confidence
        notional *= conf_mult

        # --- 3. Regime position size multiplier (volatile shrinks size) ----
        regime_mult = max(0.1, min(2.0, float(regime_position_mult)))
        notional *= regime_mult

        # --- 4. Leverage-aware hard cap ------------------------------------
        # The cap is a function of leverage, so leverage is NOT silently
        # nullified: raising leverage raises the maximum allowed notional.
        max_single_pct = float(self.risk_config.get("max_single_pct", 0.20))
        max_notional = capital * leverage * max_single_pct
        notional = min(notional, max_notional)

        # --- 5. Margin / liquidation guard ---------------------------------
        # A leveraged position requires margin = notional / leverage. If the
        # position we computed would need more margin than we actually have,
        # the position is unsafe (over-leveraged → liquidation risk) — skip it.
        required_margin = notional / leverage
        if required_margin > capital:
            self._log.warning(
                "Required margin $%.2f exceeds available capital $%.2f "
                "(notional $%.2f, leverage %.1fx) - skipping trade",
                required_margin, capital, notional, leverage,
            )
            return 0.0

        # --- 6. Convert to base-currency amount ----------------------------
        amount = notional / price

        # Minimum amount check (avoid dust trades)
        min_amount = 0.00001  # ~$0.10 at $10k BTC
        if amount < min_amount:
            return 0.0

        # Minimum position value check (no micro-positions)
        min_position_value_usd = float(
            self.trading_config.get("min_position_value_usd", 50.0)
        )
        actual_value = amount * price
        if actual_value < min_position_value_usd:
            self._log.warning(
                "Position size $%.2f below minimum $%.2f - skipping trade",
                actual_value, min_position_value_usd
            )
            return 0.0

        return round(amount, 8)

    def update_position(
        self,
        position: dict,
        current_price: float,
    ) -> Dict[str, Any]:
        """Update stop-loss/take-profit/trailing stop for a position.

        Parameters
        ----------
        position : dict
            Must contain: entry_price, amount, side ('buy'/'sell'),
            stop_loss, take_profit. May contain: trailing_stop,
            highest_price.
        current_price : float
            Current market price.

        Returns
        -------
        dict
            If the position should be closed::
                {'closed': True, 'pnl': float, 'reason': str}
            Otherwise::
                {'closed': False, 'updated_stop': float, ...}
        """
        entry = float(position.get("entry_price", 0))
        amount = float(position.get("amount", 0))
        side = position.get("side", "buy")

        if entry <= 0 or current_price <= 0 or amount <= 0:
            return {"closed": False, "reason": "Invalid position data"}

        pnl_pct = (current_price - entry) / entry * 100.0
        if side == "sell":
            pnl_pct = -pnl_pct

        # --- Stop-loss check ---
        sl = float(position.get("stop_loss", 0))
        if sl > 0:
            hit = False
            if side == "buy" and current_price <= sl:
                hit = True
            elif side == "sell" and current_price >= sl:
                hit = True
            if hit:
                pnl = (
                    (sl - entry) * amount if side == "buy"
                    else (entry - sl) * amount
                )
                return {
                    "closed": True,
                    "pnl": round(pnl, 2),
                    "reason": f"Stop-loss hit at {sl:.2f} (entry: {entry:.2f})",
                    "close_price": sl,
                }

        # --- Take-profit check ---
        tp = float(position.get("take_profit", 0))
        if tp > 0:
            hit = False
            if side == "buy" and current_price >= tp:
                hit = True
            elif side == "sell" and current_price <= tp:
                hit = True
            if hit:
                pnl = (tp - entry) * amount if side == "buy" else (entry - tp) * amount
                return {
                    "closed": True,
                    "pnl": round(pnl, 2),
                    "reason": f"Take-profit hit at {tp:.2f} (entry: {entry:.2f})",
                    "close_price": tp,
                }

        # --- Trailing stop ---
        trailing = position.get("trailing_stop", False)
        if trailing:
            new_stop = self.get_trailing_stop(entry, current_price, side, self.risk_config)
            # Update position with new trailing stop
            position["stop_loss"] = new_stop
            return {
                "closed": False,
                "updated_stop": new_stop,
                "pnl_pct": round(pnl_pct, 2),
                "reason": "Trailing stop updated",
            }

        return {
            "closed": False,
            "reason": "Position open",
            "pnl_pct": round(pnl_pct, 2),
        }

    def get_trailing_stop(
        self,
        entry_price: float,
        current_price: float,
        side: str,
        config: Optional[dict] = None,
    ) -> float:
        """Calculate trailing stop price.

        Parameters
        ----------
        entry_price : float
            Position entry price.
        current_price : float
            Current market price.
        side : str
            'buy' (long) or 'sell' (short).
        config : dict, optional
            Risk config dict. Falls back to ``self.risk_config``.

        Returns
        -------
        float
            New trailing stop price.
        """
        if config is None:
            config = self.risk_config

        activation_pct = float(config.get("trailing_stop_activation", 2.0))
        distance_pct = float(config.get("trailing_stop_distance", 0.5))

        if side == "buy":
            pnl_pct = (current_price - entry_price) / entry_price * 100.0
            if pnl_pct >= activation_pct:
                # Trail from highest price seen
                stop_distance = current_price * (distance_pct / 100.0)
                return round(current_price - stop_distance, 8)
            else:
                # Not yet activated — use fixed stop-loss (convert % to price)
                sl_pct = float(config.get("stop_loss_pct", 2.0))
                return round(entry_price * (1.0 - sl_pct / 100.0), 8)
        else:  # sell / short
            pnl_pct = (entry_price - current_price) / entry_price * 100.0
            if pnl_pct >= activation_pct:
                stop_distance = current_price * (distance_pct / 100.0)
                return round(current_price + stop_distance, 8)
            else:
                sl_pct = float(config.get("stop_loss_pct", 2.0))
                return round(entry_price * (1.0 + sl_pct / 100.0), 8)

    def get_dynamic_interval(self) -> int:
        """Return sleep interval in seconds based on market volatility.

        Higher volatility → check more often (shorter interval).
        Uses the risk config stop-loss as a rough proxy, and returns
        an interval between 30s and 300s.

        Returns
        -------
        int
            Seconds to sleep between trade cycles.
        """
        stop_loss_pct = abs(float(self.risk_config.get("stop_loss_pct", 2.0)))

        # Higher stop-loss % implies higher tolerance → lower vol → longer interval
        # Map stop_loss_pct [0.5, 5.0] → interval [30, 300]
        # stop_loss 0.5% → 30s (very tight, high vol)
        # stop_loss 5.0% → 300s (loose, low vol)

        clamped_sl = max(0.5, min(5.0, stop_loss_pct))
        # Linear interpolation
        interval = 30 + (clamped_sl - 0.5) / (5.0 - 0.5) * (300 - 30)
        interval = int(interval)

        # If we recently had a loss, check more frequently
        if self._consecutive_losses > 0 and self._last_loss_timestamp > 0:
            elapsed = time.time() - self._last_loss_timestamp
            if elapsed < 300:  # within 5 minutes of a loss
                interval = max(30, interval // 2)

        return interval

    def get_kelly_fraction(
        self,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
    ) -> float:
        """Compute the Kelly criterion fraction.

        Formula (standard Kelly for gambling with win/loss sizes):
            f = (p * b - q) / b
        where:
            p = win_rate     (probability of winning)
            q = 1 - p        (probability of losing)
            b = |avg_win / avg_loss|   (net odds received)

        For trading, we use the variant:
            f = p - (q / (avg_win / abs(avg_loss)))

        avg_loss is expected as a positive number (the magnitude of loss).
        If avg_win <= 0 or avg_loss <= 0, returns 0 (invalid).

        Returns
        -------
        float
            Kelly fraction (0 to 1). Typically users risk at 0.25 * Kelly
            for safety.
        """
        p = float(win_rate)
        if not (0 < p < 1):
            return 0.0

        avg_win = float(avg_win)
        avg_loss = abs(float(avg_loss))  # ensure positive magnitude

        if avg_win <= 0 or avg_loss <= 0:
            return 0.0

        b = avg_win / avg_loss  # win/loss ratio > 0
        q = 1.0 - p

        kelly = (p * b - q) / b
        return max(0.0, min(1.0, kelly))

    # ── internal state management ─────────────────────────────────────────

    def record_loss(self):
        """Record a losing trade for cooldown/consecutive tracking."""
        self._consecutive_losses += 1
        self._last_loss_timestamp = time.time()
        self._log.info(
            "Loss recorded: consecutive=%d", self._consecutive_losses
        )

    def record_win(self):
        """Record a winning trade (resets consecutive loss counter)."""
        self._consecutive_losses = 0
        self._last_loss_timestamp = 0.0

    def reset(self):
        """Reset risk state (daily counters)."""
        self._consecutive_losses = 0
        self._last_loss_timestamp = 0.0
