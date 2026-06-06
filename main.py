#!/usr/bin/env python3
"""
Hermes Crypto Trading Bot — Bitfinex Integration
Main entry point and orchestrator.

Usage:
    python main.py --paper        # Paper trading mode (default)
    python main.py --live         # Live mode (requires API keys)
    python main.py --backtest     # Backtest mode
    python main.py --monitor      # Monitoring dashboard only
"""

import os
import sys
import time
import json
import re
import yaml
import pandas as pd
import logging
import signal
from pathlib import Path
from datetime import datetime, timedelta, date, timezone
from typing import Optional, Dict, Any, List

# Add project root to path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# Load .env file if it exists (for API keys)
def _load_env_file():
    """Load .env file into os.environ."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Handle both formats: KEY=value and export KEY=value
            if line.startswith("export "):
                line = line[7:]  # Remove 'export ' prefix
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")  # Remove quotes
                if key and key not in os.environ:
                    os.environ[key] = val

_load_env_file()

# --- Config helpers ---

def _resolve_env_vars(value):
    """Replace ${VAR_NAME} patterns with environment variable values."""
    import os
    if isinstance(value, str):
        # Simple string replacement without regex (avoids escaping issues)
        for env_name, env_val in sorted(os.environ.items(), key=lambda x: -len(x[0])):
            placeholder = f"${{{env_name}}}"
            if placeholder in value:
                value = value.replace(placeholder, env_val)
        return value
    elif isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_env_vars(v) for v in value]
    return value


def load_config(config_path: str) -> dict:
    """Load YAML config and resolve ${VAR} env var references."""
    with open(ROOT / config_path) as f:
        config = yaml.safe_load(f)
    return _resolve_env_vars(config)


from market_data.collector import MarketDataCollector
from analysis.technical import TechnicalAnalyzer
from analysis.ml_predictor import MLPredictor
from strategies.selector import StrategySelector
from risk.manager import RiskManager
from risk.regime_detector import MarketRegimeDetector, MarketRegime
from analysis.sentiment import SentimentAnalyzer
from analysis.llm_reviewer import LLMReviewer
from analysis.utils import flatten_ta
from execution.engine import ExecutionEngine
from monitoring.logger import BotLogger
from monitoring.telegram_alerts import TelegramNotifier


class TradingBot:
    """Main trading bot orchestrator.
    
    Supports dual-instance trading with direction filtering:
    - Instance "long" only trades BUY signals
    - Instance "short" only trades SELL signals
    """

    def __init__(self, config_path: str = "config/default.yaml", mode: str = "paper", 
                 instance: str = "default"):
        self.config = load_config(config_path)

        self.mode = mode
        self.instance = instance
        # Propagate the instance name into the config so EVERY BitfinexClient
        # built from it (MarketDataCollector, ExecutionEngine) registers the
        # API key under the SAME instance/pid — otherwise the collector's client
        # claims the key as 'default' and the engine's 'long'/'short' claim
        # collides with it (KeyConflictError) on dual-instance startup.
        self.config["instance"] = instance
        self.running = False
        self.paused = False
        self.start_time = None
        self.daily_pnl = 0.0
        self.trade_count = 0
        self.consecutive_losses = 0
        self._last_reset_date: Optional[date] = None
        self._cached_1h_dfs: Dict[str, Optional[pd.DataFrame]] = {}

        # Get trade direction from config or default based on instance name
        self.trade_direction = self.config.get('trading', {}).get('trade_direction', 'both')
        if instance in ('long', 'short'):
            self.trade_direction = instance  # Override from instance name
        
        # Initialize components
        self.logger = BotLogger(self.config)
        self.telegram = TelegramNotifier(self.config)
        self.collector = MarketDataCollector(self.config, mode=mode)
        self.analyzer = TechnicalAnalyzer(self.config)
        self.ml_predictor = MLPredictor(self.config)
        # Pass trade_direction to StrategySelector for signal filtering
        self.strategies = StrategySelector(self.config, trade_direction=self.trade_direction)
        self.risk = RiskManager(self.config)
        self.regime_detector = MarketRegimeDetector(self.config)
        self.sentiment = SentimentAnalyzer(self.config)
        self.llm_reviewer = LLMReviewer(self.config)
        # Pass trade_direction to ExecutionEngine for validation
        self.executor = ExecutionEngine(self.config, mode=mode,
                                        trade_direction=self.trade_direction,
                                        instance=self.instance)

        self.log = self.logger.get_logger("Bot")

        # Load symbol configs — filter enabled=True
        raw_symbols = self.config.get('trading', {}).get('symbols', [])
        if not raw_symbols:
            # Fallback to legacy single-symbol config
            legacy_symbol = self.config.get('trading', {}).get('symbol', 'BTC/USDT')
            raw_symbols = [{
                'name': legacy_symbol,
                'symbol': self.config.get('trading', {}).get('exchange_symbol', legacy_symbol.replace('/', '')),
                'allocation_pct': 100.0,
                'enabled': True
            }]
        self.symbols = [s for s in raw_symbols if s.get('enabled', True)]

        self.initial_capital = float(self.config.get('trading', {}).get('initial_capital', 100.0))

        # Calculate per-pair capital
        for s in self.symbols:
            s['pair_capital'] = self.initial_capital * s.get('allocation_pct', 100.0) / 100.0

        self.log.info(f"🤖 Hermes Trading Bot initialized — Mode: {mode.upper()}")
        self.log.info(f"📊 Total Capital: ${self.initial_capital} | Pairs: {[s['name'] for s in self.symbols]}")
        for s in self.symbols:
            self.log.info(f"   - {s['name']} (exchange: {s['symbol']}) | "
                         f"Allocation: {s.get('allocation_pct', 100)}% = ${s['pair_capital']:.2f}")
        self.log.info(f"🎯 Daily Target: ${self.config.get('trading', {}).get('daily_target', 100)}")
        self.log.info(f"⚙️  Max Positions: {self.config.get('trading', {}).get('max_open_positions', 6)}")

    def _equity_snapshot_values(self):
        """Best-effort (balance, equity) for the per-cycle snapshot.

        Balance = starting capital + realized daily PnL. Equity adds any
        unrealized PnL exposed on open positions (0 when unavailable). This is
        an audit snapshot, refined when the WS account feed lands (Phase 3).
        NOTE (Phase-1): balance = capital + *daily* PnL, so it steps at the
        daily reset; equity == balance until the WS feed supplies unrealized.
        """
        balance = self.initial_capital + self.daily_pnl
        unrealized = 0.0
        try:
            for pos in self.executor.open_positions:
                unrealized += float(pos.get("unrealized_pnl", 0.0) or 0.0)
        except Exception:
            pass
        return round(balance, 2), round(balance + unrealized, 2)

    def _record_decision_signal(self, symbol: str, decision: dict) -> None:
        """Persist one decision signal per symbol per cycle (additive; never
        raises). `_acted`/`_db_order_id` are present only when an order was
        attempted; HOLD/blocked decisions record with acted=False."""
        try:
            from persistence import SignalRecord
            self.executor._repo.record_signal(SignalRecord(
                ts=datetime.now(timezone.utc).isoformat(),
                symbol=symbol,
                decision=str(decision.get("signal", "HOLD")),
                confidence=float(decision.get("confidence", 0.0) or 0.0),
                strategy_breakdown=str(decision.get("reason", "")),
                acted=bool(decision.get("_acted", False)),
                order_id=decision.get("_db_order_id")))
        except Exception as exc:
            self.log.error("Signal record failed: %s", exc)

    def _flatten_ta(self, ta_dict: dict) -> dict:
        """Flatten nested TA dict — delegates to shared utility."""
        return flatten_ta(ta_dict)

    def analyze_market(self, symbol: str = "BTC/USDT", exchange_symbol: str = None) -> Dict[str, Any]:
        """Analyze market data and return trading signals for a given symbol."""
        # 1. Get fresh market data
        df_1h = self.collector.get_ohlcv(symbol, timeframe="1h", limit=200)
        # Cache for reuse by regime detection (P2-13)
        self._cached_1h_dfs[symbol] = df_1h
        df_5m = self.collector.get_ohlcv(symbol, timeframe="5m", limit=200)
        df_15m = self.collector.get_ohlcv(symbol, timeframe="15m", limit=200)

        if df_1h is None or df_5m is None:
            return {"signal": "HOLD", "confidence": 0.0, "reason": "Insufficient data", "symbol": symbol}

        # 2. Technical analysis on all timeframes
        ta_1h_raw = self.analyzer.analyze(df_1h, "1h")
        ta_5m_raw = self.analyzer.analyze(df_5m, "5m")
        ta_15m_raw = self.analyzer.analyze(df_15m, "15m")

        # Flatten nested TA keys so strategies can access e.g. ta["ema_9"]
        ta_1h = self._flatten_ta(ta_1h_raw)
        ta_5m = self._flatten_ta(ta_5m_raw)
        ta_15m = self._flatten_ta(ta_15m_raw)

        # 3. ML prediction
        ml_signal = self.ml_predictor.predict(df_1h, symbol=symbol)
        ml_signal_5m = self.ml_predictor.predict(df_5m, symbol=symbol)

        # 4. Get strategy signals (pass real 1h OHLCV so the ensemble can run)
        strategy_signals = self.strategies.get_signals(
            ta_1h, ta_5m, ta_15m, ml_signal, symbol=symbol, df_1h=df_1h)

        # 5. Combine into final decision
        final = self._combine_signals(strategy_signals, ml_signal, ta_1h, symbol=symbol)
        final['symbol'] = symbol

        # Log analysis summary
        self.log.info(f"[{symbol}] Analysis: {final['signal']} (conf: {final['confidence']:.2f}) | "
                     f"TA: {ta_1h.get('signal','HOLD')} | ML: {ml_signal.get('signal','HOLD')}")

        return final

    def _combine_signals(self, strategy_signals: dict, ml_signal: dict, ta: dict,
                         symbol: str = "") -> Dict:
        """Weighted signal combination."""
        weights = {
            s['name']: s.get('weight', 0.25)
            for s in self.strategies.get_enabled_strategies()
        }

        buy_score = 0.0
        sell_score = 0.0
        total_weight = 0.0
        reasons = []

        for sig in strategy_signals:
            # Shadow strategies are observed only — logged + recorded, but never
            # added to the score, so they cannot move a trade decision.
            if sig.get('shadow'):
                self.log.info(
                    f"[{symbol or sig.get('name')}] SHADOW {sig.get('name')}: "
                    f"{sig.get('signal', 'HOLD')} conf={sig.get('confidence', 0.0):.2f} "
                    f"— observed, not trading"
                )
                reasons.append(
                    f"SHADOW:{sig.get('name')}:{sig.get('signal', 'HOLD')}"
                    f"({sig.get('confidence', 0.0):.2f})"
                )
                continue
            w = weights.get(sig['name'], 0.25)
            total_weight += w
            if sig['signal'] == 'BUY':
                buy_score += w * sig['confidence']
                reasons.append(f"{sig['name']}:BUY({sig['confidence']:.2f})")
            elif sig['signal'] == 'SELL':
                sell_score += w * sig['confidence']
                reasons.append(f"{sig['name']}:SELL({sig['confidence']:.2f})")

        # Add ML signal — weight based on model accuracy (P2-14)
        # Base higher when ML confidence is strong
        ml_weight = 0.35
        try:
            sym = ml_signal.get("symbol", "")
            ml_conf = ml_signal.get("confidence", 0.0)
            if sym:
                acc = self.ml_predictor._training_accuracies.get(sym, 0.55)
                if acc > 0.5:
                    ml_weight = min(0.5, max(0.2, acc - 0.35))
            # Boost ML weight if ML is very confident (>0.70)
            if ml_conf > 0.70:
                ml_weight = min(0.5, ml_weight + 0.10)
        except Exception:
            pass
        if ml_signal['signal'] == 'BUY':
            buy_score += ml_weight * ml_signal['confidence']
            reasons.append(f"ML:BUY({ml_signal['confidence']:.2f})")
        elif ml_signal['signal'] == 'SELL':
            sell_score += ml_weight * ml_signal['confidence']
            reasons.append(f"ML:SELL({ml_signal['confidence']:.2f})")

        total_weight += ml_weight

        # Normalize
        if total_weight > 0:
            buy_score /= total_weight
            sell_score /= total_weight

        # Decision
        confidence_threshold = 0.20
        if buy_score > sell_score and buy_score > confidence_threshold:
            signal = "BUY"
            confidence = buy_score
        elif sell_score > buy_score and sell_score > confidence_threshold:
            signal = "SELL"
            confidence = sell_score
        else:
            return {
                "signal": "HOLD",
                "confidence": max(buy_score, sell_score),
                "reason": " | ".join(reasons) if reasons else "No strong signal",
                "current_price": ta.get('current_price', 0),
            }

        # Layer 2: Sentiment confirmation (applied only to BUY/SELL signals)
        sent_score = 0.0
        sent_label = "Neutral"
        # Bind unconditionally — the LLM-review block below also references
        # symbol_raw, and it runs even when sentiment is disabled. Assigning it
        # only inside the sentiment branch caused an UnboundLocalError that
        # silently disabled LLM review every cycle.
        symbol_raw = ta.get("symbol", ta.get("close_symbol", "")) or "BTC"
        if self.config.get("sentiment", {}).get("enabled", False):
            coin_symbol = symbol_raw.split("/")[0] if "/" in symbol_raw else symbol_raw

            adj_sig, adj_conf, sent_reason = self.sentiment.get_signal_filter(
                coin_symbol, signal, confidence
            )
            sent_score = self.sentiment._last_score if hasattr(self.sentiment, '_last_score') else 0.0
            sent_label = self.sentiment._last_label if hasattr(self.sentiment, '_last_label') else "Neutral"
            reasons.append(f"Sent:{sent_reason}")
            signal = adj_sig
            confidence = adj_conf

            # If sentiment downgraded to HOLD, return HOLD
            if signal == "HOLD":
                return {
                    "signal": "HOLD",
                    "confidence": confidence,
                    "reason": " | ".join(reasons),
                    "current_price": ta.get('current_price', 0),
                }

        # Layer 3: LLM review (only for BUY/SELL signals below high-confidence threshold)
        if signal in ("BUY", "SELL") and hasattr(self, 'llm_reviewer'):
            try:
                # Get TA details for the prompt
                rsi = float(ta.get('rsi', 50))
                adx = float(ta.get('adx', 20))
                macd_hist = float(ta.get('macd_histogram', ta.get('macd_hist', 0)))
                vol_ratio = float(ta.get('volume_ratio', 1.0))
                price_change = float(ta.get('price_change_24h', 0))
                regime_str = "UNKNOWN"
                if hasattr(self, 'regime_detector') and self.regime_detector:
                    try:
                        regime_str = self.regime_detector.current_regime.value if self.regime_detector.current_regime else "RANGING"
                    except:
                        regime_str = "RANGING"

                llm_result = self.llm_reviewer.review_signal(
                    symbol=ta.get('symbol', symbol_raw),
                    combined_signal=signal,
                    combined_confidence=confidence,
                    strategies=strategy_signals,
                    ml_signal=ml_signal,
                    sentiment_score=sent_score,
                    sentiment_label=sent_label,
                    regime=regime_str,
                    price=ta.get('current_price', 0),
                    daily_pnl=self.daily_pnl,
                    price_change_24h=price_change,
                    volume_ratio=vol_ratio,
                    rsi=rsi,
                    adx=adx,
                    macd_hist=macd_hist,
                )

                if llm_result.get("llm_called", False):
                    llm_action = llm_result.get("action", "HOLD")
                    llm_conf = llm_result.get("confidence", 0.0)
                    llm_reason = llm_result.get("reason", "")
                    reasons.append(f"LLM:{llm_action}({llm_conf:.2f})")
                    
                    # If LLM says SKIP, downgrade to HOLD
                    if llm_action in ("SKIP", "HOLD") and llm_action != signal:
                        self.log.info(f"LLM overrode {signal} -> {llm_action}: {llm_reason}")
                        return {
                            "signal": "HOLD",
                            "confidence": confidence * 0.8,
                            "reason": " | ".join(reasons),
                            "current_price": ta.get('current_price', 0),
                        }
                    # If LLM says CONFIRM or BUY/SELL matching signal, keep it and adjust confidence
                    if llm_action == "CONFIRM" or llm_action == signal:
                        confidence = max(confidence, llm_conf)
                        self.log.info(f"LLM confirmed {signal}: {llm_reason}")
                    # If LLM says opposite direction, flag it but don't override
                    elif llm_action in ("BUY", "SELL") and llm_action != signal:
                        confidence = min(confidence, llm_conf)
                        self.log.info(f"LLM diverges from {signal}: {llm_reason}")
            except Exception as e:
                self.log.warning(f"LLM review error: {e}")

        # Return the sentiment-filtered signal
        if signal == "BUY":
            return {
                "signal": "BUY",
                "confidence": confidence,
                "sell_confidence": sell_score,
                "reason": " | ".join(reasons),
                "current_price": ta.get('current_price', 0),
            }
        else:  # SELL
            return {
                "signal": "SELL",
                "confidence": confidence,
                "buy_confidence": buy_score,
                "reason": " | ".join(reasons),
                "current_price": ta.get('current_price', 0),
            }

    # Max tolerated gap between the OHLCV-derived entry price and the live
    # ticker before an entry is aborted as a stale-candle/phantom price.
    _PRICE_DIVERGENCE_TOL = 0.02

    def _price_diverges_from_live(self, symbol: str, price: float) -> bool:
        """True if the live ticker disagrees with the OHLCV-derived entry
        ``price`` beyond ``_PRICE_DIVERGENCE_TOL`` — a stale-candle guard against
        the 2026-06-05 phantom-price incident.

        Fails OPEN (returns False) when the live ticker is unavailable — the
        collector's candle-staleness check is the other guard layer — but logs
        a warning so the missing cross-check is visible.
        """
        try:
            live_price = self.collector.get_current_price(symbol)
        except Exception:
            live_price = None
        if not live_price:
            # Fail CLOSED on entry: with no live ticker we cannot rule out a
            # stale/phantom OHLCV price (the 2026-06-05 incident). Block the
            # entry rather than open blind. (Exits/SL-TP do not use this guard.)
            self.log.warning(
                f"[{symbol}] live ticker unavailable — BLOCKING entry "
                f"(cannot cross-check OHLCV price {price:.2f})"
            )
            return True
        if abs(live_price - price) / live_price > self._PRICE_DIVERGENCE_TOL:
            self.log.error(
                f"[{symbol}] OHLCV/ticker price divergence: entry={price:.2f} "
                f"vs live={live_price:.2f} "
                f"({abs(live_price - price) / live_price * 100:.1f}%) — skipping"
            )
            return True
        return False

    def execute_trade_cycle(self, symbol_config: dict = None):
        """One complete trade cycle for a given symbol config.

        Parameters
        ----------
        symbol_config : dict
            Dict with keys: name, symbol (exchange), allocation_pct, pair_capital.
        """
        if symbol_config is None:
            # Fallback for backward compatibility — use first enabled symbol
            symbol_config = self.symbols[0] if self.symbols else {
                'name': 'BTC/USDT',
                'symbol': 'tBTCUST',
                'pair_capital': self.initial_capital,
            }

        symbol = symbol_config['name']
        exchange_symbol = symbol_config['symbol']
        pair_capital = symbol_config.get('pair_capital', self.initial_capital)

        try:
            # Risk check first
            risk_ok, risk_msg = self.risk.check_trade_allowed(self.daily_pnl)
            if not risk_ok:
                self.log.warning(f"[{symbol}] Risk check failed: {risk_msg}")
                # Still return the analysis for the cycle summary
                return self.analyze_market(symbol=symbol, exchange_symbol=exchange_symbol)

            # Analyze market
            decision = self.analyze_market(symbol=symbol, exchange_symbol=exchange_symbol)

            if decision['signal'] == 'HOLD':
                self.log.info(f"[{symbol}] HOLD — {decision['reason']}")
                return decision

            # ── direction filter for dual-instance trading ──
            # The direction rule (long-only blocks SELL, short-only blocks BUY
            # unless closing/covering) is now enforced solely by the executor's
            # execute_order, which is the single source of truth because it can
            # see live positions. It returns success=False with a descriptive
            # reason when a signal is blocked; that is logged where the order is
            # placed below. No inline pre-filter here to avoid duplicating the
            # check across layers.

            # Get fresh market data for regime detection (reuse cached 1h when possible — P2-13)
            regime = MarketRegime.RANGING
            regime_params = None
            try:
                df_regime = self._cached_1h_dfs.get(symbol)
                if df_regime is None or len(df_regime) < 30:
                    df_regime = self.collector.get_ohlcv(
                        symbol, timeframe="1h",
                        limit=self.config.get('regime_detector', {}).get('lookback', 50) + 20
                    )
                if df_regime is not None and len(df_regime) > 30:
                    regime = self.regime_detector.detect(df_regime)
                    base_sl = float(self.config.get('risk', {}).get('stop_loss_pct', 2.0))
                    base_tp = float(self.config.get('risk', {}).get('take_profit_pct', 5.0))
                    regime_params = self.regime_detector.get_adapted_params(
                        regime, base_sl, base_tp
                    )
                    self.log.info(
                        f"[{symbol}] Regime: {regime.value} | "
                        f"SL={regime_params['stop_loss_pct']}% "
                        f"TP={regime_params['take_profit_pct']}% "
                        f"Size={regime_params['position_size_mult']}x"
                    )
            except Exception as e:
                self.log.warning(f"[{symbol}] Regime detection error: {e}")

            # Check correlation limits (prevent over-concentration)
            if self.config.get('regime_detector', {}).get('enabled', True):
                open_positions = self.executor.open_positions
                if open_positions:
                    max_corr = float(
                        self.config.get('regime_detector', {})
                        .get('max_correlation', 0.7)
                    )
                    corr_ok, corr_val = self.regime_detector.check_correlation(
                        symbol, open_positions, max_corr
                    )
                    if not corr_ok:
                        self.log.warning(
                            f"[{symbol}] Skipping — too correlated "
                            f"(corr={corr_val:.2f}, limit={max_corr})"
                        )
                        # Return HOLD so cycle summary doesn't show false BUY/SELL (P1-8)
                        decision["signal"] = "HOLD"
                        decision["confidence"] = 0.0
                        decision["reason"] += f" | CorrBlocked({corr_val:.2f})"
                        return decision

            # Get position size
            price = decision['current_price']
            if price <= 0:
                decision["signal"] = "HOLD"
                decision["reason"] += " | InvalidPrice"
                return decision

            # Safety: the entry price (derived from OHLCV) must agree with the
            # live ticker before we open. A stale/mismatched OHLCV candle would
            # otherwise open at a phantom price and get stopped out instantly at
            # the true market — the 2026-06-05 real-money incident.
            if self._price_diverges_from_live(symbol, price):
                decision["signal"] = "HOLD"
                decision["confidence"] = 0.0
                decision["reason"] += " | PriceDivergence"
                return decision

            # Portfolio exposure cap — enforce max_open_positions and never stack
            # a second position on a symbol already held. max_open_positions was
            # declared in every config but NEVER enforced, so cycles could open
            # unbounded/repeat exposure.
            open_positions = self.executor.open_positions
            held_symbols = {p.get('symbol') for p in open_positions}
            max_open = int(self.config.get('trading', {}).get('max_open_positions', 3))
            if symbol in held_symbols:
                decision["signal"] = "HOLD"
                decision["reason"] += " | AlreadyHolding"
                return decision
            if len(open_positions) >= max_open:
                self.log.info(f"[{symbol}] Max open positions ({max_open}) reached — skipping")
                decision["signal"] = "HOLD"
                decision["reason"] += " | MaxPositions"
                return decision

            # Risk-adjusted position sizing — use pair's allocated capital
            effective_capital = pair_capital + (self.daily_pnl / max(len(self.symbols), 1))
            regime_size_mult = (
                regime_params['position_size_mult']
                if regime_params else 1.0
            )
            position_size = self.risk.calculate_position_size(
                capital=effective_capital,
                price=price,
                confidence=decision['confidence'],
                signal_type=decision['signal'],
                regime_position_mult=regime_size_mult,
            )

            if position_size <= 0:
                self.log.info(f"[{symbol}] Position size too small, skipping trade")
                decision["signal"] = "HOLD"
                decision["reason"] += " | PositionTooSmall"
                return decision

            # Enforce exchange minimum order sizes
            symbol_base = symbol.split("/")[0]
            exchange_min_sizes = {"SOL": 0.02, "BTC": 0.0001, "ETH": 0.001}
            min_size = exchange_min_sizes.get(symbol_base, 0.0001)
            if position_size < min_size:
                self.log.info(f"[{symbol}] Position size {position_size:.6f} below minimum {min_size}, "
                             f"scaling up to minimum")
                position_size = min_size

            # Determine regime-adjusted SL/TP
            stop_loss_pct = (
                regime_params['stop_loss_pct']
                if regime_params
                else float(self.config.get('risk', {}).get('stop_loss_pct', 2.0))
            )
            take_profit_pct = (
                regime_params['take_profit_pct']
                if regime_params
                else float(self.config.get('risk', {}).get('take_profit_pct', 5.0))
            )

            # Execute order
            self.log.info(f"[{symbol}] Executing {decision['signal']} — Size: {position_size:.6f} @ ${price:.2f} | "
                         f"Conf: {decision['confidence']:.2f} | Regime: {regime.value}")

            result = self.executor.execute_order(
                symbol=symbol,
                side=decision['signal'].lower(),
                amount=position_size,
                price=price,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
            )

            # Surface the order outcome on the decision so the run loop can
            # link the persisted signal to its order row.
            decision["_acted"] = bool(result.get("success"))
            decision["_db_order_id"] = result.get("db_order_id")

            if result.get('success'):
                self.trade_count += 1
                short_name = symbol.split('/')[0]
                emoji_regime = {
                    'TRENDING': '📈',
                    'RANGING': '📊',
                    'VOLATILE': '🌪️',
                }.get(regime.value, '🤖')
                
                # Signal emoji for BUY vs SELL
                signal_emoji = "🟢" if decision['signal'] == "BUY" else "🔴"
                
                msg = (f"{emoji_regime} **Trade Executed**\n"
                       f"{signal_emoji} *{short_name}* — {decision['signal']}\n"
                       f"Amount: {position_size:.6f}\n"
                       f"Price: ${price:.2f}\n"
                       f"Regime: {regime.value}\n"
                       f"Confidence: {decision['confidence']:.2%}\n"
                       f"Reason: {decision['reason']}")
                self.telegram.send(msg)
                self.log.info(f"[{symbol}] Trade executed: {result}")
            else:
                err = result.get('error', 'Unknown')
                # A blocked direction (dual-instance filter, enforced by the
                # executor) is expected, not a failure — log it at INFO like the
                # old inline filter did. Everything else is a real order failure.
                if "blocked" in str(err).lower():
                    self.log.info(f"[{symbol}] Order blocked: {err}")
                else:
                    self.log.error(f"[{symbol}] Order failed: {err}")

            return decision  # Return analysis result for cycle summary

        except Exception as e:
            self.log.error(f"[{symbol}] Trade cycle error: {e}", exc_info=True)
            return {"symbol": symbol, "signal": "ERROR", "confidence": 0, "current_price": 0, "reason": str(e)}

    def run(self):
        """Main bot loop."""
        self.running = True
        self.start_time = datetime.now()

        # Signal handler for graceful shutdown
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        symbols_str = ', '.join([s['name'] for s in self.symbols])
        
        # Instance-specific startup message
        if self.instance == 'long':
            startup_emoji = "🟢"
            instance_name = "LONG Bot"
            direction_note = "\n📍 Only BUY signals (long positions)"
        elif self.instance == 'short':
            startup_emoji = "🔴"
            instance_name = "SHORT Bot"
            direction_note = "\n📍 Only SELL signals (short positions)"
        else:
            startup_emoji = "🤖"
            instance_name = "Bot"
            direction_note = ""
        
        self.telegram.send(f"{startup_emoji} **{instance_name} Started**{direction_note}\n"
                          f"Mode: {self.mode.upper()}\n"
                          f"Capital: ${self.initial_capital}\n"
                          f"Target: ${self.config.get('trading', {}).get('daily_target', 100)}/day\n"
                          f"Symbols: {symbols_str}")

        # Sync existing positions from exchange (for live mode)
        if self.mode == "live":
            self.executor.sync_positions_at_startup()

        # On first run, train ML models for each symbol
        for s in self.symbols:
            symbol = s['name']
            self.log.info(f"[{symbol}] Checking/training ML model...")
            df = self.collector.get_ohlcv(
                symbol,
                timeframe="1h",
                limit=self.config.get('ml', {}).get('min_train_samples', 500)
            )
            if df is not None:
                result = self.ml_predictor.train(df, symbol=symbol)
                if result.get('status') == 'success':
                    self.log.info(f"[{symbol}] ML model training complete")
                else:
                    self.log.warning(f"[{symbol}] ML training failed: {result.get('reason', 'unknown')}")
            else:
                self.log.warning(f"[{symbol}] No data available for ML training")

        cycle_count = 0
        while self.running:
            try:
                # Daily reset runs even while paused, so a new day can clear a
                # daily-loss-breaker pause (otherwise the pause would be permanent).
                self._check_daily_reset()

                if self.paused:
                    time.sleep(5)
                    continue

                # Trade cycle for EACH symbol
                cycle_results = []
                for s in self.symbols:
                    result = self.execute_trade_cycle(symbol_config=s)
                    if result:
                        # Add display name for Telegram
                        result["symbol_name"] = s["name"]
                        cycle_results.append(result)
                        self._record_decision_signal(s["name"], result)

                # Send cycle summary to Telegram (every cycle)
                if cycle_results:
                    # Get current regime
                    regime_str = ""
                    if hasattr(self, "regime_detector") and self.regime_detector:
                        try:
                            reg = self.regime_detector.current_regime
                            regime_str = reg.name if reg else ""
                        except Exception:
                            pass

                    # Get daily target from config
                    daily_target = self.config.get('trading', {}).get('daily_target', 10.0)

                    self.telegram.send_cycle_summary(
                        results=cycle_results,
                        daily_pnl=self.daily_pnl,
                        open_positions=len(self.executor.open_positions),
                        trade_count=self.trade_count,
                        regime=regime_str,
                        daily_target=daily_target,
                    )

                    # Persist a per-cycle equity snapshot (additive; never raises).
                    try:
                        from persistence import EquitySnapshot
                        # NOTE: equity == balance until the Phase-3 WS account feed
                        # supplies per-position unrealized PnL.
                        bal, eq = self._equity_snapshot_values()
                        self.executor._repo.snapshot_equity(EquitySnapshot(
                            ts=datetime.now(timezone.utc).isoformat(),
                            balance=bal, equity=eq,
                            open_count=len(self.executor.open_positions),
                            daily_pnl=round(self.daily_pnl, 2)))
                    except Exception as exc:
                        self.log.error("Equity snapshot failed: %s", exc)

                # Check open positions for ALL symbols
                self._check_positions()

                # Periodic ML retraining
                cycle_count += 1
                if cycle_count % 24 == 0:  # Every ~24 minutes in 1-min cycles, but with sleep
                    self._maybe_retrain_ml()

                # Sleep between cycles (adjust based on market conditions)
                sleep_time = max(30, min(300, self.risk.get_dynamic_interval()))
                self.log.debug(f"Sleeping {sleep_time}s until next cycle")
                for _ in range(sleep_time):
                    if not self.running:
                        break
                    # Poll Telegram commands every 10s for responsive /commands
                    if _ % 10 == 0:
                        self._check_telegram_commands()
                    time.sleep(1)

            except Exception as e:
                self.log.error(f"Main loop error: {e}", exc_info=True)
                time.sleep(60)

        self._shutdown()

    def _check_positions(self):
        """Monitor open positions for all symbols — stop-loss/take-profit."""
        for s in self.symbols:
            symbol = s['name']
            current_price = self.collector.get_current_price(symbol)
            if current_price is None:
                continue

            for pos in list(self.executor.open_positions):
                if pos.get('symbol') != symbol:
                    continue
                updated = self.risk.update_position(pos, current_price)
                if updated.get('closed'):
                    reason = updated.get('reason', 'SL/TP')
                    side = pos.get('side', 'buy').upper()
                    short_name = symbol.split('/')[0]
                    side_emoji = "🟢" if side == "BUY" else "🔴"

                    # Close on exchange (paper or live): use realized PnL from the
                    # close order.  Executor clears RiskState on success.
                    close_result = self.executor.close_position(
                        symbol, reason=reason
                    )
                    if not close_result.get('success'):
                        self.log.error(
                            f"[{symbol}] Failed to close position: "
                            f"{close_result.get('error')} — will retry next cycle"
                        )
                        # Do NOT touch daily_pnl, do NOT send a closed message,
                        # and leave tracking intact so it is retried.
                        continue

                    # Use REALIZED pnl from the close order.
                    pnl = float(close_result.get('pnl', 0.0))
                    self.daily_pnl += pnl
                    # Feed the RiskManager so its consecutive-loss breaker AND
                    # cooldown-after-loss actually arm (previously dead code —
                    # record_loss/record_win were never called, so the breaker
                    # could never trip).
                    if pnl < 0:
                        self.consecutive_losses += 1
                        self.risk.record_loss()
                    else:
                        self.consecutive_losses = 0
                        self.risk.record_win()

                    emoji = "🟢" if pnl > 0 else "🔴"
                    msg = (f"{emoji} **Position Closed**\n"
                           f"{side_emoji} {short_name} {side}\n"
                           f"PnL: ${pnl:+.2f}\n"
                           f"Reason: {reason}\n"
                           f"Daily PnL: ${self.daily_pnl:+.2f}")
                    self.telegram.send(msg)
                    self.log.info(f"[{symbol}] Position closed: {close_result}")
                    # Executor owns removing the position from tracking.
                else:
                    # Position stays open — write back any trailing stop update.
                    # update_position mutates pos["stop_loss"] in-place but pos is
                    # a transient copy; persist the new value to RiskState.
                    new_sl = pos.get("stop_loss")
                    if updated.get("updated_stop") is not None and new_sl is not None:
                        self.executor._risk_state.update_trailing(
                            symbol, stop_loss=new_sl)

        # Daily summary
        open_count = len(self.executor.open_positions)
        self.log.info(f"Daily PnL: ${self.daily_pnl:.2f} | Open: {open_count} | "
                     f"Trades: {self.trade_count}")

        # Daily-loss circuit breaker: when the loss limit is breached, don't just
        # block new entries — FLATTEN open positions and pause, so a bad day
        # can't keep bleeding through still-open positions.
        max_daily_loss = abs(float(self.config.get('risk', {}).get('max_daily_loss', 20.0)))
        if not self.paused and self.daily_pnl <= -max_daily_loss:
            self.log.error(
                f"DAILY LOSS LIMIT BREACHED: ${self.daily_pnl:.2f} <= -${max_daily_loss:.2f}"
                f" — flattening all positions and pausing until daily reset."
            )
            try:
                self.executor.close_all_positions()
            except Exception as exc:
                self.log.error(f"close_all_positions during breaker failed: {exc}")
            self.paused = True
            self.telegram.send(
                f"🛑 **Daily loss limit hit** (${self.daily_pnl:+.2f}). "
                f"Positions flattened, trading paused until daily reset."
            )

    def _check_daily_reset(self):
        """Reset daily counters using date comparison (P1-9)."""
        today = datetime.now().date()
        if self._last_reset_date is None or today > self._last_reset_date:
            # Send daily summary before reset
            if self._last_reset_date is not None:
                symbols_str = ', '.join([s['name'] for s in self.symbols])
                msg = (f"📊 **Daily Summary**\n"
                       f"Date: {self._last_reset_date.isoformat()}\n"
                       f"Pairs: {symbols_str}\n"
                       f"PnL: ${self.daily_pnl:.2f}\n"
                       f"Trades: {self.trade_count}\n"
                       f"Open: {len(self.executor.open_positions)}")
                self.telegram.send(msg)

            self.daily_pnl = 0.0
            self.trade_count = 0
            self.consecutive_losses = 0
            # Clear a daily-loss-breaker pause and reset the RiskManager's
            # consecutive-loss/cooldown state so trading re-arms for the new day.
            if self.paused:
                self.paused = False
                self.log.info("Daily reset — clearing daily-loss-breaker pause")
            try:
                self.risk.reset()
            except Exception:
                pass
            self._last_reset_date = today
            self.log.info(f"Daily counters reset ({today.isoformat()})")

    def _maybe_retrain_ml(self):
        """Retrain ML models for all symbols if enough new data."""
        for s in self.symbols:
            symbol = s['name']
            hours_since_train = (datetime.now() - self.ml_predictor._last_train_times.get(symbol, datetime.min)).total_seconds() / 3600
            retrain_interval = self.config.get('ml', {}).get('retrain_interval', 24)

            if hours_since_train >= retrain_interval:
                self.log.info(f"[{symbol}] Retraining ML model with new data...")
                df = self.collector.get_ohlcv(
                    symbol,
                    timeframe="1h",
                    limit=self.config.get('ml', {}).get('min_train_samples', 500)
                )
                if df is not None:
                    self.ml_predictor.train(df, symbol=symbol)
                    self.log.info(f"[{symbol}] ML model retrained")

    def _check_telegram_commands(self):
        """Poll Telegram for slash commands and execute them."""
        cmd = self.telegram.poll_commands()
        if cmd is None:
            return
        response = self.telegram.execute_command(
            cmd["cmd"], cmd["args"], cmd["chat_id"], bot=self
        )
        if response:
            self.telegram._send_to_chat(cmd["chat_id"], response)
            self.log.info(f"Telegram command processed: {cmd['cmd']}")

    def _handle_shutdown(self, signum, frame):
        self.log.info("Shutdown signal received...")
        self.running = False

    def _shutdown(self):
        """Clean shutdown."""
        self.log.info("Shutting down bot...")
        self.executor.close_all_positions()
        try:
            self.executor.close()
        except Exception:
            pass
        # Save all ML models
        for s in self.symbols:
            symbol = s['name']
            if self.ml_predictor._model_loaded.get(symbol, False):
                self.ml_predictor.save_model(symbol)
        total = self.daily_pnl
        msg = (f"🛑 **Bot Stopped**\n"
               f"Session PnL: ${total:.2f}\n"
               f"Total Trades: {self.trade_count}\n"
               f"Duration: {datetime.now() - self.start_time}")
        self.telegram.send(msg)
        self.log.info("Bot shutdown complete")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Hermes Crypto Trading Bot")
    parser.add_argument('--mode', choices=['paper', 'live', 'backtest', 'monitor'],
                       default='paper', help='Trading mode')
    parser.add_argument('--config', default='config/default.yaml',
                       help='Config file path')
    parser.add_argument('--symbol', help='Override trading symbol')
    parser.add_argument('--capital', type=float, help='Override initial capital')
    parser.add_argument('--instance', choices=['long', 'short', 'default'],
                       default='default', help='Trading instance type (long=only BUY, short=only SELL)')
    args = parser.parse_args()

    bot = TradingBot(config_path=args.config, mode=args.mode, instance=args.instance)

    if args.mode == 'backtest':
        from backtest.engine import BacktestEngine
        engine = BacktestEngine(bot.config)
        engine.run()
    elif args.mode == 'monitor':
        from monitoring.dashboard import Dashboard
        dash = Dashboard(bot.config)
        dash.run()
    else:
        bot.run()


if __name__ == "__main__":
    main()
