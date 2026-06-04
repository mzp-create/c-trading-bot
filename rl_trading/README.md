# RL Trading Agent - PPO-Based Strategy

A reinforcement learning trading system using **Proximal Policy Optimization (PPO)** that learns optimal position sizing, entries, and exits through trial-and-error on historical data.

## Architecture

```
rl_trading/
├── trading_env.py          # Gym-style trading environment
├── ppo_agent.py            # PPO implementation with LSTM
├── rl_strategy.py          # Live trading strategy wrapper
├── train_rl.py             # Training loop and data handling
├── run_training.py         # CLI entry point
└── README.md               # This file
```

## Key Features

| Component | Description |
|-----------|-------------|
| **LSTM State Representation** | 10-timestep sequence of 36 features captures market context |
| **Discrete Action Space** | 9 actions: HOLD, BUY (3 sizes), SELL (3 sizes), CLOSE (2 types) |
| **Multi-Component Reward** | Profit + Risk Penalty + Consistency Bonus + Trade Efficiency |
| **GAE Advantage Estimation** | Reduces variance in policy gradients |
| **Per-Symbol Models** | Separate trained agents for BTC, ETH, SOL |

## Action Space (15 actions)

| Action | ID | Description | Position Impact |
|--------|-----|-------------|-----------------|
| HOLD | 0 | No action | No change |
| BUY_SMALL | 1 | Open/increase long | +25% position |
| BUY_MEDIUM | 2 | Open/increase long | +50% position |
| BUY_LARGE | 3 | Open/increase long | +100% position |
| SELL_SMALL | 4 | Reduce long position | -25% position |
| SELL_MEDIUM | 5 | Reduce long position | -50% position |
| SELL_LARGE | 6 | Close long position | -100% position |
| CLOSE_LONG | 7 | Close long position | Close all |
| CLOSE_SHORT | 8 | Close short position | Close all |
| SHORT_SMALL | 9 | Open/increase short | +25% short |
| SHORT_MEDIUM | 10 | Open/increase short | +50% short |
| SHORT_LARGE | 11 | Open/increase short | +100% short |
| COVER_SMALL | 12 | Reduce short position | -25% short |
| COVER_MEDIUM | 13 | Reduce short position | -50% short |
| COVER_LARGE | 14 | Close short position | -100% short |

## Reward Function

```
Reward = Profit_Reward + Trade_Bonus - Drawdown_Penalty + Consistency_Bonus - Holding_Penalty

Where:
- Profit_Reward = equity_return × 100
- Trade_Bonus = +2 for profitable trade, -1 for loss
- Drawdown_Penalty = -10 × (drawdown²) if drawdown > 5%
- Consistency_Bonus = Sharpe-like ratio × 0.5
- Holding_Penalty = -0.1 for holding losing positions > 50 steps
```

## State Features (36 total)

**Market Features (30):**
- Price: close, high, low, open, volume
- Returns: 1h, 3h, 6h, 12h, 24h
- RSI, Price vs rolling min/max
- Volume ratio/trend
- Volatility (10, 30 period)
- Moving average ratios (7, 14, 21, 50)
- Time cyclical encoding
- Price acceleration, high-low range

**Account Features (6):**
- Position size %
- Unrealized PnL %
- Time in position
- Recent return (5-step)
- Recent volatility
- Equity to peak ratio

## Quick Start

### 1. Train the Agent

```bash
# Train BTC/USDThourly for 500 episodes
cd /mnt/hermes-data/.hermes/hermes-agent/trading-bot
python rl_trading/run_training.py --symbol BTC/USDT --timeframe 1h --episodes 500

# Train with custom parameters
python rl_trading/run_training.py \
  --symbol ETH/USDT \
  --timeframe 1h \
  --episodes 1000 \
  --steps 1000 \
  --leverage 5.0
```

### 2. Use in Trading Bot

```python
from strategies.rl_strategy_integration import create_rl_strategy

# Create RL strategy
rl = create_rl_strategy(symbol="BTC/USDT")

# Generate signal
signal = rl.analyze(market_data, position_info={
    'side': 'long',
    'size': 0.001,
    'entry_price': 95000
})

print(signal)
# {
#   'signal': 'HOLD',
#   'strength': 'strong',
#   'confidence': 0.82,
#   'strategy': 'rl_agent',
#   'reason': 'RL agent action 0 (log_prob: -0.198, value: 12.5)'
# }
```

## Training Output

Models saved to `data/models/rl/`:
- `{SYMBOL}_ppo_best.pt` - Best model by validation return
- `{SYMBOL}_ppo_ep{N}.pt` - Checkpoints every 100 episodes
- `{SYMBOL}_history.json` - Training metrics

## Training Metrics

The agent tracks:
- Episode reward (cumulative)
- Total return %
- Win rate %
- Sharpe ratio
- Max drawdown %
- Number of trades
- Evaluation return on validation set

## Hyperparameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| Learning Rate | 3e-4 | Adam optimizer LR |
| Gamma | 0.99 | Discount factor |
| GAE Lambda | 0.95 | Advantage estimation λ |
| Clip Epsilon | 0.2 | PPO clipping parameter |
| Value Coef | 0.5 | Value loss weight |
| Entropy Coef | 0.01 | Exploration bonus |
| Batch Size | 64 | Mini-batch size for updates |
| Epochs | 4 | Update epochs per batch |
| Hidden Dim | 256 | LSTM hidden size |

## Requirements

```bash
pip install torch numpy pandas scikit-learn
```

## Performance Expectations

Training 500 episodes on BTC 1h data:
- Time: ~2-4 hours (CPU), ~30-60 min (GPU)
- Target: >50% win rate, >1.5 profit factor
- Best models typically found at 300-500 episodes

## Next Steps / Future Improvements

1. **Multi-timeframe input** - Combine 5m, 15m, 1h data
2. **Curriculum learning** - Start simple, increase difficulty
3. **Ensemble of agents** - Train multiple seeds, ensemble predictions
4. **Real-time adaptation** - Online learning from live trades
5. **Multi-asset training** - Train single agent across BTC/ETH/SOL

## Recent Fixes (May 2025)

| Issue | Fix |
|-------|-----|
| Position sizing magic number | Changed from hardcoded $10,000 to $263.40 (your account size) |
| No short-selling support | Expanded action space from 9 to 15 actions (added SHORT_* and COVER_*) |
| Stochastic live trading | Changed `deterministic=False` to `deterministic=True` for consistency |
| Confidence calculation | Now uses softmax probabilities instead of `exp(log_prob)` |
| Buffer update on episode end | Fixed: always clear buffer, only update if enough samples |
| Missing data freshness check | Added warning if data is >7 days old |
| No early stopping | Added patience=50 eval intervals |
| No resume training | Added `--resume` flag to continue from checkpoint |
| Logging conflict | Only configure logging if not already configured |
| Missing input validation | Added checks for NaN, Inf, and insufficient data |

## Troubleshooting

**"Model not loaded" error:**
- Run training first: `python rl_trading/run_training.py`
- Check model exists: `ls data/models/rl/`

**Poor training performance:**
- Increase episodes: `--episodes 1000`
- Try different learning rate: `--learning-rate 1e-4`
- Check data quality in `data/ohlcv/`

**Out of memory:**
- Reduce batch size in `ppo_agent.py`
- Reduce sequence length in `trading_env.py`
