#!/usr/bin/env python3
"""
Run RL Training - Entry point for training the trading agent
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rl_trading.train_rl import train_agent
import argparse
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


def main():
    parser = argparse.ArgumentParser(
        description="Train RL trading agent with PPO",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Train on BTC/USDThourly data
  python run_training.py --symbol BTC/USDT --timeframe 1h --episodes 1000
  
  # Train on ETH with custom parameters
  python run_training.py --symbol ETH/USDT --episodes 500 --steps 1000
  
  # Resume training from checkpoint (will load latest)
  python run_training.py --symbol BTC/USDT --resume
        """
    )
    
    parser.add_argument(
        '--symbol', '-s',
        default='BTC/USDT',
        help='Trading pair to train on (default: BTC/USDT)'
    )
    
    parser.add_argument(
        '--timeframe', '-t',
        default='1h',
        choices=['5m', '15m', '1h', '4h'],
        help='Data timeframe (default: 1h)'
    )
    
    parser.add_argument(
        '--episodes', '-e',
        type=int,
        default=500,
        help='Number of training episodes (default: 500)'
    )
    
    parser.add_argument(
        '--steps',
        type=int,
        default=500,
        help='Steps per episode (default: 500)'
    )
    
    parser.add_argument(
        '--save-dir',
        default='data/models/rl',
        help='Directory to save trained models'
    )
    
    parser.add_argument(
        '--data-dir',
        default='data/ohlcv',
        help='Directory containing OHLCV data files'
    )
    
    parser.add_argument(
        '--learning-rate', '-lr',
        type=float,
        default=3e-4,
        help='Learning rate for PPO (default: 0.0003)'
    )
    
    parser.add_argument(
        '--gamma',
        type=float,
        default=0.99,
        help='Discount factor (default: 0.99)'
    )
    
    parser.add_argument(
        '--leverage', '-l',
        type=float,
        default=5.0,
        help='Trading leverage for simulation (default: 5.0)'
    )
    
    parser.add_argument(
        '--initial-balance',
        type=float,
        default=1000.0,
        help='Initial balance for simulation (default: 1000)'
    )
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("RL TRADING AGENT - TRAINING")
    print("=" * 60)
    print(f"Symbol:        {args.symbol}")
    print(f"Timeframe:     {args.timeframe}")
    print(f"Episodes:      {args.episodes}")
    print(f"Steps/episode: {args.steps}")
    print(f"Leverage:      {args.leverage}x")
    print(f"Learning rate: {args.learning_rate}")
    print(f"Save dir:      {args.save_dir}")
    print("=" * 60)
    print()
    
    # Run training
    history = train_agent(
        symbol=args.symbol,
        timeframe=args.timeframe,
        total_episodes=args.episodes,
        steps_per_episode=args.steps,
        save_dir=args.save_dir
    )
    
    print("\n" + "=" * 60)
    print("TRAINING COMPLETE")
    print("=" * 60)
    
    if history:
        import numpy as np
        recent_returns = history['returns'][-50:] if len(history['returns']) >= 50 else history['returns']
        recent_winrates = history['win_rates'][-50:] if len(history['win_rates']) >= 50 else history['win_rates']
        
        print(f"Final Avg Return:    {np.mean(recent_returns):.2f}%")
        print(f"Final Avg Win Rate:  {np.mean(recent_winrates):.1f}%")
        print(f"Best Eval Return:    {max(history['eval_returns']) if history['eval_returns'] else 0:.2f}%")
        print(f"Training Episodes:   {len(history['episodes'])}")
        
        # List saved models
        import glob
        model_files = glob.glob(f"{args.save_dir}/{args.symbol.replace('/', '_')}*.pt")
        if model_files:
            print(f"\nSaved models ({len(model_files)}):")
            for f in sorted(model_files)[-5:]:  # Show last 5
                print(f"  - {f}")
    
    print("=" * 60)


if __name__ == "__main__":
    main()
