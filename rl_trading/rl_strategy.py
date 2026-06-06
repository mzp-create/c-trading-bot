"""
RL Trading Strategy
Integrates PPO agent with existing bot infrastructure
"""

import numpy as np
import pandas as pd
import torch
from pathlib import Path
from typing import Dict, Optional, Tuple
import logging

from .ppo_agent import PPOAgent
from .trading_env import TradingEnv


class RLStrategy:
    """
    Reinforcement Learning-based trading strategy
    Uses trained PPO agent to make trading decisions
    """
    
    def __init__(
        self,
        symbol: str = "BTC/USDT",
        model_path: Optional[str] = None,
        confidence_threshold: float = 0.6,
        data_dir: str = "data/ohlcv",
        leverage: float = 5.0,
        device: str = 'auto'
    ):
        self.symbol = symbol
        self.confidence_threshold = confidence_threshold
        self.data_dir = Path(data_dir)
        self.leverage = leverage
        self.device = device
        
        self.log = logging.getLogger(f"RLStrategy-{symbol}")
        
        # Agent will be loaded on first use
        self.agent: Optional[PPOAgent] = None
        self.env: Optional[TradingEnv] = None
        self.df: Optional[pd.DataFrame] = None
        self._model_loaded = False
        
        # State tracking for live trading
        self.position_side: Optional[str] = None
        self.position_size: float = 0.0
        self.entry_price: float = 0.0
        
        if model_path:
            self.load_model(model_path)
    
    def load_model(self, model_path: str) -> bool:
        """Load trained PPO model"""
        try:
            self.agent = PPOAgent(
                state_dim=37,  # n_features per timestep (31 base + 6 account features)
                n_actions=15,  # Updated for short-selling support
                device=self.device
            )
            self.agent.load(model_path)
            self._model_loaded = True
            self.log.info(f"Loaded RL model from {model_path}")
            return True
        except Exception as e:
            self.log.error(f"Failed to load model: {e}")
            self._model_loaded = False
            return False
    
    def prepare_data(self, df: pd.DataFrame) -> pd.DataFrame:
        """Prepare data with features"""
        df = df.copy()
        close = df['close'].values
        high = df['high'].values
        low = df['low'].values
        volume = df['volume'].values
        
        # Returns at different horizons
        for p in [1, 3, 6, 12, 24]:
            if len(close) > p:
                df[f'return_{p}h'] = df['close'].pct_change(p)
        
        # RSI
        delta = pd.Series(close).diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        df['rsi'] = 100 - (100 / (1 + rs))
        df['rsi'] = df['rsi'].fillna(50)
        
        # Price vs rolling min/max
        for w in [10, 20, 50]:
            df[f'price_vs_high_{w}'] = (close / pd.Series(high).rolling(w).max().values) - 1
            df[f'price_vs_low_{w}'] = (close / pd.Series(low).rolling(w).min().values) - 1
        
        # Volume features
        vol_series = pd.Series(volume)
        df['volume_ratio'] = vol_series / vol_series.rolling(20).mean().replace(0, np.nan)
        df['volume_trend'] = vol_series.rolling(5).mean() / vol_series.rolling(20).mean().replace(0, np.nan)
        
        # Volatility
        df['volatility_10'] = pd.Series(close).pct_change().rolling(10).std()
        df['volatility_30'] = pd.Series(close).pct_change().rolling(30).std()
        
        # Moving averages
        for w in [7, 14, 21, 50]:
            ma = pd.Series(close).rolling(w).mean()
            ma_safe = ma.ffill().bfill().fillna(close.iloc[0] if hasattr(close, 'iloc') else close[0])
            df[f'ma_{w}_ratio'] = close / ma_safe.replace(0, np.nan).ffill()
        
        # Time features
        df['hour'] = pd.to_datetime(df.index).hour if hasattr(df.index, 'hour') else 0
        df['day_of_week'] = pd.to_datetime(df.index).dayofweek if hasattr(df.index, 'dayofweek') else 0
        df['hour_sin'] = np.sin(2 * np.pi * df['hour'] / 24)
        df['hour_cos'] = np.cos(2 * np.pi * df['hour'] / 24)
        
        # Acceleration and range
        df['acceleration'] = pd.Series(close).diff().diff()
        df['hl_range'] = (high - low) / close
        df['hl_range_ma'] = df['hl_range'].rolling(14).mean()
        
        return df.fillna(0)
    
    def get_state(self, df: pd.DataFrame) -> np.ndarray:
        """Build state observation from recent data"""
        seq_len = 10
        n_features = 37  # 31 base + 6 account features
        
        # Get last seq_len rows
        window = df.iloc[-seq_len:].copy()
        
        # Feature columns (must match training)
        feature_cols = [
            'close', 'high', 'low', 'open', 'volume',
            'return_1h', 'return_3h', 'return_6h', 'return_12h', 'return_24h',
            'rsi', 'price_vs_high_10', 'price_vs_low_10',
            'price_vs_high_20', 'price_vs_low_20',
            'price_vs_high_50', 'price_vs_low_50',
            'volume_ratio', 'volume_trend',
            'volatility_10', 'volatility_30',
            'ma_7_ratio', 'ma_14_ratio', 'ma_21_ratio', 'ma_50_ratio',
            'hour_sin', 'hour_cos', 'day_of_week',
            'acceleration', 'hl_range', 'hl_range_ma'
        ]
        
        # Build state
        state = np.zeros((seq_len, n_features), dtype=np.float32)
        
        for i, (idx, row) in enumerate(window.iterrows()):
            features = []
            
            # Market features
            for col in feature_cols[:31]:  # All 31 base features
                val = row.get(col, 0.0)
                if pd.isna(val):
                    val = 0.0
                features.append(val)
            
            # Account features (for current step only)
            if i == len(window) - 1:
                current_price = row['close']
                
                # Position size % - normalize by account balance (default $263.40 for your setup)
                ACCOUNT_BALANCE = 263.40  # Can be parameterized via generate_signal
                if self.position_side:
                    position_pct = min(self.position_size * current_price / ACCOUNT_BALANCE, 1.0)
                else:
                    position_pct = 0.0
                features.append(position_pct)
                
                # Unrealized PnL %
                if self.position_side and self.entry_price > 0:
                    if self.position_side == 'long':
                        unreal_pnl = (current_price - self.entry_price) / self.entry_price
                    else:
                        unreal_pnl = (self.entry_price - current_price) / self.entry_price
                else:
                    unreal_pnl = 0.0
                features.append(unreal_pnl)
                
                # Time in position (placeholder - would track actual)
                features.append(0.0)
                
                # Recent return
                if len(df) >= 5:
                    recent_ret = (df['close'].iloc[-1] - df['close'].iloc[-5]) / df['close'].iloc[-5]
                else:
                    recent_ret = 0.0
                features.append(recent_ret)
                
                # Recent volatility
                if len(df) >= 10:
                    recent_vol = df['close'].pct_change().iloc[-10:].std()
                else:
                    recent_vol = 0.0
                features.append(recent_vol)
                
                # Equity to peak (placeholder)
                features.append(1.0)
            else:
                features.extend([0.0] * 6)
            
            state[i] = features[:n_features]
        
        return state
    
    def generate_signal(
        self,
        df: pd.DataFrame,
        current_position: Optional[str] = None,
        position_size: float = 0.0,
        entry_price: float = 0.0
    ) -> Dict:
        """
        Generate trading signal from RL agent
        
        Args:
            df: Recent price data
            current_position: 'long', 'short', or None
            position_size: Current position size
            entry_price: Entry price of current position
        
        Returns:
            Signal dictionary with action, confidence, reasoning
        """
        # Update position tracking
        self.position_side = current_position
        self.position_size = position_size
        self.entry_price = entry_price
        
        if not self._model_loaded or self.agent is None:
            return {
                'signal': 'HOLD',
                'confidence': 0.0,
                'reason': 'RL model not loaded',
                'action_type': None,
                'position_pct': 0.0
            }
        
        # Prepare data
        df = self.prepare_data(df)
        
        if len(df) < 50:
            return {
                'signal': 'HOLD',
                'confidence': 0.0,
                'reason': 'Insufficient data',
                'action_type': None,
                'position_pct': 0.0
            }
        
        # Get state and predict (deterministic=True for live trading consistency)
        state = self.get_state(df)
        action, log_prob, value = self.agent.select_action(state, deterministic=True)
        
        # Map action to signal (updated for 15 actions with short support)
        action_map = {
            0: ('HOLD', None, 0.0),
            1: ('BUY', 'long', 0.25),
            2: ('BUY', 'long', 0.50),
            3: ('BUY', 'long', 1.00),
            4: ('SELL', 'reduce_long', 0.25),
            5: ('SELL', 'reduce_long', 0.50),
            6: ('SELL', 'close_long', 1.00),
            7: ('CLOSE', 'close_long', 1.00),
            8: ('CLOSE', 'close_short', 1.00),
            9: ('SHORT', 'short', 0.25),
            10: ('SHORT', 'short', 0.50),
            11: ('SHORT', 'short', 1.00),
            12: ('COVER', 'reduce_short', 0.25),
            13: ('COVER', 'reduce_short', 0.50),
            14: ('COVER', 'close_short', 1.00)
        }
        
        signal_type, action_type, position_pct = action_map.get(action, ('HOLD', None, 0.0))
        
        # Calculate confidence using softmax probabilities (more accurate than exp(log_prob))
        confidence = self._get_action_confidence(state, action)
        
        # Override based on current position
        if current_position == 'long' and signal_type == 'BUY':
            signal_type = 'HOLD'  # Already long, don't double buy
        
        if current_position is None and signal_type in ['SELL', 'CLOSE']:
            signal_type = 'HOLD'  # No position to close
        
        return {
            'signal': signal_type,
            'confidence': float(confidence),
            'reason': f'RL agent action {action} (confidence: {confidence:.3f}, value: {value:.3f})',
            'action_type': action_type,
            'position_pct': position_pct,
            'raw_action': action
        }
    
    def _get_action_confidence(self, state: np.ndarray, action: int) -> float:
        """Get probability of chosen action from policy using softmax"""
        try:
            with torch.no_grad():
                state_tensor = torch.FloatTensor(state).unsqueeze(0).to(self.agent.device)
                action_logits, _ = self.agent.network(state_tensor)
                probs = torch.softmax(action_logits, dim=-1)
                confidence = probs[0, action].item()
            return confidence
        except Exception as e:
            self.log.warning(f"Could not calculate softmax confidence: {e}")
            # `action` is an integer action index, not a log-prob — exp(action)
            # would return absurd values (e.g. e^14 ≈ 1.2M). Fall back to a
            # neutral 0.0 so a confidence-failure can never inflate a signal.
            return 0.0
    
    def update_position(self, side: Optional[str], size: float, price: float):
        """Update tracked position state"""
        self.position_side = side
        self.position_size = size
        self.entry_price = price
    
    def get_model_info(self) -> Dict:
        """Get information about loaded model"""
        if not self._model_loaded or self.agent is None:
            return {'loaded': False}
        
        return {
            'loaded': True,
            'training_steps': self.agent.training_step,
            'symbol': self.symbol,
            'leverage': self.leverage,
            'confidence_threshold': self.confidence_threshold
        }
