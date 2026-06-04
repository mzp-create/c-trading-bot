#!/usr/bin/env python3
"""
Test Ensemble Strategy with Live Market Data
Runs ensemble predictions on current market conditions
"""

import sys
sys.path.insert(0, '.')

import pandas as pd
from pathlib import Path
from datetime import datetime

from strategies.ensemble_bot_integration import create_ensemble_bot_strategy
from rl_trading.train_rl import load_data


def test_ensemble_strategy(symbol="BTC/USDT", timeframe="1h"):
    """Test ensemble strategy on current data"""
    
    print("=" * 60)
    print("ENSEMBLE STRATEGY TEST")
    print("=" * 60)
    print(f"Symbol: {symbol}")
    print(f"Timeframe: {timeframe}")
    print()
    
    # Load fresh data
    print("Loading market data...")
    try:
        df = load_data(symbol, timeframe)
        print(f"✅ Loaded {len(df)} rows")
        print(f"   Date range: {df.index[0]} to {df.index[-1]}")
        print(f"   Current price: ${df['close'].iloc[-1]:,.2f}")
        print()
    except Exception as e:
        print(f"❌ Failed to load data: {e}")
        return
    
    # Create ensemble strategy
    print("Initializing ensemble strategy...")
    strategy = create_ensemble_bot_strategy(
        symbol=symbol,
        ml_weight=0.4,
        rl_weight=0.6
        # Uses default confidence_threshold=0.15 from config
    )
    
    print(f"✅ Strategy initialized")
    print(f"   Ready: {strategy.is_ready()}")
    print()
    
    # Get model info
    info = strategy.get_model_info()
    print("Model Status:")
    print(f"   ML Model: {'✅ Loaded' if info['ml_loaded'] else '❌ Not Loaded'}")
    print(f"   RL Model: {'✅ Loaded' if info['rl_loaded'] else '❌ Not Loaded'}")
    print(f"   Weights: ML={info['ml_weight']}, RL={info['rl_weight']}")
    print()
    
    # Test 1: No position
    print("Test 1: No current position")
    print("-" * 40)
    signal = strategy.analyze(
        current_price=df['close'].iloc[-1],
        position={'side': None},
        market_data=df
    )
    print(f"Signal: {signal['signal']}")
    print(f"Strength: {signal['strength']}")
    print(f"Confidence: {signal['confidence']:.3f}")
    print(f"Position %: {signal['position_pct']:.2f}")
    print(f"Reason: {signal['reason']}")
    print()
    
    # Test 2: With long position
    print("Test 2: Currently in LONG position")
    print("-" * 40)
    signal = strategy.analyze(
        current_price=df['close'].iloc[-1],
        position={
            'side': 'long',
            'size': 0.001,
            'entry_price': df['close'].iloc[-1] * 0.98  # 2% below current
        },
        market_data=df
    )
    print(f"Signal: {signal['signal']}")
    print(f"Strength: {signal['strength']}")
    print(f"Confidence: {signal['confidence']:.3f}")
    print(f"Position %: {signal['position_pct']:.2f}")
    print(f"Reason: {signal['reason']}")
    print()
    
    # Test 3: With short position (for dual-instance)
    print("Test 3: Currently in SHORT position")
    print("-" * 40)
    signal = strategy.analyze(
        current_price=df['close'].iloc[-1],
        position={
            'side': 'short',
            'size': 0.001,
            'entry_price': df['close'].iloc[-1] * 1.02  # 2% above current
        },
        market_data=df
    )
    print(f"Signal: {signal['signal']}")
    print(f"Strength: {signal['strength']}")
    print(f"Confidence: {signal['confidence']:.3f}")
    print(f"Position %: {signal['position_pct']:.2f}")
    print(f"Reason: {signal['reason']}")
    print()
    
    # Summary
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Ensemble strategy is {'READY ✅' if strategy.is_ready() else 'NOT READY ❌'}")
    print()
    print("To use in live trading:")
    print("  1. Import: from strategies.ensemble_bot_integration import create_ensemble_bot_strategy")
    print("  2. Create: strategy = create_ensemble_bot_strategy('BTC/USDT')")
    print("  3. Signal:  signal = strategy.analyze(price, position, market_data)")
    print("  4. Trade:   if signal['signal'] == 'BUY' and signal['confidence'] > 0.15:")
    print()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Test ensemble strategy")
    parser.add_argument("--symbol", default="BTC/USDT", help="Trading pair")
    parser.add_argument("--timeframe", default="1h", help="Timeframe")
    args = parser.parse_args()
    
    test_ensemble_strategy(args.symbol, args.timeframe)
