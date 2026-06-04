"""
RL Training Script for Trading Agent
"""

import numpy as np
import pandas as pd
from pathlib import Path
from typing import Dict, List
import logging
import json
from datetime import datetime
import argparse

from .trading_env import TradingEnv
from .ppo_agent import PPOAgent, ReplayBuffer


def load_data(symbol: str = "BTC/USDT", timeframe: str = "1h", data_dir: str = "data/ohlcv") -> pd.DataFrame:
    """Load and prepare OHLCV data"""
    safe_symbol = symbol.replace('/', '_')
    file_path = Path(data_dir) / f"{safe_symbol}_{timeframe}.csv"
    
    if not file_path.exists():
        raise FileNotFoundError(f"Data file not found: {file_path}")
    
    df = pd.read_csv(file_path)
    
    # Handle both millisecond and second timestamps
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    
    # Check data freshness
    last_timestamp = df.index[-1]
    days_old = (datetime.now() - last_timestamp).days
    if days_old > 7:
        logging.warning(f"Data is {days_old} days old! Consider refreshing for current market conditions.")
    
    # Validate data quality
    if df.isnull().any().any():
        logging.warning("Data contains NaN values - filling with zeros")
        df = df.fillna(0)
    
    if len(df) < 200:
        raise ValueError(f"Insufficient data: only {len(df)} rows (need >= 200)")
    
    # Add engineered features if missing
    df = add_features(df)
    
    return df


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add technical features to dataframe"""
    df = df.copy()
    close = df['close'].values
    high = df['high'].values
    low = df['low'].values
    volume = df['volume'].values
    
    # Returns
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
        # Handle division by zero: if MA is 0 or NaN, forward fill then use close[0]
        ma_safe = ma.replace(0, np.nan).ffill().fillna(close[0] if len(close) > 0 else 1.0)
        df[f'ma_{w}_ratio'] = close / ma_safe
    
    # Time features
    df['hour'] = df.index.hour
    df['day_of_week'] = df.index.dayofweek
    df['hour_sin'] = np.sin(2 * np.pi * df['hour'] / 24)
    df['hour_cos'] = np.cos(2 * np.pi * df['hour'] / 24)
    
    # Acceleration and range
    df['acceleration'] = pd.Series(close).diff().diff()
    df['hl_range'] = (high - low) / close
    df['hl_range_ma'] = df['hl_range'].rolling(14).mean()
    
    return df.fillna(0)


def train_agent(
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    total_episodes: int = 500,
    steps_per_episode: int = 500,
    save_dir: str = "data/models/rl",
    log_interval: int = 10,
    eval_interval: int = 50,
    resume_from: str = None
) -> Dict:
    """
    Train PPO agent on historical data
    
    Args:
        symbol: Trading pair
        timeframe: Data timeframe
        total_episodes: Number of training episodes
        steps_per_episode: Max steps per episode
        save_dir: Directory to save models
        log_interval: Log every N episodes
        eval_interval: Evaluate every N episodes
        resume_from: Path to checkpoint to resume from
    
    Returns:
        Training history dictionary
    """
    # Get logger (don't configure, let caller configure)
    log = logging.getLogger("RLTraining")
    
    # Load data
    log.info(f"Loading data for {symbol} ({timeframe})")
    df = load_data(symbol, timeframe)
    log.info(f"Loaded {len(df)} data points")
    
    # Split train/val
    split_idx = int(len(df) * 0.8)
    train_df = df.iloc[:split_idx]
    val_df = df.iloc[split_idx:]
    
    # Create environment
    env = TradingEnv(
        df=train_df,
        initial_balance=1000.0,
        leverage=5.0,
        window_size=50
    )
    
    # Create agent
    agent = PPOAgent(
        state_dim=env.n_features,
        n_actions=15,  # Updated for short-selling support
        lr=3e-4,
        gamma=0.99,
        gae_lambda=0.95
    )
    
    # Resume from checkpoint if provided
    start_episode = 1
    if resume_from and Path(resume_from).exists():
        log.info(f"Resuming from checkpoint: {resume_from}")
        agent.load(resume_from)
        # Try to parse episode number from filename
        try:
            start_episode = int(resume_from.split('ep')[-1].split('.')[0])
            log.info(f"Starting from episode {start_episode}")
        except (ValueError, IndexError):
            log.info("Could not parse episode number, starting from 1")
    
    # Training tracking
    history = {
        'episodes': [],
        'rewards': [],
        'returns': [],
        'win_rates': [],
        'sharpe_ratios': [],
        'max_drawdowns': [],
        'eval_returns': []
    }
    
    save_path = Path(save_dir)
    save_path.mkdir(parents=True, exist_ok=True)
    
    best_return = -np.inf
    episodes_without_improvement = 0
    early_stopping_patience = 50  # Stop if no improvement for 50 eval intervals
    
    for episode in range(start_episode, total_episodes + 1):
        # Sample random starting point
        start_idx = np.random.randint(50, len(train_df) - steps_per_episode - 1)
        episode_df = train_df.iloc[start_idx:start_idx + steps_per_episode + 100]
        
        env = TradingEnv(
            df=episode_df,
            initial_balance=1000.0,
            leverage=5.0,
            window_size=50
        )
        
        state = env.reset()
        buffer = ReplayBuffer()
        episode_reward = 0
        
        for step in range(steps_per_episode):
            # Select action
            action, log_prob, value = agent.select_action(state, deterministic=False)
            
            # Take step
            next_state, reward, done, info = env.step(action)
            
            # Store transition
            buffer.add(state, action, reward, log_prob, value, done)
            
            episode_reward += reward
            state = next_state
            
            # Update policy after enough steps OR when episode ends (don't lose last transitions)
            if len(buffer) >= 2048 or done:
                if len(buffer) >= 64:  # Only update if we have enough samples
                    metrics = agent.update(buffer, next_state, n_epochs=4, batch_size=64)
                buffer.clear()  # Always clear, but only update if enough data
            
            if done:
                break
        
        # Get episode metrics
        metrics = env.get_performance_metrics()
        
        history['episodes'].append(episode)
        history['rewards'].append(episode_reward)
        history['returns'].append(metrics['total_return_pct'])
        history['win_rates'].append(metrics['win_rate'])
        history['sharpe_ratios'].append(metrics['sharpe_ratio'])
        history['max_drawdowns'].append(metrics['max_drawdown_pct'])
        
        # Logging
        if episode % log_interval == 0:
            avg_reward = np.mean(history['rewards'][-log_interval:])
            avg_return = np.mean(history['returns'][-log_interval:])
            log.info(
                f"Episode {episode}/{total_episodes} | "
                f"Avg Reward: {avg_reward:.2f} | "
                f"Return: {avg_return:.2f}% | "
                f"Win Rate: {metrics['win_rate']:.1f}% | "
                f"Trades: {metrics['total_trades']}"
            )
        
        # Evaluation
        if episode % eval_interval == 0:
            eval_return = evaluate_agent(agent, val_df, n_episodes=5, steps_per_episode=steps_per_episode)
            history['eval_returns'].append(eval_return)
            log.info(f"Evaluation Return: {eval_return:.2f}%")
            
            # Save best model
            if eval_return > best_return:
                best_return = eval_return
                episodes_without_improvement = 0
                model_path = save_path / f"{symbol.replace('/', '_')}_ppo_best.pt"
                agent.save(str(model_path))
                log.info(f"Saved best model (return: {best_return:.2f}%)")
            else:
                episodes_without_improvement += 1
                if episodes_without_improvement >= early_stopping_patience:
                    log.info(f"Early stopping triggered: no improvement for {early_stopping_patience} eval intervals")
                    break
        
        # Save checkpoint
        if episode % 100 == 0:
            checkpoint_path = save_path / f"{symbol.replace('/', '_')}_ppo_ep{episode}.pt"
            agent.save(str(checkpoint_path))
            
            # Save history
            history_path = save_path / f"{symbol.replace('/', '_')}_history.json"
            with open(history_path, 'w') as f:
                json.dump(history, f, indent=2)
    
    log.info("Training complete!")
    return history


def evaluate_agent(agent: PPOAgent, df: pd.DataFrame, n_episodes: int = 5, steps_per_episode: int = 50) -> float:
    """
    Evaluate agent on validation data
    
    Args:
        agent: Trained PPO agent
        df: Validation dataframe
        n_episodes: Number of evaluation episodes
        steps_per_episode: Steps per episode (should match training)
    
    Returns:
        Average return across episodes
    """
    returns = []
    min_data_needed = steps_per_episode + 60  # window + steps + buffer
    
    # If not enough data for full episodes, use what's available
    if len(df) < min_data_needed:
        # Run single episode on all available data
        env = TradingEnv(df=df, initial_balance=1000.0, leverage=5.0, window_size=50)
        state = env.reset()
        done = False
        steps = 0
        max_steps = len(df) - 51  # Leave room for window
        
        while not done and steps < max_steps:
            action, _, _ = agent.select_action(state, deterministic=True)
            state, _, done, _ = env.step(action)
            steps += 1
        
        metrics = env.get_performance_metrics()
        return metrics['total_return_pct']
    
    for _ in range(n_episodes):
        # Sample random episode
        start_idx = np.random.randint(50, len(df) - steps_per_episode)
        episode_df = df.iloc[start_idx:start_idx + steps_per_episode + 50]
        
        env = TradingEnv(df=episode_df, initial_balance=1000.0, leverage=5.0, window_size=50)
        state = env.reset()
        done = False
        
        while not done:
            action, _, _ = agent.select_action(state, deterministic=True)
            state, _, done, _ = env.step(action)
        
        metrics = env.get_performance_metrics()
        returns.append(metrics['total_return_pct'])
    
    return np.mean(returns) if returns else 0.0


def main():
    parser = argparse.ArgumentParser(description="Train RL trading agent")
    parser.add_argument("--symbol", default="BTC/USDT", help="Trading pair")
    parser.add_argument("--timeframe", default="1h", help="Data timeframe")
    parser.add_argument("--episodes", type=int, default=500, help="Number of episodes")
    parser.add_argument("--steps", type=int, default=500, help="Steps per episode")
    parser.add_argument("--save-dir", default="data/models/rl", help="Model save directory")
    parser.add_argument("--resume", type=str, default=None, help="Resume from checkpoint path")
    parser.add_argument("--quiet", action="store_true", help="Reduce logging verbosity")
    
    args = parser.parse_args()
    
    # Only configure logging if not already configured
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.WARNING if args.quiet else logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
    
    history = train_agent(
        symbol=args.symbol,
        timeframe=args.timeframe,
        total_episodes=args.episodes,
        steps_per_episode=args.steps,
        save_dir=args.save_dir,
        resume_from=args.resume
    )
    
    print(f"\nTraining complete! Final stats:")
    print(f"  Avg Return (last 50): {np.mean(history['returns'][-50:]):.2f}%")
    print(f"  Best Eval Return: {max(history['eval_returns']) if history['eval_returns'] else 0:.2f}%")


if __name__ == "__main__":
    main()
