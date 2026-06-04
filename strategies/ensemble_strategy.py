"""
Ensemble Strategy - Combines ML + RL predictions for stronger signals

This strategy combines:
1. ML Predictor (RandomForest + GradientBoosting) - Direction prediction
2. RL Agent (PPO) - Position sizing and timing
3. Weighted ensemble for final decision
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
from typing import Dict, Optional, Any, Tuple
import pandas as pd
import numpy as np

from strategies.rl_strategy_integration import create_rl_strategy
from analysis.ml_predictor import MLPredictor


class EnsembleStrategy:
    """
    Ensemble trading strategy combining ML and RL predictions
    """
    
    def __init__(
        self,
        symbol: str = "BTC/USDT",
        ml_weight: float = 0.4,
        rl_weight: float = 0.6,
        confidence_threshold: float = 0.20,  # Default: 0.20 for quality signals
        agreement_required: bool = False,
        config: Optional[Dict] = None
    ):
        """
        Initialize ensemble strategy
        
        Args:
            symbol: Trading pair
            ml_weight: Weight for ML predictor (0-1)
            rl_weight: Weight for RL agent (0-1)
            confidence_threshold: Minimum confidence to trade
            agreement_required: Only trade when ML and RL agree
            config: Additional configuration
        """
        self.symbol = symbol
        self.ml_weight = ml_weight
        self.rl_weight = rl_weight
        self.confidence_threshold = confidence_threshold
        self.agreement_required = agreement_required
        self.config = config or {}
        
        self.log = logging.getLogger(f"Ensemble-{symbol}")
        
        # Initialize ML predictor
        self.ml_config = self.config.get('ml', {
            'models_dir': 'data/models',
            'training_threshold': 0.6,
            'min_samples': 200
        })
        self.ml_predictor = MLPredictor(self.ml_config)
        
        # Initialize RL strategy
        self.rl_strategy = create_rl_strategy(
            symbol=symbol,
            confidence_threshold=confidence_threshold
        )
        
        # Track prediction history for debugging
        self.prediction_history = []
        
    def analyze(
        self,
        data: pd.DataFrame,
        position_info: Dict = None
    ) -> Dict[str, Any]:
        """
        Analyze market using both ML and RL, return ensemble signal
        
        Args:
            data: OHLCV DataFrame
            position_info: Current position info
            
        Returns:
            Ensemble signal dictionary
        """
        if position_info is None:
            position_info = {'side': None, 'size': 0.0, 'entry_price': 0.0}
        
        # Get ML prediction
        ml_signal = self._get_ml_signal(data)
        
        # Get RL prediction
        rl_signal = self.rl_strategy.analyze(data, position_info)
        
        # Combine predictions
        ensemble_signal = self._combine_signals(ml_signal, rl_signal, position_info)
        
        # Store prediction for analysis
        self.prediction_history.append({
            'timestamp': data.index[-1] if len(data) > 0 else None,
            'ml_signal': ml_signal,
            'rl_signal': rl_signal,
            'ensemble': ensemble_signal
        })
        
        # Keep history manageable
        if len(self.prediction_history) > 1000:
            self.prediction_history = self.prediction_history[-500:]
        
        return ensemble_signal
    
    def _get_ml_signal(self, data: pd.DataFrame) -> Dict:
        """Get ML predictor signal"""
        try:
            # Check if model is loaded/trained
            if not self.ml_predictor.model_loaded:
                # Try to load existing model
                safe_symbol = self.symbol.replace('/', '_')
                model_path = Path(f"data/models/{safe_symbol}_ml_model.joblib")
                if model_path.exists():
                    self.ml_predictor.load_model(self.symbol)
                else:
                    # Train if enough data
                    if len(data) >= 200:
                        self.log.info(f"Training ML model for {self.symbol}...")
                        result = self.ml_predictor.train(data, self.symbol)
                        if result.get('status') != 'success':
                            return {'signal': 'HOLD', 'confidence': 0.5, 'prob_up': 0.5}
            
            # Get prediction
            prediction = self.ml_predictor.predict(data, self.symbol)
            return prediction
            
        except Exception as e:
            self.log.error(f"ML prediction error: {e}")
            return {'signal': 'HOLD', 'confidence': 0.5, 'prob_up': 0.5}
    
    def _combine_signals(
        self,
        ml_signal: Dict,
        rl_signal: Dict,
        position_info: Dict
    ) -> Dict[str, Any]:
        """
        Combine ML and RL signals into ensemble decision
        """
        # Extract components
        ml_direction = ml_signal.get('signal', 'HOLD')  # BUY, SELL, HOLD
        ml_conf = ml_signal.get('confidence', 0.5)
        ml_prob_up = ml_signal.get('prob_up', 0.5)
        
        rl_direction = rl_signal.get('signal', 'HOLD')  # BUY, SELL, HOLD, CLOSE
        rl_conf = rl_signal.get('confidence', 0.5)
        rl_position_pct = rl_signal.get('position_pct', 0.0)
        
        # Convert directions to numeric scores
        ml_score = self._direction_to_score(ml_direction, ml_prob_up)
        rl_score = self._direction_to_score(rl_direction)
        
        # Weighted ensemble score (-1 to 1)
        ensemble_score = (ml_score * self.ml_weight) + (rl_score * self.rl_weight)
        
        # Calculate ensemble confidence
        ensemble_conf = (ml_conf * self.ml_weight) + (rl_conf * self.rl_weight)
        
        # Check agreement
        agreement = self._check_agreement(ml_direction, rl_direction)
        
        # Determine final signal
        signal, strength, reason = self._determine_signal(
            ensemble_score,
            ensemble_conf,
            agreement,
            position_info
        )
        
        # Calculate position size based on confidence
        position_pct = self._calculate_position_size(
            ensemble_score,
            ensemble_conf,
            rl_position_pct,
            agreement
        )
        
        return {
            'signal': signal,
            'strength': strength,
            'confidence': float(ensemble_conf),
            'ensemble_score': float(ensemble_score),
            'strategy': 'ensemble',
            'reason': reason,
            'position_pct': position_pct,
            'ml_signal': ml_direction,
            'ml_confidence': float(ml_conf),
            'rl_signal': rl_direction,
            'rl_confidence': float(rl_conf),
            'agreement': agreement,
            'metadata': {
                'ml_prob_up': ml_prob_up,
                'rl_position_pct': rl_position_pct,
                'ml_weight': self.ml_weight,
                'rl_weight': self.rl_weight
            }
        }
    
    def _direction_to_score(self, direction: str, prob_up: float = None) -> float:
        """Convert direction to numeric score (-1 to 1)"""
        if direction == 'BUY':
            return 1.0
        elif direction == 'SELL':
            return -1.0
        elif direction == 'CLOSE':
            return 0.0  # Neutral
        else:  # HOLD
            if prob_up is not None:
                # Use probability for HOLD signals
                return (prob_up - 0.5) * 2  # Scale to -1 to 1
            return 0.0
    
    def _check_agreement(self, ml_direction: str, rl_direction: str) -> bool:
        """Check if ML and RL agree on direction"""
        # Normalize directions
        ml_long = ml_direction in ['BUY']
        ml_short = ml_direction in ['SELL']
        ml_neutral = ml_direction in ['HOLD', 'CLOSE']
        
        rl_long = rl_direction in ['BUY']
        rl_short = rl_direction in ['SELL', 'SHORT']
        rl_close = rl_direction in ['CLOSE']
        
        # Agreement conditions
        if ml_long and rl_long:
            return True
        if ml_short and rl_short:
            return True
        if ml_neutral and (rl_close or rl_direction == 'HOLD'):
            return True
        
        return False
    
    def _determine_signal(
        self,
        ensemble_score: float,
        ensemble_conf: float,
        agreement: bool,
        position_info: Dict
    ) -> Tuple[str, str, str]:
        """
        Determine final trading signal
        
        Returns: (signal, strength, reason)
        """
        current_position = position_info.get('side')
        
        # If agreement is required but not present, HOLD
        if self.agreement_required and not agreement:
            return 'HOLD', 'weak', 'ML and RL disagree'
        
        # Check confidence threshold
        if ensemble_conf < self.confidence_threshold:
            return 'HOLD', 'weak', f'Confidence {ensemble_conf:.2f} below threshold'
        
        # Determine signal based on score
        if ensemble_score > 0.3:
            signal = 'BUY'
            if current_position == 'short':
                signal = 'CLOSE'  # Close short before going long
        elif ensemble_score < -0.3:
            signal = 'SELL'
            if current_position == 'long':
                signal = 'CLOSE'  # Close long before going short
        else:
            signal = 'HOLD'
        
        # Determine strength
        abs_score = abs(ensemble_score)
        if abs_score > 0.7 and agreement:
            strength = 'strong'
        elif abs_score > 0.5:
            strength = 'moderate'
        else:
            strength = 'weak'
        
        # Build reason
        reason_parts = [
            f"Ensemble score: {ensemble_score:.2f}",
            f"Confidence: {ensemble_conf:.2f}",
            f"Agreement: {'Yes' if agreement else 'No'}"
        ]
        
        return signal, strength, '; '.join(reason_parts)
    
    def _calculate_position_size(
        self,
        ensemble_score: float,
        ensemble_conf: float,
        rl_position_pct: float,
        agreement: bool
    ) -> float:
        """Calculate position size based on confidence and agreement"""
        # Base size from RL
        base_size = rl_position_pct
        
        # Scale by ensemble confidence
        conf_multiplier = ensemble_conf  # 0 to 1
        
        # Boost if agreement
        agreement_boost = 1.3 if agreement else 1.0
        
        # Scale by score magnitude
        score_multiplier = min(abs(ensemble_score) * 1.5, 1.0)
        
        # Calculate final size
        position_pct = base_size * conf_multiplier * agreement_boost * score_multiplier
        
        # Cap at 100%
        return min(position_pct, 1.0)
    
    def get_model_info(self) -> Dict:
        """Get information about loaded models"""
        ml_loaded = self.ml_predictor.model_loaded
        rl_loaded = self.rl_strategy._model_loaded if hasattr(self.rl_strategy, '_model_loaded') else False
        
        return {
            'symbol': self.symbol,
            'initialized': ml_loaded or rl_loaded,
            'ml_loaded': ml_loaded,
            'rl_loaded': rl_loaded,
            'ml_weight': self.ml_weight,
            'rl_weight': self.rl_weight,
            'confidence_threshold': self.confidence_threshold,
            'agreement_required': self.agreement_required
        }
    
    def get_prediction_stats(self) -> Dict:
        """Get statistics on recent predictions"""
        if not self.prediction_history:
            return {}
        
        recent = self.prediction_history[-100:]
        
        ml_signals = [p['ml_signal'].get('signal', 'HOLD') for p in recent]
        rl_signals = [p['rl_signal'].get('signal', 'HOLD') for p in recent]
        ensemble_signals = [p['ensemble'].get('signal', 'HOLD') for p in recent]
        
        return {
            'total_predictions': len(recent),
            'ml_buy_pct': ml_signals.count('BUY') / len(ml_signals) * 100,
            'ml_sell_pct': ml_signals.count('SELL') / len(ml_signals) * 100,
            'rl_buy_pct': rl_signals.count('BUY') / len(rl_signals) * 100,
            'rl_sell_pct': rl_signals.count('SELL') / len(rl_signals) * 100,
            'ensemble_buy_pct': ensemble_signals.count('BUY') / len(ensemble_signals) * 100,
            'ensemble_sell_pct': ensemble_signals.count('SELL') / len(ensemble_signals) * 100,
            'agreement_rate': sum(
                1 for p in recent 
                if self._check_agreement(
                    p['ml_signal'].get('signal', 'HOLD'),
                    p['rl_signal'].get('signal', 'HOLD')
                )
            ) / len(recent) * 100
        }


def create_ensemble_strategy(
    symbol: str = "BTC/USDT",
    ml_weight: float = 0.4,
    rl_weight: float = 0.6,
    **kwargs
) -> EnsembleStrategy:
    """
    Factory function to create ensemble strategy
    
    Usage:
        from strategies.ensemble_strategy import create_ensemble_strategy
        
        strategy = create_ensemble_strategy(
            symbol="BTC/USDT",
            ml_weight=0.4,
            rl_weight=0.6
        )
        signal = strategy.analyze(market_data, position_info)
    """
    return EnsembleStrategy(
        symbol=symbol,
        ml_weight=ml_weight,
        rl_weight=rl_weight,
        **kwargs
    )


# Example usage
if __name__ == "__main__":
    print("Testing Ensemble Strategy...")
    
    # Create strategy
    strategy = create_ensemble_strategy("BTC/USDT")
    
    # Create dummy data
    import numpy as np
    dates = pd.date_range(end=pd.Timestamp.now(), periods=100, freq='1h')
    dummy_data = pd.DataFrame({
        'open': np.random.randn(100).cumsum() + 75000,
        'high': np.random.randn(100).cumsum() + 75100,
        'low': np.random.randn(100).cumsum() + 74900,
        'close': np.random.randn(100).cumsum() + 75000,
        'volume': np.random.rand(100) * 100
    }, index=dates)
    
    # Generate signal
    signal = strategy.analyze(dummy_data)
    
    print(f"\nEnsemble Signal:")
    print(f"  Signal: {signal['signal']}")
    print(f"  Strength: {signal['strength']}")
    print(f"  Confidence: {signal['confidence']:.3f}")
    print(f"  ML Signal: {signal['ml_signal']} ({signal['ml_confidence']:.3f})")
    print(f"  RL Signal: {signal['rl_signal']} ({signal['rl_confidence']:.3f})")
    print(f"  Agreement: {signal['agreement']}")
    print(f"  Position %: {signal['position_pct']:.2%}")
