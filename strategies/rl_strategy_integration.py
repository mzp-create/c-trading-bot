"""
RL Strategy Integration with Existing Trading Bot
Connects the PPO agent to the live trading system
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
from typing import Dict, Optional, Any
import pandas as pd

from rl_trading.rl_strategy import RLStrategy


class RLStrategyIntegration:
    """
    Integration layer between RL strategy and existing bot infrastructure
    """
    
    def __init__(
        self,
        symbol: str = "BTC/USDT",
        model_path: Optional[str] = None,
        confidence_threshold: float = 0.6,
        data_lookback: int = 100,
        **kwargs
    ):
        self.symbol = symbol
        self.data_lookback = data_lookback
        self.log = logging.getLogger(f"RL-Integration-{symbol}")
        
        # Initialize RL strategy
        if model_path is None:
            # Try to find best model
            model_path = self._find_best_model()
        
        self.rl_strategy = RLStrategy(
            symbol=symbol,
            model_path=model_path,
            confidence_threshold=confidence_threshold,
            **kwargs
        )
        
        self._model_loaded = self.rl_strategy._model_loaded
        
    def _find_best_model(self) -> Optional[str]:
        """Find the best trained model for symbol"""
        safe_symbol = self.symbol.replace('/', '_')
        model_dir = Path("data/models/rl")
        
        # Look for best model first
        best_model = model_dir / f"{safe_symbol}_ppo_best.pt"
        if best_model.exists():
            return str(best_model)
        
        # Look for latest checkpoint
        checkpoints = list(model_dir.glob(f"{safe_symbol}_ppo_ep*.pt"))
        if checkpoints:
            return str(sorted(checkpoints)[-1])
        
        return None
    
    def analyze(self, data: pd.DataFrame, position_info: Dict = None) -> Dict[str, Any]:
        """
        Analyze market data and return trading signal
        Compatible with existing strategy interface
        
        Args:
            data: OHLCV DataFrame
            position_info: Current position info dict with keys:
                - side: 'long' or 'short' or None
                - size: position size
                - entry_price: entry price
        
        Returns:
            Signal dictionary
        """
        if position_info is None:
            position_info = {'side': None, 'size': 0.0, 'entry_price': 0.0}
        
        # Get recent data
        if len(data) > self.data_lookback:
            data = data.iloc[-self.data_lookback:]
        
        # Generate signal from RL agent
        signal = self.rl_strategy.generate_signal(
            df=data,
            current_position=position_info.get('side'),
            position_size=position_info.get('size', 0.0),
            entry_price=position_info.get('entry_price', 0.0)
        )
        
        # Convert to standard format
        return self._format_signal(signal)
    
    def _format_signal(self, rl_signal: Dict) -> Dict[str, Any]:
        """
        Format RL signal to match existing bot interface
        """
        signal_type = rl_signal.get('signal', 'HOLD')
        confidence = rl_signal.get('confidence', 0.0)
        
        # Determine signal strength
        if confidence >= 0.8:
            strength = 'strong'
        elif confidence >= 0.6:
            strength = 'moderate'
        else:
            strength = 'weak'
        
        # Build standard signal dict
        formatted = {
            'signal': signal_type,  # BUY, SELL, HOLD, CLOSE
            'strength': strength,
            'confidence': confidence,
            'strategy': 'rl_agent',
            'reason': rl_signal.get('reason', 'RL agent decision'),
            'metadata': {
                'action_type': rl_signal.get('action_type'),
                'position_pct': rl_signal.get('position_pct', 0.0),
                'raw_action': rl_signal.get('raw_action'),
                'model_loaded': self._model_loaded
            }
        }
        
        return formatted
    
    def update_position_state(self, side: Optional[str], size: float, entry_price: float):
        """Update RL strategy's position tracking"""
        self.rl_strategy.update_position(side, size, entry_price)
    
    def get_strategy_info(self) -> Dict:
        """Get information about the RL strategy"""
        return self.rl_strategy.get_model_info()


def create_rl_strategy(symbol: str = "BTC/USDT", **kwargs) -> RLStrategyIntegration:
    """
    Factory function to create RL strategy
    
    Usage:
        from strategies.rl_strategy_integration import create_rl_strategy
        
        rl_strategy = create_rl_strategy(symbol="BTC/USDT")
        signal = rl_strategy.analyze(market_data, position_info)
    """
    return RLStrategyIntegration(symbol=symbol, **kwargs)


# Example standalone usage
if __name__ == "__main__":
    # Test RL strategy
    print("Testing RL Strategy Integration...")
    
    strategy = create_rl_strategy("BTC/USDT")
    info = strategy.get_strategy_info()
    print(f"Model loaded: {info.get('loaded', False)}")
    print(f"Training steps: {info.get('training_steps', 0)}")
    
    # Create dummy data for testing
    import numpy as np
    
    dates = pd.date_range(end=pd.Timestamp.now(), periods=100, freq='1h')
    dummy_data = pd.DataFrame({
        'open': np.random.randn(100).cumsum() + 100000,
        'high': np.random.randn(100).cumsum() + 100010,
        'low': np.random.randn(100).cumsum() + 99990,
        'close': np.random.randn(100).cumsum() + 100000,
        'volume': np.random.rand(100) * 1000
    }, index=dates)
    
    signal = strategy.analyze(dummy_data)
    print(f"\nSignal: {signal}")
