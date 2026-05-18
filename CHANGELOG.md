# Changelog — Hermes Crypto Trading Bot

All notable changes to the trading bot project.

---

## [1.6.0] — 2026-05-18

### Added
- **ETH/USDT & SOL/USDT ML Models** — XGBoost ensemble models trained on 1,000 candles each (~42 days of 1h data)
  - ETH: 59.3% accuracy (270 samples, 132 up / 138 down)
  - SOL: 61.5% accuracy (479 samples, 258 up / 221 down)
  - Both saved to `data/models/{symbol}_ml_model.joblib`
- **Sentiment Analysis Layer** (`analysis/sentiment.py`, 807 lines) — Layer 2 signal filter
  - Live RSS feed fetchers for CoinTelegraph + CoinDesk (free, no API key required)
  - 200+ bullish/bearish crypto keyword lexicon with weighted scoring
  - 5-min result caching to reduce requests
  - `get_signal_filter()` method: confirms BUY on positive news, blocks BUY on negative news
  - Configurable filter strength (default: 0.30)
- **Integration Guide** — `analysis/sentiment_integration.md` documenting upgrade paths:
  - NewsAPI.org (100 free/day)
  - Reddit OAuth (no API key needed)
  - LLM scoring via DeepSeek ($0.0001/batch)
- **Research Document** — `analysis/sentiment_research.md` with full evaluation of all approaches

### Changed
- `main.py` — Sentiment init in `TradingBot.__init__`, `_apply_sentiment_filter()` in `_combine_signals()`
- `config/default.yaml` — New `sentiment:` section: `enabled`, `filter_strength`, `sources`
- `monitoring/telegram_alerts.py` — Cycle summary alerts muted; only trade/error/daily alerts sent

---

## [1.5.0] — 2026-05-17

### Added
- **Market Regime Detector** (`risk/regime_detector.py`, 355 lines) — Identifies market state:
  - Volatility ratio (recent vs historical std dev)
  - Linear regression trend strength (R² + normalized slope)
  - Trend consistency scoring (directional bar count)
  - Returns: `TRENDING | RANGING | VOLATILE`
- **Regime-Adaptive Risk Parameters** — Automatically adjusts SL/TP/sizing per regime:
  - 📈 TRENDING: 1.2× SL, 1.5× TP, 1.0× position size
  - 📊 RANGING: 0.8× SL, 0.7× TP, 0.8× position size
  - 🌪️ VOLATILE: 1.5× SL, 1.0× TP, 0.5× position size
- **Correlation-Aware Position Limits** — Prevents over-concentration on correlated assets (>0.7)
- **Regime Emoji in Trade Alerts** — 📈/📊/🌪️ in Telegram trade notifications

### Changed
- `risk/manager.py` — `calculate_position_size()` now accepts `regime_position_mult`
- `main.py` — Regime detection runs before each position sizing; correlation checked before new positions
- `config/default.yaml` — New `regime_detector:` section with lookback, volatility thresholds, trend params

### Fixed
- **TA Flattening** — `_flatten_ta()` normalizes nested TA dicts to flat keys (`ema_9`, `rsi`, `bb_upper`, etc.)
- **Regime Detector** — Handles lowercase column names from data collector

---

## [1.4.0] — 2026-05-17

### Added
- **Multi-Pair Trading** — Bot now trades 3 pairs simultaneously:
  - BTC/USDT (50% capital allocation) — `tBTCUST`
  - ETH/USDT (30% capital allocation) — `tETHUST`
  - SOL/USDT (20% capital allocation) — `tSOLUST`
- **Per-Symbol ML Models** — `MLPredictor` refactored to support separate models per symbol (BTC, ETH, SOL each train independently)
- **Live Cycle Telegram Alerts** — `send_cycle_summary()` posts a compact update every ~2min showing all 3 pairs' signals + PnL
- **Per-Pair Capital Allocation** — Capital split by allocation_pct across enabled symbols
- **Backward Compatible** — Single-symbol config (`symbol` field) still works as fallback

