#!/usr/bin/env python3
"""
Ensemble Strategy Bot Integration
Integrates ML + RL ensemble into the live trading bot
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
from typing import Dict, Optional, Any
import pandas as pd

from strategies.ensemble_strategy import EnsembleStrategy


class EnsembleBotStrategy:
    """
    Wrapper to integrate Ensemble Strategy into the trading bot
    Compatible with existing bot strategy interface
    """
    
    def __init__(
        self,
        symbol: str = "BTC/USDT",
        ml_weight: float = 0.4,
        rl_weight: float = 0.6,
        confidence_threshold: float = 0.20,  # Default: 0.20 for quality signals
        agreement_required: bool = False,
        use_position_sizing: bool = True,
        config: Optional[Dict] = None  # Accept config dict for compatibility
    ):
        self.symbol = symbol
        self.log = logging.getLogger(f"EnsembleBot-{symbol}")
        
        # Extract settings from config if provided
        if config:
            ml_weight = config.get("ml_weight", ml_weight)
            rl_weight = config.get("rl_weight", rl_weight)
            confidence_threshold = config.get("confidence_threshold", confidence_threshold)
            agreement_required = config.get("agreement_required", agreement_required)
            use_position_sizing = config.get("use_dynamic_sizing", use_position_sizing)
        
        # Initialize ensemble
        self.ensemble = EnsembleStrategy(
            symbol=symbol,
            ml_weight=ml_weight,
            rl_weight=rl_weight,
            confidence_threshold=confidence_threshold,
            agreement_required=agreement_required
        )
        
        self.use_position_sizing = use_position_sizing
        
        # Track status - check if any models loaded
        ml_loaded = self.ensemble.ml_predictor.model_loaded
        rl_loaded = self.ensemble.rl_strategy._model_loaded if hasattr(self.ensemble.rl_strategy, '_model_loaded') else False
        self._initialized = ml_loaded or rl_loaded
        
        if self._initialized:
            self.log.info(f"Ensemble strategy initialized for {symbol}")
            self.log.info(f"  ML loaded: {ml_loaded}")
            self.log.info(f"  RL loaded: {rl_loaded}")
        else:
            self.log.warning(f"No models loaded for {symbol} - strategy will return HOLD")
    
    def analyze(
        self,
        current_price: float,
        position: Optional[Dict] = None,
        market_data: pd.DataFrame = None
    ) -> Dict[str, Any]:
        """
        Analyze market and return trading signal
        
        Args:
            current_price: Current market price
            position: Current position dict with 'side', 'size', 'entry_price'
            market_data: OHLCV DataFrame (optional, will fetch if None)
        
        Returns:
            Signal dict compatible with bot interface
        """
        if market_data is None or len(market_data) < 50:
            return {
                'signal': 'HOLD',
                'strength': 'neutral',
                'confidence': 0.0,
                'reason': 'Insufficient data for analysis',
                'position_pct': 0.0
            }
        
        # Format position info for ensemble
        position_info = None
        if position and position.get('side'):
            position_info = {
                'side': position['side'],
                'size': position.get('size', 0.0),
                'entry_price': position.get('entry_price', 0.0)
            }
        
        # Get ensemble signal
        signal = self.ensemble.analyze(market_data, position_info)
        
        # Convert to bot format
        return self._format_bot_signal(signal)
    
    def _format_bot_signal(self, signal: Dict) -> Dict[str, Any]:
        """Convert ensemble signal to bot-compatible format"""
        
        signal_type = signal.get('signal', 'HOLD')
        confidence = signal.get('confidence', 0.0)
        position_pct = signal.get('position_pct', 0.0)
        
        # Determine strength
        if confidence >= 0.8:
            strength = 'strong'
        elif confidence >= 0.65:
            strength = 'moderate'
        elif confidence >= 0.5:
            strength = 'weak'
        else:
            strength = 'neutral'
        
        # Build detailed reason
        ml_sig = signal.get('ml_signal', 'N/A')
        ml_conf = signal.get('ml_confidence', 0.0)
        rl_sig = signal.get('rl_signal', 'N/A')
        rl_conf = signal.get('rl_confidence', 0.0)
        agreement = signal.get('agreement', False)
        
        reason = (
            f"Ensemble[{self.symbol}]: {signal_type} | "
            f"Score: {signal.get('ensemble_score', 0):.2f} | "
            f"Conf: {confidence:.2f} | "
            f"ML: {ml_sig}({ml_conf:.2f}) | "
            f"RL: {rl_sig}({rl_conf:.2f}) | "
            f"Agree: {'Yes' if agreement else 'No'}"
        )
        
        return {
            'signal': signal_type,
            'strength': strength,
            'confidence': confidence,
            'strategy': 'ensemble',
            'reason': reason,
            'position_pct': position_pct,
            'metadata': {
                'ml_signal': ml_sig,
                'ml_confidence': ml_conf,
                'rl_signal': rl_sig,
                'rl_confidence': rl_conf,
                'agreement': agreement,
                'ensemble_score': signal.get('ensemble_score', 0.0)
            }
        }
    
    def is_ready(self) -> bool:
        """Check if strategy is ready to trade"""
        return self._initialized
    
    def get_model_info(self) -> Dict:
        """Get information about loaded models"""
        return self.ensemble.get_model_info()
    
    def generate(
        self,
        ta_1h: dict,
        ta_5m: dict,
        ta_15m: dict,
        ml_signal: dict,
        df_1h: "pd.DataFrame" = None,
    ) -> Dict[str, Any]:
        """
        Generate signal from ensemble - compatible with StrategySelector interface

        Args:
            ta_1h: 1h timeframe technical analysis data
            ta_5m: 5m timeframe technical analysis data
            ta_15m: 15m timeframe technical analysis data
            ml_signal: ML predictor signal
            df_1h: the real 1h OHLCV window. REQUIRED for a real signal — the
                ensemble's analyze() needs >=50 rows. Without it we fall back to
                a 1-row stub that always yields HOLD (kept only for back-compat).

        Returns:
            Signal dict with signal, confidence, reason, params. `signal` is
            normalized to BUY/SELL/HOLD — the ensemble's CLOSE (and any other
            exit/short action) maps to HOLD because this bot consumes strategy
            output only as an entry-direction vote; exits/sizing are owned by the
            risk and SL/TP layers.
        """
        if not ta_1h:
            return self._hold("No 1h TA data")

        try:
            import pandas as pd
            if df_1h is not None and len(df_1h) >= 50:
                market_data = df_1h
            else:
                # Fallback stub — analyze() will short-circuit to HOLD.
                market_data = pd.DataFrame({
                    'open': [ta_1h.get('open', ta_1h.get('close', 0))],
                    'high': [ta_1h.get('high', ta_1h.get('close', 0))],
                    'low': [ta_1h.get('low', ta_1h.get('close', 0))],
                    'close': [ta_1h.get('close', 0)],
                    'volume': [ta_1h.get('volume', 0)],
                })

            signal = self.analyze(
                current_price=ta_1h.get('close', 0),
                position=None,
                market_data=market_data,
            )

            sig = signal.get('signal', 'HOLD')
            if sig not in ('BUY', 'SELL'):
                sig = 'HOLD'

            return {
                'signal': sig,
                'confidence': signal.get('confidence', 0.0),
                'reason': signal.get('reason', 'Ensemble analysis'),
                'params': signal.get('metadata', {}),
            }

        except Exception as e:
            self.log.error(f"Ensemble generate error: {e}")
            return self._hold(f"Error: {e}")
    
    def _hold(self, reason: str) -> dict:
        """Return HOLD signal"""
        return {
            'signal': 'HOLD',
            'confidence': 0.0,
            'reason': reason,
            'params': {}
        }


def create_ensemble_bot_strategy(
    symbol: str = "BTC/USDT",
    ml_weight: float = 0.4,
    rl_weight: float = 0.6,
    confidence_threshold: float = 0.15,  # Default: 0.15 for new models
    **kwargs
) -> EnsembleBotStrategy:
    """
    Factory function to create ensemble strategy for bot
    
    Usage:
        from strategies.ensemble_bot_integration import create_ensemble_bot_strategy
        
        strategy = create_ensemble_bot_strategy(
            symbol="BTC/USDT",
            ml_weight=0.4,
            rl_weight=0.6
        )
        
        signal = strategy.analyze(
            current_price=95000,
            position={'side': None},
            market_data=df
        )
    """
    return EnsembleBotStrategy(
        symbol=symbol,
        ml_weight=ml_weight,
        rl_weight=rl_weight,
        confidence_threshold=confidence_threshold,
        **kwargs
    )


# Example usage
if __name__ == "__main__":
    print("Testing Ensemble Bot Strategy Integration...")
    
    # Create strategy
    strategy = create_ensemble_bot_strategy("BTC/USDT")
    
    # Check if ready
    print(f"\nStrategy ready: {strategy.is_ready()}")
    
    # Get model info
    info = strategy.get_model_info()
    print(f"\nModel Info:")
    print(f"  Symbol: {info['symbol']}")
    print(f"  Initialized: {info['initialized']}")
    print(f"  ML loaded: {info['ml_loaded']}")
    print(f"  RL loaded: {info['rl_loaded']}")
    print(f"  Weights: ML={info['ml_weight']}, RL={info['rl_weight']}")
