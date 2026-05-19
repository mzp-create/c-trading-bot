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
import logging
import signal
from pathlib import Path
from datetime import datetime, timedelta, date
from typing import Optional, Dict, Any, List

# Add project root to path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

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
    """Main trading bot orchestrator."""

    def __init__(self, config_path: str = "config/default.yaml", mode: str = "paper"):
        self.config = load_config(config_path)

        self.mode = mode
        self.running = False
        self.paused = False
        self.start_time = None
        self.daily_pnl = 0.0
        self.trade_count = 0
        self.consecutive_losses = 0
        self._last_reset_date: Optional[date] = None
        self._cached_1h_dfs: Dict[str, Optional[pd.DataFrame]] = {}

        # Initialize components
        self.logger = BotLogger(self.config)
        self.telegram = TelegramNotifier(self.config)
        self.collector = MarketDataCollector(self.config)
        self.analyzer = TechnicalAnalyzer(self.config)
        self.ml_predictor = MLPredictor(self.config)
        self.strategies = StrategySelector(self.config)
        self.risk = RiskManager(self.config)
        self.regime_detector = MarketRegimeDetector(self.config)
        self.sentiment = SentimentAnalyzer(self.config)
        self.llm_reviewer = LLMReviewer(self.config)
        self.executor = ExecutionEngine(self.config, mode=mode)

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

        # 4. Get strategy signals
        strategy_signals = self.strategies.get_signals(ta_1h, ta_5m, ta_15m, ml_signal)

        # 5. Combine into final decision
        final = self._combine_signals(strategy_signals, ml_signal, ta_1h)
        final['symbol'] = symbol

        # Log analysis summary
        self.log.info(f"[{symbol}] Analysis: {final['signal']} (conf: {final['confidence']:.2f}) | "
                     f"TA: {ta_1h.get('signal','HOLD')} | ML: {ml_signal.get('signal','HOLD')}")

        return final

    def _combine_signals(self, strategy_signals: dict, ml_signal: dict, ta: dict) -> Dict:
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
            w = weights.get(sig['name'], 0.25)
            total_weight += w
            if sig['signal'] == 'BUY':
                buy_score += w * sig['confidence']
                reasons.append(f"{sig['name']}:BUY({sig['confidence']:.2f})")
            elif sig['signal'] == 'SELL':
                sell_score += w * sig['confidence']
                reasons.append(f"{sig['name']}:SELL({sig['confidence']:.2f})")

        # Add ML signal — weight based on model accuracy (P2-14)
        ml_weight = 0.3
        try:
            sym = ml_signal.get("symbol", "")
            if sym:
                acc = self.ml_predictor._training_accuracies.get(sym, 0.55)
                if acc > 0.5:
                    ml_weight = min(0.4, max(0.1, acc - 0.4))
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
        confidence_threshold = 0.50
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
        if self.config.get("sentiment", {}).get("enabled", False):
            symbol_raw = ta.get("symbol", ta.get("close_symbol", ""))
            if not symbol_raw:
                symbol_raw = "BTC"
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
                return

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
                return

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

            if result.get('success'):
                self.trade_count += 1
                short_name = symbol.split('/')[0]
                emoji_regime = {
                    'TRENDING': '📈',
                    'RANGING': '📊',
                    'VOLATILE': '🌪️',
                }.get(regime.value, '🤖')
                msg = (f"{emoji_regime} **Trade Executed**\n"
                       f"*{short_name}* — {decision['signal']}\n"
                       f"Amount: {position_size:.6f}\n"
                       f"Price: ${price:.2f}\n"
                       f"Regime: {regime.value}\n"
                       f"Confidence: {decision['confidence']:.2%}\n"
                       f"Reason: {decision['reason']}")
                self.telegram.send(msg)
                self.log.info(f"[{symbol}] Trade executed: {result}")
            else:
                self.log.error(f"[{symbol}] Order failed: {result.get('error', 'Unknown')}")

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
        self.telegram.send(f"🤖 **Bot Started**\n"
                          f"Mode: {self.mode.upper()}\n"
                          f"Capital: ${self.initial_capital}\n"
                          f"Target: ${self.config.get('trading', {}).get('daily_target', 100)}/day\n"
                          f"Symbols: {symbols_str}")

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
                self.ml_predictor.train(df, symbol=symbol)
                self.log.info(f"[{symbol}] ML model training complete")
            else:
                self.log.warning(f"[{symbol}] No data available for ML training")

        cycle_count = 0
        while self.running:
            try:
                if self.paused:
                    time.sleep(5)
                    continue

                # Check for daily reset
                self._check_daily_reset()

                # Trade cycle for EACH symbol
                cycle_results = []
                for s in self.symbols:
                    result = self.execute_trade_cycle(symbol_config=s)
                    if result:
                        # Add display name for Telegram
                        result["symbol_name"] = s["name"]
                        cycle_results.append(result)

                # Send cycle summary to Telegram (every cycle)
                if cycle_results:
                    self.telegram.send_cycle_summary(
                        results=cycle_results,
                        daily_pnl=self.daily_pnl,
                        open_positions=len(self.executor.open_positions),
                        trade_count=self.trade_count,
                    )

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
                    # Poll Telegram commands once per cycle (P2-12 fix: was every 5s)
                    if _ == 0:
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
                    pnl = updated.get('pnl', 0.0)
                    self.daily_pnl += pnl
                    self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0

                    short_name = symbol.split('/')[0]
                    emoji = "🟢" if pnl > 0 else "🔴"
                    msg = (f"{emoji} **Position Closed**\n"
                           f"{short_name}: ${pnl:.2f}\n"
                           f"Reason: {updated.get('reason', 'Unknown')}\n"
                           f"Daily PnL: ${self.daily_pnl:.2f}")
                    self.telegram.send(msg)
                    self.log.info(f"[{symbol}] Position closed: {updated}")

                    # Remove position from executor so it doesn't accumulate forever (P1-6)
                    try:
                        self.executor.open_positions.remove(pos)
                    except (ValueError, AttributeError):
                        self.log.warning(f"[{symbol}] Position already removed from list")

        # Daily summary
        open_count = len(self.executor.open_positions)
        self.log.info(f"Daily PnL: ${self.daily_pnl:.2f} | Open: {open_count} | "
                     f"Trades: {self.trade_count}")

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

    def _handle_shutdown(self, signum, frame):
        self.log.info("Shutdown signal received...")
        self.running = False

    def _shutdown(self):
        """Clean shutdown."""
        self.log.info("Shutting down bot...")
        self.executor.close_all_positions()
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
    args = parser.parse_args()

    bot = TradingBot(config_path=args.config, mode=args.mode)

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