### Changed
- `config/default.yaml` — `trading.symbols` is now a list (was single `trading.symbol`)
- `main.py` — Trade loop iterates over all enabled symbols each cycle
- `execution/engine.py` — Paper balance init distributes capital across all symbols
- `analysis/ml_predictor.py` — Models stored per-symbol (`BTC_USDT_ml_model.joblib`, `ETH_USDT_ml_model.joblib`, `SOL_USDT_ml_model.joblib`)

### Fixed
- `execute_trade_cycle()` now returns analysis result for cycle summary aggregation
- Telegram `send_cycle_summary()` added with per-pair signal breakdown

### Added
- **Web Dashboard** (`dashboard/`) — Full-featured single-page dashboard with:
  - Live Bitfinex wallet balances via authenticated API
  - Bot status monitoring (mode, uptime, last analysis signal)
  - Trade history table from `trades.csv`
  - Performance metrics (PnL, win rate, best/worst trade)
  - Configuration overview (symbol, capital, strategies, risk settings)
  - Dark crypto-themed UI with 30s auto-refresh
- **Dashboard Password Protection** — Random 12-char password generated at each startup, saved to `.dashboard_password`
- **API Endpoints** — FastAPI server on port 8999 serving REST + HTML:
  - `GET /` — Dashboard HTML
  - `GET /api/status` — Bot status, mode, uptime, last analysis
  - `GET /api/balance` — Live wallet balances from Bitfinex
  - `GET /api/trades` — Trade history (most recent first)
  - `GET /api/summary` — Summary statistics
  - `GET /api/config` — Bot configuration
  - `GET /health` — Health check (no auth required)
- `run.sh` — Dashboard launcher script

### Changed
- `.env` — Added `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for trade alerts

### Fixed
- Telegram notifications now enabled (was "not configured" in earlier runs)

---

## [1.2.0] — 2026-05-16

### Added
- **Live Bitfinex Trading** — Switched from paper to live mode via Bitfinex API
- **Margin Wallet Support** — Bot configured to use margin wallet (`default_type: "margin"`) where $526.80 UST was found
- **UST → USDT Alias** — Balance parser now aliases Bitfinex's "UST" (margin stablecoin) as USDT
- **Trade Permission Verification** — Confirmed API key works with `POST /v2/auth/w/order/submit`
- `start_live.sh` — Script to start the bot in live mode

### Changed
- Config `testnet: false`, `default_type: "margin"`, `initial_capital: 526.8`
- Config env var resolver — Replaced regex interpolation with simpler `.replace()` approach

### Fixed
- Config env var resolution was stripping `.` and other chars from values

---

## [1.1.0] — 2026-05-16

### Added
- **Telegram Alert Integration** — `monitoring/telegram_alerts.py` with configurable token and chat ID
- **Bitfinex API Fix** — Added `since` parameter to `fetch_ohlcv()` for timeframes >= 1h to get recent data
- **ML Model Training** — Ensemble model (Random Forest + Gradient Boosting) trained on 720 1h candles, achieving 63.2% accuracy
- **3 Trading Strategies** — Trend following (EMA/RSI), scalping (MACD/Bollinger), grid trading
- **Risk Management** — Kelly sizing, stop-loss/take-profit, trailing stop, daily loss limit, drawdown protection
- **Paper Trading** — Full simulation with slippage and fees

### Changed
- ML profit threshold: 1.5% → 0.8%
- ML forward periods: 6 → 3
- Min training samples: 500 → 100 (later 50)

### Fixed
- Bitfinex 1h data returning 2019 candles (missing `since` parameter)

---

## [1.0.0] — 2026-05-16

### Added
- Initial project scaffold with 11 modules
- Bitfinex client with CCXT integration
- Market data collector with caching
- Technical analysis (RSI, MACD, Bollinger Bands, EMA, ADX, volume)
- ML predictor (scikit-learn ensemble)
- Strategy selector with weight-based voting
- Execution engine (paper mode)
- Logging and monitoring system
- Config file with YAML + env var resolution
- Setup script for one-shot initialization
