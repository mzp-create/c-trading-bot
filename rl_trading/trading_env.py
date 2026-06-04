"""
Trading Environment for RL
OpenAI Gym-style environment for crypto trading
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from enum import Enum


class Action(Enum):
    """Discrete actions for position management"""
    HOLD = 0
    BUY_SMALL = 1   # Open/increase long 25%
    BUY_MEDIUM = 2  # Open/increase long 50%
    BUY_LARGE = 3   # Open/increase long 100%
    SELL_SMALL = 4  # Close long 25%
    SELL_MEDIUM = 5 # Close long 50%
    SELL_LARGE = 6  # Close long 100%
    CLOSE_LONG = 7
    CLOSE_SHORT = 8
    SHORT_SMALL = 9   # Open/increase short 25%
    SHORT_MEDIUM = 10 # Open/increase short 50%
    SHORT_LARGE = 11  # Open/increase short 100%
    COVER_SMALL = 12  # Close short 25%
    COVER_MEDIUM = 13 # Close short 50%
    COVER_LARGE = 14  # Close short 100%


@dataclass
class Position:
    """Track open position state"""
    side: str  # 'long' or 'short'
    entry_price: float
    size: float  # position size in base currency
    entry_time: int
    
    def pnl_pct(self, current_price: float) -> float:
        """Unrealized PnL percentage"""
        if self.side == 'long':
            return (current_price - self.entry_price) / self.entry_price
        else:
            return (self.entry_price - current_price) / self.entry_price
    
    def unrealized_pnl(self, current_price: float) -> float:
        """Unrealized PnL in quote currency"""
        return self.size * self.pnl_pct(current_price)


class TradingEnv:
    """
    Crypto trading environment for RL training
    
    State space: 40+ features
    Action space: 9 discrete actions
    Reward: Profit - risk_penalty - drawdown_penalty + consistency_bonus
    """
    
    def __init__(
        self,
        df: pd.DataFrame,
        initial_balance: float = 1000.0,
        leverage: float = 5.0,
        commission: float = 0.0004,  # 0.04%
        max_position_pct: float = 0.95,
        risk_free_rate: float = 0.02,  # Annual
        window_size: int = 50,
        seq_len: int = 10,
    ):
        self.df = df.reset_index(drop=True)
        self.initial_balance = initial_balance
        self.leverage = leverage
        self.commission = commission
        self.max_position_pct = max_position_pct
        self.risk_free_rate = risk_free_rate
        self.window_size = window_size
        self.seq_len = seq_len
        
        # State tracking
        self.current_step = 0
        self.balance = initial_balance
        self.position: Optional[Position] = None
        self.trades: List[Dict] = []
        self.equity_curve: List[float] = [initial_balance]
        self.returns: List[float] = []
        self.peak_equity = initial_balance
        self.max_drawdown = 0.0
        
        # Feature dimensions
        self.feature_cols = self._get_feature_columns()
        self.n_features = len(self.feature_cols)
        
    def _get_feature_columns(self) -> List[str]:
        """Define feature columns for state representation"""
        # Technical indicators
        base_features = [
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
        
        # Account state features (computed dynamically)
        account_features = [
            'position_size_pct',  # Current position as % of balance
            'unrealized_pnl_pct',  # Unrealized PnL %
            'time_in_position',   # Steps since entry
            'recent_return',      # Return over last 5 steps
            'recent_volatility',  # Std of returns last 10 steps
            'equity_to_peak',     # Current equity / peak equity
        ]
        
        return base_features + account_features
    
    def reset(self) -> np.ndarray:
        """Reset environment to initial state"""
        self.current_step = self.window_size
        self.balance = self.initial_balance
        self.position = None
        self.trades = []
        self.equity_curve = [self.initial_balance]
        self.returns = []
        self.peak_equity = self.initial_balance
        self.max_drawdown = 0.0
        
        return self._get_observation()
    
    def _get_observation(self) -> np.ndarray:
        """Get current state observation (seq_len x n_features)"""
        # Get window of data
        start_idx = max(0, self.current_step - self.seq_len + 1)
        end_idx = self.current_step + 1
        
        window_df = self.df.iloc[start_idx:end_idx]
        
        # Build feature matrix
        features = []
        for i in range(len(window_df)):
            row_features = []
            row = window_df.iloc[i]
            
            # Market features (first 31 are market data from feature_cols)
            for col in self.feature_cols[:31]:  # Base market features
                val = row.get(col, 0.0)
                if pd.isna(val):
                    val = 0.0
                row_features.append(val)
            
            # Account state features (only for current step)
            if i == len(window_df) - 1:
                current_price = row['close']
                
                # Position size %
                if self.position:
                    position_value = self.position.size * current_price
                    position_pct = min(position_value / (self.balance + position_value), 1.0)
                else:
                    position_pct = 0.0
                row_features.append(position_pct)
                
                # Unrealized PnL %
                if self.position:
                    unreal_pnl = self.position.pnl_pct(current_price)
                else:
                    unreal_pnl = 0.0
                row_features.append(unreal_pnl)
                
                # Time in position
                if self.position:
                    time_in = self.current_step - self.position.entry_time
                else:
                    time_in = 0
                row_features.append(min(time_in / 100, 1.0))  # Normalize
                
                # Recent return (5 steps)
                if len(self.returns) >= 5:
                    recent_ret = np.mean(self.returns[-5:])
                else:
                    recent_ret = 0.0
                row_features.append(recent_ret)
                
                # Recent volatility (10 steps)
                if len(self.returns) >= 10:
                    recent_vol = np.std(self.returns[-10:])
                else:
                    recent_vol = 0.0
                row_features.append(recent_vol)
                
                # Equity to peak
                current_equity = self._get_equity(current_price)
                equity_to_peak = current_equity / self.peak_equity if self.peak_equity > 0 else 1.0
                row_features.append(equity_to_peak)
                
            else:
                # Pad account features for historical steps
                row_features.extend([0.0] * 6)
            
            features.append(row_features)
        
        # Pad if needed
        while len(features) < self.seq_len:
            features.insert(0, [0.0] * len(self.feature_cols))
        
        return np.array(features, dtype=np.float32)
    
    def _get_equity(self, current_price: float) -> float:
        """Calculate total equity (balance + position value)"""
        equity = self.balance
        if self.position:
            equity += self.position.unrealized_pnl(current_price)
        return equity
    
    def step(self, action: int) -> Tuple[np.ndarray, float, bool, Dict]:
        """
        Execute one trading step
        
        Returns: observation, reward, done, info
        """
        action_enum = Action(action)
        current_row = self.df.iloc[self.current_step]
        current_price = current_row['close']
        
        # Execute action
        trade_executed = False
        trade_pnl = 0.0
        
        if action_enum == Action.HOLD:
            pass
            
        elif action_enum in [Action.BUY_SMALL, Action.BUY_MEDIUM, Action.BUY_LARGE]:
            if self.position is None or self.position.side == 'short':
                # Close short first if exists
                if self.position and self.position.side == 'short':
                    trade_pnl = self._close_position(current_price)
                    trade_executed = True
                
                # Open long
                size_map = {
                    Action.BUY_SMALL: 0.25,
                    Action.BUY_MEDIUM: 0.50,
                    Action.BUY_LARGE: 1.00
                }
                self._open_position('long', current_price, size_map[action_enum])
                trade_executed = True
                
        elif action_enum in [Action.SELL_SMALL, Action.SELL_MEDIUM, Action.SELL_LARGE]:
            if self.position and self.position.side == 'long':
                close_pct = {
                    Action.SELL_SMALL: 0.25,
                    Action.SELL_MEDIUM: 0.50,
                    Action.SELL_LARGE: 1.00
                }[action_enum]
                trade_pnl = self._close_partial(current_price, close_pct)
                trade_executed = True
                
        elif action_enum == Action.CLOSE_LONG:
            if self.position and self.position.side == 'long':
                trade_pnl = self._close_position(current_price)
                trade_executed = True
                
        elif action_enum == Action.CLOSE_SHORT:
            if self.position and self.position.side == 'short':
                trade_pnl = self._close_position(current_price)
                trade_executed = True
        
        # Move to next step
        self.current_step += 1
        done = self.current_step >= len(self.df) - 1
        
        # Calculate new equity
        next_price = self.df.iloc[self.current_step]['close']
        new_equity = self._get_equity(next_price)
        
        # Update tracking
        self.equity_curve.append(new_equity)
        if len(self.equity_curve) > 1:
            ret = (self.equity_curve[-1] - self.equity_curve[-2]) / self.equity_curve[-2]
            self.returns.append(ret)
        
        # Update peak and drawdown
        if new_equity > self.peak_equity:
            self.peak_equity = new_equity
        drawdown = (self.peak_equity - new_equity) / self.peak_equity
        self.max_drawdown = max(self.max_drawdown, drawdown)
        
        # Calculate reward
        reward = self._calculate_reward(
            old_equity=self.equity_curve[-2] if len(self.equity_curve) > 1 else self.initial_balance,
            new_equity=new_equity,
            trade_pnl=trade_pnl,
            trade_executed=trade_executed,
            drawdown=drawdown
        )
        
        # Info dict
        info = {
            'step': self.current_step,
            'equity': new_equity,
            'balance': self.balance,
            'position': self.position.side if self.position else None,
            'position_size': self.position.size if self.position else 0,
            'unrealized_pnl': self.position.unrealized_pnl(next_price) if self.position else 0,
            'trade_executed': trade_executed,
            'trade_pnl': trade_pnl,
            'drawdown': drawdown,
            'max_drawdown': self.max_drawdown,
            'total_return_pct': (new_equity - self.initial_balance) / self.initial_balance * 100
        }
        
        return self._get_observation(), reward, done, info
    
    def _open_position(self, side: str, price: float, size_pct: float):
        """Open a new position"""
        position_value = self.balance * size_pct * self.leverage
        size = position_value / price
        
        # Commission
        commission = position_value * self.commission
        self.balance -= commission
        
        self.position = Position(
            side=side,
            entry_price=price,
            size=size,
            entry_time=self.current_step
        )
    
    def _close_position(self, price: float) -> float:
        """Close entire position"""
        if not self.position:
            return 0.0
        
        pnl = self.position.unrealized_pnl(price)
        position_value = self.position.size * price
        
        # Commission
        commission = position_value * self.commission
        
        # Update balance
        self.balance += pnl - commission
        
        # Record trade
        self.trades.append({
            'entry_time': self.position.entry_time,
            'exit_time': self.current_step,
            'side': self.position.side,
            'entry_price': self.position.entry_price,
            'exit_price': price,
            'size': self.position.size,
            'pnl': pnl,
            'pnl_pct': self.position.pnl_pct(price) * 100,
            'duration': self.current_step - self.position.entry_time
        })
        
        realized_pnl = pnl
        self.position = None
        
        return realized_pnl
    
    def _close_partial(self, price: float, close_pct: float) -> float:
        """Close partial position"""
        if not self.position:
            return 0.0
        
        close_size = self.position.size * close_pct
        close_value = close_size * price
        
        # PnL for closed portion
        if self.position.side == 'long':
            pnl = close_size * (price - self.position.entry_price)
        else:
            pnl = close_size * (self.position.entry_price - price)
        
        # Commission
        commission = close_value * self.commission
        
        # Update balance
        self.balance += pnl - commission
        
        # Reduce position size
        self.position.size -= close_size
        
        if self.position.size <= 0:
            self.position = None
        
        return pnl
    
    def _calculate_reward(
        self,
        old_equity: float,
        new_equity: float,
        trade_pnl: float,
        trade_executed: bool,
        drawdown: float
    ) -> float:
        """
        Multi-component reward function
        
        Components:
        1. Profit reward: Direct PnL
        2. Risk penalty: Penalize drawdowns
        3. Consistency bonus: Reward steady gains
        4. Trade efficiency: Reward profitable trades
        5. Holding penalty: Small penalty for doing nothing
        """
        reward = 0.0
        
        # 1. Profit reward (scaled)
        equity_return = (new_equity - old_equity) / old_equity
        reward += equity_return * 100  # Scale up
        
        # 2. Drawdown penalty (exponential)
        if drawdown > 0.05:  # 5% threshold
            dd_penalty = -10 * (drawdown ** 2)
            reward += dd_penalty
        
        # 3. Trade PnL bonus
        if trade_executed and trade_pnl > 0:
            reward += 2.0  # Bonus for profitable trade
        elif trade_executed and trade_pnl < 0:
            reward -= 1.0  # Smaller penalty for losses (already in equity_return)
        
        # 4. Consistency bonus (sharpe-like)
        if len(self.returns) >= 10:
            recent_returns = np.array(self.returns[-10:])
            mean_ret = np.mean(recent_returns)
            std_ret = np.std(recent_returns) + 1e-8
            sharpe_like = mean_ret / std_ret
            reward += sharpe_like * 0.5
        
        # 5. Small penalty for holding too long without profit
        if self.position:
            time_held = self.current_step - self.position.entry_time
            if time_held > 50 and equity_return < 0:
                reward -= 0.1  # Penalty for bag holding
        
        # Clip reward for stability
        return np.clip(reward, -10, 10)
    
    def get_performance_metrics(self) -> Dict:
        """Calculate final performance metrics"""
        if not self.trades:
            return {
                'total_trades': 0,
                'win_rate': 0.0,
                'profit_factor': 0.0,
                'sharpe_ratio': 0.0,
                'max_drawdown_pct': 0.0,
                'total_return_pct': 0.0
            }
        
        profits = [t['pnl'] for t in self.trades if t['pnl'] > 0]
        losses = [t['pnl'] for t in self.trades if t['pnl'] < 0]
        
        win_rate = len(profits) / len(self.trades) if self.trades else 0
        
        gross_profit = sum(profits) if profits else 0
        gross_loss = abs(sum(losses)) if losses else 1e-8
        profit_factor = gross_profit / gross_loss
        
        # Sharpe ratio
        if len(self.returns) > 10:
            returns_arr = np.array(self.returns)
            sharpe = (np.mean(returns_arr) * 252) / (np.std(returns_arr) * np.sqrt(252) + 1e-8)
        else:
            sharpe = 0.0
        
        total_return = (self.equity_curve[-1] - self.initial_balance) / self.initial_balance
        
        return {
            'total_trades': len(self.trades),
            'winning_trades': len(profits),
            'losing_trades': len(losses),
            'win_rate': win_rate * 100,
            'profit_factor': profit_factor,
            'sharpe_ratio': sharpe,
            'max_drawdown_pct': self.max_drawdown * 100,
            'total_return_pct': total_return * 100,
            'final_equity': self.equity_curve[-1],
            'avg_trade_pnl': np.mean([t['pnl'] for t in self.trades]),
            'avg_trade_duration': np.mean([t['duration'] for t in self.trades])
        }
