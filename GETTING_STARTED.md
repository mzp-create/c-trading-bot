# Getting Started

Quick-start guide for the Hermes Trading Bot. For architecture details, see `README.md` and `CHANGELOG.md`.

> ⚠️ **WARNING: This bot places real orders on Bitfinex margin accounts.**
> Start in `paper` mode. Only switch to `live` once you understand the risk parameters.

---

## 1. Prerequisites

- Python 3.11+
- Bitfinex account with API keys (for `live` mode)
- Telegram bot token + chat ID (optional, for alerts and commands)

---

## 2. Installation

The repo includes a virtualenv at `.venv`:

```bash
cd /mnt/hermes-data/.hermes/hermes-agent/trading-bot
source .venv/bin/activate
```

To recreate:
```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Key deps: `bitfinex-api-py==4.0.0`, `ccxt==4.5.54`, `fastapi`, `uvicorn`, `pandas`, `xgboost`, `PyYAML`

---

## 3. Configure Credentials

Secrets via environment variables (never commit). Config files use `${VAR}` placeholders.

Create `.env` in repo root (git-ignored):

```bash
# Bitfinex (required for live)
export BITFINEX_API_KEY="your_key_here"
export BITFINEX_API_SECRET="your_secret_here"

# Telegram (optional — enables alerts + commands)
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="your_numeric_chat_id"

# DeepSeek LLM review layer (optional)
export DEEPSEEK_API_KEY="your_key_here"
```

**Dual-instance trading?** Long + short bots need **separate API keys** — shared keys cause `nonce: small` errors:

```bash
export BITFINEX_LONG_API_KEY="..."
export BITFINEX_LONG_API_SECRET="..."
export BITFINEX_SHORT_API_KEY="..."
export BITFINEX_SHORT_API_SECRET="..."
```

> 🔒 Rotate keys immediately if exposed anywhere.

---

## 4. Start Trading

### Quick Start (Paper Mode — Safe)

```bash
./start.sh paper
```

### Live Trading (Real Money)

```bash
./start.sh live
```

Requires typing `LIVE` to confirm.

### Dual Instance (Long + Short Together)

```bash
./start_dual.sh --paper    # or --live
```

Launches two processes:
- **Long bot**: BUY signals only (`config/long.yaml`, `instances/long/`)
- **Short bot**: SELL signals only (`config/short.yaml`, `instances/short/`)

Management:
```bash
./status_dual.sh    # Check both instances
./stop_dual.sh      # Stop both
```

---

## 5. Configuration

Configs in `config/`:

| File | Purpose |
|------|---------|
| `default.yaml` | Single bot (both directions) |
| `long.yaml` | Long-only instance |
| `short.yaml` | Short-only instance |

Key sections:

| Section | Controls |
|---------|----------|
| `trading` | Symbols, capital, daily target, max positions |
| `exchange` | Bitfinex settings, API keys, WebSocket config |
| `strategies` | Enabled strategies (ensemble, trend, grid) |
| `ml` | XGBoost model, features, retrain interval |
| `regime_detector` | Market regime (TRENDING/RANGING/VOLATILE) |
| `sentiment` | News/RSS filtering |
| `risk` | Leverage, SL/TP, daily loss, drawdown limits |
| `monitoring` | Logging, Telegram alerts |
| `dashboard` | Public URL for `/dashboard` links |
| `data` | SQLite DB, models, logs paths |

**Review the `risk` section before live trading.**

---

## 6. Architecture Overview

### Major Recent Changes (Phase 1-4 Refactor)

| Component | What Changed |
|-----------|--------------|
| **Execution Engine** | Mode-agnostic: paper/live share same code path via `BitfinexClient` |
| **WebSocket Feed** | Real-time ticker + order updates, REST fallback on disconnect |
| **RiskState** | In-memory SL/TP/trailing metadata (replaces position cache) |
| **Persistence** | SQLite single source of truth (orders, fills, trades, equity, signals) |
| **Dashboard** | HMAC login tokens, cookie/Basic auth, read-only API |
| **Direction Filter** | Enforced in executor (not strategy layer) using live position data |
| **Position Tracking** | Union of exchange positions + RiskState safety net for margin shorts |

### Data Flow

```
Market Data (WS/REST)
       ↓
