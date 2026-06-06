# Quickstart Checklist — Start Trading Now

## Pre-Flight (Do This First)

- [ ] API keys configured in `.env` (this only prints the var NAMES, never the secrets)
  ```bash
  grep -oE '^(export +)?BITFINEX_[A-Z_]+' .env | sed -E 's/export +//'
  ```

- [ ] Virtualenv activated
  ```bash
  source .venv/bin/activate
  ```

- [ ] Tests pass
  ```bash
  python -m pytest tests/ -q
  # Expect: 143 passed, 2 failed (nonce tests — OK)
  ```

---

## Option 1: Single Bot (Paper Mode)

```bash
./start.sh paper
```

**What happens:**
- Simulated trading with fake money
- Real market data from Bitfinex
- Logs to `logs/bot.log`

**Stop:** Ctrl+C

---

## Option 2: Dual Instance (Paper Mode) ⭐ Recommended

```bash
./start_dual.sh --paper
```

**What happens:**
- 🟢 **Long bot**: Only BUY signals, $263.40 capital
- 🔴 **Short bot**: Only SELL signals, $263.40 capital
- Separate DBs: `instances/long/data/`, `instances/short/data/`
- Separate logs: `instances/long/logs/`, `instances/short/logs/`

**Monitor:**
```bash
./status_dual.sh               # Check if running
tail -f instances/long/logs/bot.log
tail -f instances/short/logs/bot.log
```

**Stop:**
```bash
./stop_dual.sh
```

---

## Option 3: Go Live (Real Money)

### Step 1: Verify credentials
```bash
echo "Key loaded: ${BITFINEX_API_KEY:0:8}..."
```

### Step 2: Single bot live
```bash
./start.sh live
# Type 'LIVE' when prompted
```

### Step 3: Dual instance live
```bash
./start_dual.sh --live
```

**Risk limits enforced:**
- Max daily loss: $8 per instance ($16 total)
- Max drawdown: 10% ($52 total)
- Stop loss: 2% per position
- Min position: $50

---

## Dashboard Access

```bash
cd dashboard && ./run.sh
```

Open: http://localhost:8999

Password: `cat dashboard/.dashboard_password`

Or get login link via Telegram: `/dashboard`

---

## Telegram Commands

Send to your bot:
```
/status      - Bot status + PnL
/balance     - Wallet breakdown
/positions   - Open positions
/pause       - Pause trading
/resume      - Resume trading
/close       - Close all positions
/dashboard   - Get login link
```

---

## Validation Checklist

Within 30 minutes of starting:

- [ ] Bot logs show "Hermes Trading Bot initialized"
- [ ] No errors in log
- [ ] Telegram shows startup message
- [ ] Dashboard loads
- [ ] (Paper) Simulated trades appear in logs
- [ ] (Live) Real positions appear in `/positions`

---

## Emergency Stop

```bash
# Stop everything
pkill -f "main.py"

# Or use the script
./stop_dual.sh
```

---

## Daily Routine

```bash
# Morning
./status_dual.sh

# Throughout day
# Check Telegram alerts

# Evening
tail -20 instances/long/logs/bot.log
tail -20 instances/short/logs/bot.log
```

---

**Ready? Start with:** `./start_dual.sh --paper`
