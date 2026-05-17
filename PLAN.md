# Crypto Trading Bot — Bitfinex Integration

## Strategy Overview
- **Objective**: $100/day profit from $526.80 USDT initial capital
- **Exchange**: Bitfinex (margin wallet)
- **Assets**: BTC/USDT, ETH/USDT, SOL/USDT
- **Approach**: Multi-strategy with ML-enhanced market analysis + adaptive risk

## Architecture

```
┌─────────────────────────────────────────────┐
│           Trading Bot Core                   │
├─────────────────────────────────────────────┤
│  market_data/   → Fetch OHLCV + orderbook    │
│  analysis/      → Technical + ML analysis    │
│  strategies/    → Trading strategies          │
│  risk/          → Risk management + Regime    │
│  execution/     → Order placement             │
│  monitoring/    → Performance + alerts        │
│  main.py        → Orchestrator loop           │
└─────────────────────────────────────────────┘
```

## Phase 1: Foundation ✅ (Done)
1. Bitfinex API client (REST + WebSocket)
2. Market data collector (OHLCV)
3. Technical analysis engine (indicators)
4. 3 strategies: TrendFollowing, Scalping, Grid
5. Paper + Live trading modes
6. Risk manager (stop-loss, trailing, position sizing)
7. Telegram alerts via @mzphs_247_tb_bot
8. Web dashboard on port 8999

## Phase 2: Market Regime Detector (Current)
9. **MarketRegimeDetector** — classify market as TRENDING / RANGING / VOLATILE
10. **Regime-adaptive params** — dynamic SL/TP/position size per regime
11. **Correlation-aware position limits** — prevent over-concentration
12. **Ensemble vote display** — clearer logging of strategy disagreement

### Implementation Plan (Regime Detector)

```
New file: risk/regime_detector.py
  └── MarketRegimeDetector class
       ├── detect_regime(data, lookback=50) -> str
       │    Uses: volatility ratio, ADX, trend strength
       │    Returns: 'TRENDING' | 'RANGING' | 'VOLATILE'
       ├── get_adapted_params(regime, base_sl, base_tp) -> dict
       │    Returns regime-adjusted SL, TP, position_size_mult
       └── check_correlation(new_asset, open_positions) -> (bool, float)

Modified: risk/manager.py
  └── RiskManager.calculate_position_size()
       └── Apply regime multiplier from detector

Modified: main.py
  └── TradingBot.execute_trade_cycle()
       └── Call regime detector before position sizing

Modified: config/default.yaml
  └── Add [regime_detector] section
```

## Phase 3: Live Trading
- Real order execution ✅
- Risk management ✅
- Telegram alerts ✅
- Gradual capital scaling

## Key Design Decisions
- Use **ccxt** for Bitfinex REST API (battle-tested)
- Bitfinex margin wallet for UST/USDT funds
- Log everything for post-mortem analysis
- Telegram alerts for every trade and error