Technical Analysis (1h/5m/15m)
       ↓
ML Prediction + Strategy Signals
       ↓
Signal Combination + Sentiment Filter
       ↓
LLM Review Layer (optional)
       ↓
Risk Check + Position Sizing
       ↓
Direction Validation (dual-instance)
       ↓
Order Execution → SQLite + Telegram
       ↓
Position Monitor (SL/TP/Trailing)
```

---

## 7. Web Dashboard

Start:
```bash
cd dashboard && ./run.sh
# Or: uvicorn api_server:app --host 0.0.0.0 --port 8999
```

Open **http://localhost:8999**

Auth methods:
- **HTTP Basic**: Username blank, password from `.dashboard_password`
- **Telegram `/dashboard`**: One-time HMAC link (2 min expiry)

The session cookie is `Secure` when served over HTTPS.

---

## 8. Telegram Commands

If `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set:

| Command | Action |
|---------|--------|
| `/status` | Mode, uptime, PnL, positions |
| `/balance` | Wallet breakdown |
| `/portfolio` | Holdings + valuations |
| `/positions` | Open positions detail |
| `/pause` / `/resume` | Pause/resume trading |
| `/close` | Close all positions |
| `/config` | Key config values |
| `/sentiment` | Latest sentiment + regime |
| `/retrain` | Force ML retrain |
| `/dashboard` | One-time login link (2 min) |
| `/help` | Command list |

---

## 9. Running Tests

```bash
source .venv/bin/activate
python -m pytest tests/ -q              # Full suite
python -m pytest tests/ -q -k engine    # Filter by keyword
python -m pytest tests/test_persistence.py -v
```

Most tests pass; a couple of nonce-atomic concurrency tests can fail on some
systems (non-critical edge cases). Run the suite to see the current state rather
than relying on a fixed count.

---

## 10. First Session Checklist

```bash
# 1. Activate environment
source .venv/bin/activate

# 2. Run tests
python -m pytest tests/ -q

# 3. Check credentials loaded
echo $BITFINEX_API_KEY | head -c 8

# 4. Start in paper mode
./start.sh paper

# 5. (Another terminal) Start dashboard
cd dashboard && ./run.sh

# 6. Watch logs
tail -f logs/bot.log
```

Once confident: `./start.sh live` or `./start_dual.sh --live`

---

## 11. Operational Scripts

| Script | Purpose |
|--------|---------|
| `scripts/check_live_positions.py` | Read-only live position check |
| `scripts/reconcile_readonly.py` | Verify DB vs exchange state |
| `scripts/reconcile_deep.py` | Full reconciliation with fixes |
| `scripts/daily_report.py` | Generate daily PnL report |
| `scripts/live_close_smoke_test.py` | Test close path in live mode |
| `scripts/import_trades_csv.py` | Import historical trades |

---

## 12. Troubleshooting

### `nonce: small` errors
- Each instance needs **separate API keys**
- Check `data/.bfx_nonce` exists and is writable

### Position not showing
- Margin shorts may not appear in `fetch_positions()`
- `trust_exchange_positions: false` enables RiskState union safety net

### Dashboard login fails
- Check `dashboard.public_url` in config
- Verify `.dashboard_password` exists

### Tests fail
- A couple of nonce-atomic tests may fail on some systems — non-critical
- The rest of the suite should pass

---

## File Reference

| Path | Purpose |
|------|---------|
| `main.py` | Entry point, `TradingBot` orchestrator |
| `execution/engine.py` | Order execution, position management |
| `bitfinex/` | Exchange client, WebSocket feed |
| `state/risk_state.py` | SL/TP/trailing metadata |
| `persistence/` | SQLite schema, repository, models |
| `strategies/selector.py` | Strategy ensemble |
| `analysis/` | TA, ML predictor, sentiment, LLM reviewer |
| `risk/manager.py` | Position sizing, risk checks |
| `monitoring/` | Logging, Telegram alerts |
| `dashboard/` | FastAPI dashboard server |
