# Getting Started

A practical guide to setting up and running the trading bot. For architecture
and strategy details see `README.md` and `CHANGELOG.md`.

> ⚠️ **This bot can place real orders on a live Bitfinex margin account.**
> Start in `paper` mode. Only switch to `live` once you understand the risk
> parameters in your config.

---

## 1. Prerequisites

- Python 3.11+
- A Bitfinex account with API keys (only needed for `live` mode)
- (Optional) A Telegram bot token + chat ID for alerts and remote commands

---

## 2. Install

The repo ships with a virtualenv at `.venv`. If it's present, just activate it:

```bash
cd /mnt/hermes-data/.hermes/hermes-agent/trading-bot
source .venv/bin/activate
```

To create it fresh:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Key dependencies: `bitfinex-api-py==4.0.0`, `ccxt==4.5.54`, `fastapi`,
`uvicorn`, `pandas`, `numpy`, `scikit-learn`, `xgboost`, `PyYAML`, `pytest`.

---

## 3. Configure credentials

Secrets are supplied via environment variables (never commit them). Config
files reference them as `${VAR_NAME}` placeholders, resolved at startup.

Create a `.env` file in the repo root (it is git-ignored):

```bash
# Bitfinex (required for live mode only)
export BITFINEX_API_KEY="your_key_here"
export BITFINEX_API_SECRET="your_secret_here"

# Telegram (optional — enables alerts + remote commands)
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="your_numeric_chat_id"

# DeepSeek LLM review layer (optional)
export DEEPSEEK_API_KEY="your_key_here"
```

**Running two instances at once?** Long and short bots each need their *own*
Bitfinex key pair — a shared key produces `nonce: small` errors because nonces
must strictly increase per key:

```bash
export BITFINEX_LONG_API_KEY="..."
export BITFINEX_LONG_API_SECRET="..."
export BITFINEX_SHORT_API_KEY="..."
export BITFINEX_SHORT_API_SECRET="..."
```

> 🔒 Treat these keys like passwords. If a key is ever exposed (pasted into a
> chat, committed, logged), **rotate it immediately** in the Bitfinex UI.

---

## 4. Run the bot

The entry point is `main.py`:

```bash
python main.py [--mode {paper|live|backtest|monitor}] \
               [--config config/default.yaml] \
               [--instance {long|short|default}] \
               [--symbol SYMBOL] [--capital AMOUNT]
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--mode` | `paper` | `paper` = simulated · `live` = real orders · `backtest` = historical · `monitor` = dashboard only |
| `--config` | `config/default.yaml` | Path to the YAML config |
| `--instance` | `default` | `long` = BUY only · `short` = SELL only · `default` = both |
| `--symbol` | from config | Override the trading symbol |
| `--capital` | from config | Override `initial_capital` |

### Quickest start (safe, simulated)

```bash
./start.sh paper
```

### Go live (real money — prompts for confirmation)

```bash
./start.sh live
```

`start.sh` activates `.venv`, loads `.env` in live mode, and requires explicit
confirmation before placing real orders.

### Dual instance (long + short together)

```bash
./start_dual.sh --paper    # or --live
```

This launches two background processes using `config/long.yaml` and
`config/short.yaml`, with separate state under `instances/long/` and
`instances/short/`. Use `./status_dual.sh` and `./stop_dual.sh` to manage them.

---

## 5. Configuration overview

Configs live in `config/`. The default is `config/default.yaml`; `long.yaml`
and `short.yaml` are tuned per direction.

Top-level sections in `config/default.yaml`:

| Section | Controls |
|---------|----------|
| `trading` | Symbols, initial capital, daily target, max open positions |
| `exchange` | Bitfinex settings, API key/secret refs, WebSocket config |
| `strategies` | Enabled strategies and weights (ensemble, trend, grid) |
| `ml` | Model type, features, retrain interval |
| `regime_detector` | Market-regime detection (TRENDING / RANGING / VOLATILE) |
| `sentiment` | News/RSS/Reddit sentiment filtering |
| `risk` | Leverage, stop-loss, take-profit, daily-loss & drawdown limits |
| `monitoring` | Log level, Telegram alert toggles |
| `dashboard` | `public_url` used to build `/dashboard` login links |
| `data` | Output dirs for OHLCV, models, trades, logs, the SQLite DB |

Review the `risk` section before going live — it governs leverage, stop-loss,
take-profit, the daily loss limit, and max drawdown.

---

## 6. Web dashboard

Start the dashboard (read-only view of balances, positions, PnL, logs):

```bash
cd dashboard && ./run.sh
# equivalent to:
uvicorn api_server:app --host 0.0.0.0 --port 8999 --log-level info
```

Then open **http://localhost:8999**.

On startup it prints a randomly generated password and saves it to
`dashboard/.dashboard_password`. Authentication accepts either:

- **HTTP Basic** — leave the username blank, paste the generated password, or
- **A one-time login link** from Telegram (see below) that sets an 8-hour
  session cookie.

The session cookie is marked `Secure` automatically when the dashboard is
served over HTTPS (it honors `X-Forwarded-Proto` behind a proxy), so the token
is never sent in cleartext on a TLS deployment.

---

## 7. Telegram commands

If `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set, the bot polls for
commands (only from your whitelisted chat):

| Command | Action |
|---------|--------|
| `/status` | Mode, uptime, PnL, open positions |
| `/balance` | Wallet breakdown per symbol |
| `/portfolio` | Holdings with valuations |
| `/positions` | Open positions detail |
| `/pause` · `/resume` | Pause / resume trading |
| `/close` | Close positions |
| `/config` | Key config values |
| `/sentiment` | Latest news sentiment + market regime |
| `/retrain` | Force an ML retrain |
| `/dashboard` | Get a one-time login link to the web dashboard (valid 2 min, single use) |
| `/help` | List all commands |

The `/dashboard` link embeds a short-lived, single-use HMAC token derived from
the dashboard password — the raw password is never sent over Telegram. Set
`dashboard.public_url` in your config so the link points at the right host
(defaults to `http://localhost:8999`).

---

## 8. Run the tests

```bash
source .venv/bin/activate
python -m pytest tests/ -q              # full suite
python -m pytest tests/ -q -k dashboard # filter by keyword
python -m pytest tests/test_persistence.py -v
```

Tests live in `tests/`.

---

## 9. Typical first session

```bash
# 1. Activate the environment
source .venv/bin/activate

# 2. Sanity-check the suite
python -m pytest tests/ -q

# 3. Run in paper mode against the default config
./start.sh paper

# 4. (Optional) In another terminal, start the dashboard
cd dashboard && ./run.sh   # http://localhost:8999

# 5. Watch logs
tail -f logs/bot.log
```

When you're confident in the configuration and risk limits, switch to
`./start.sh live`.
