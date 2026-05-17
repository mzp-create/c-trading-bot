# 🤖 Hermes Crypto Trading Bot

Automated crypto trading bot for **Bitfinex** exchange. Trades BTC, ETH, and SOL with ML-enhanced technical analysis, risk management, and Telegram alerts.

~~~

## Quick Start

```bash
cd /mnt/hermes-data/.hermes/hermes-agent/trading-bot

# Start live (auto-trades with real funds)
./start.sh live

# Start paper trading (simulation, safe)
./start.sh paper
```

> **Already running:** Bot is live at PID `34526` on the server.

~~~

## Current Status

| Item | Value |
|------|-------|
| **Mode** | **LIVE** — real $526.80 on Bitfinex |
| **Wallet** | Margin UST (USDT equivalent) |
| **Pairs** | BTC/USDT (50%), ETH/USDT (30%), SOL/USDT (20%) |
| **Cycle** | Every ~2 minutes, all 3 pairs analyzed |
| **Dashboard** | http://localhost:8999 |
| **Telegram** | @mzphs_247_tb_bot — cycle updates sent here |

### Live Prices (as of last cycle)

| Pair | Exchange Symbol | Price |
|------|----------------|-------|
| BTC/USDT | tBTCUST | ~$78,143 |
| ETH/USDT | tETHUST | ~$2,194 |
| SOL/USDT | tSOLUST | ~$87 |

~~~

## Architecture

```
trading-bot/
├── main.py                    # Bot orchestrator — trade loop, signals, execution
├── config/default.yaml        # Trading config (symbols, capital, strategies, risk)
├── start.sh                   # Launch script (paper | live)
├── CHANGELOG.md               # Version history
├── .env                       # API keys (BITFINEX_API_KEY, TELEGRAM_*)
│
├── market_data/
│   ├── bitfinex_client.py     # Bitfinex v2 REST API (HMAC-SHA384 auth)
│   └── collector.py           # OHLCV caching and fetching
│
├── analysis/
│   ├── technical.py           # TA indicators (RSI, MACD, BB, EMA, ADX)
│   └── ml_predictor.py        # ML models (per-symbol Random Forest + Gradient Boosting)
│
├── strategies/
│   └── selector.py            # Trend following, scalping, grid — weighted voting
│
├── risk/
│   └── manager.py             # Kelly sizing, stop-loss, trailing, daily drawdown limits
│
├── execution/
│   └── engine.py              # Order execution (paper sim + live Bitfinex API)
│
├── monitoring/
│   ├── logger.py              # Structured logging (bot.log, errors.log, trades.log)
│   └── telegram_alerts.py     # Telegram bot notifications for trades & cycles
│
├── dashboard/
│   ├── api_server.py          # FastAPI server (port 8999) — password protected
│   ├── run.sh                 # Dashboard launcher
│   ├── .dashboard_password    # Auto-generated random password
│   └── static/index.html      # Dark-theme single-page dashboard
│
└── data/
    ├── trades.csv             # Trade history
    ├── ohlcv/                 # Cached OHLCV data
    └── models/                # Per-symbol ML model files
```

~~~

## Trading Strategy

### Signal Generation

1. **Technical Analysis** — 3 timeframes (1h, 5m, 15m) with RSI, MACD, Bollinger Bands, EMA crossovers
2. **ML Prediction** — Ensemble model (Random Forest + Gradient Boosting) trained per symbol, predicts short-term direction
3. **Strategy Voting** — 3 strategies weighted and combined:
   - Trend Following (weight: 0.4) — EMA crossovers + RSI
   - Scalping (weight: 0.35) — MACD + Bollinger Bands on 5m
   - Grid (weight: 0.25) — Mean reversion on 15m
4. **Final Decision** — Weighted signal combination with 0.55 confidence threshold

### Risk Management

| Rule | Limit |
|------|-------|
| Max daily loss | $20 (stops trading) |
| Max drawdown | 15% (shuts down) |
| Stop loss | 2% per position |
| Take profit | 4% per position |
| Trailing stop | Activates at 2%, distance 0.5% |
| Position sizing | Kelly Criterion (dynamic) |

~~~

## Web Dashboard

Access at **http://localhost:8999**

Shows:
- Live Bitfinex wallet balances
- Per-pair analysis signals (BUY/HOLD/SELL)
- Bot status and uptime
- Trade history table
- Performance metrics
- Configuration summary

**Password:** Auto-generated at startup, saved to `dashboard/.dashboard_password`

~~~

## Telegram Alerts

**Bot:** @mzphs_247_tb_bot

Automatically sends:
- 🟢 Trade executions (BUY/SELL with P&L)
- 🔄 Cycle summaries every ~2min (per-pair signal breakdown)
- 📊 Daily summaries
- 🛑 Shutdown notifications
- 🚨 Error alerts

~~~

## Configuration

Edit `config/default.yaml`:

```yaml
trading:
  symbols:
    - name: "BTC/USDT"      # Display name
      symbol: "tBTCUST"     # Bitfinex exchange symbol
      allocation_pct: 50.0  # % of total capital
      enabled: true
  initial_capital: 526.8    # Total USDT across all pairs
  daily_target: 100.0       # $/day target
  max_risk_per_trade: 0.02  # 2% per trade
```

~~~

## ML Models

Each symbol gets its own trained model saved to `data/models/`:

| Symbol | Model File | Status |
|--------|-----------|--------|
| BTC/USDT | `BTC_USDT_ml_model.joblib` | ✅ Trained (63.2% accuracy) |
| ETH/USDT | `ETH_USDT_ml_model.joblib` | ⏳ Training (needs more data) |
| SOL/USDT | `SOL_USDT_ml_model.joblib` | ⏳ Training (needs more data) |

Models retrain automatically every 24 hours with fresh data.

~~~

## Troubleshooting

**Bot not trading?** Check risk limits (`max_daily_loss: $20`), signal confidence threshold (0.55), and bot.log for reasons.

**Telegram not sending?** Verify `.env` has `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. The user must start a chat with the bot first.

**Dashboard won't load?** Check it's running: `ps aux | grep uvicorn`. Start with `cd dashboard && bash run.sh`.

**Bitfinex API errors?** Ensure the API key has **Trade** permission enabled on Bitfinex.

~~~

*Built with Hermes Agent — auto-trading since May 16, 2026*
