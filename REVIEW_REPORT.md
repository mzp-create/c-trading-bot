# Code Review Report — Hermes Crypto Trading Bot

**Version:** 1.7.0  
**Project Path:** `/mnt/hermes-data/.hermes/hermes-agent/trading-bot/`  
**Review Date:** 2026-05-19  
**Total LOC:** 7,407 across 19 Python files  
**Live Funds:** $526.80 on Bitfinex margin wallet  

---

## Executive Summary

The bot is **structurally sound** for an MVP — clear architecture (6 modules), proper separation of concerns, and 7 working layers from data collection to execution. However, there are **3 critical bugs** in position lifecycle management that would cause incorrect trading behavior, and **2 critical security issues** that expose live funds.

**Verdict: Ship with caveats.** Fix the P0 items before the bot takes its first real trade. The logic and architecture are otherwise solid.

---

## 🔴 Critical (P0 — Must Fix)

### 1. Telegram `current_regime` AttributeError in `/status`

**File:** `monitoring/telegram_alerts.py:305`  
**Bug:** `/status` command accesses `bot.regime_detector.current_regime`, but `MarketRegimeDetector` stores it as `self._last_regime` (no `current_regime` property exists).  

```python
# telegram_alerts.py:305 (broken — AttributeError)
reg = bot.regime_detector.current_regime

# regime_detector.py stores as (matches this naming)
self._last_regime: Optional[MarketRegime] = None
```

**Impact:** `/status` Telegram command always returns `⚠️ Error running /status: 'MarketRegimeDetector' object has no attribute 'current_regime'`. This means the user's status check always errors out.

**Fix:** Add a `@property` to `MarketRegimeDetector`:
```python
@property
def current_regime(self) -> Optional[MarketRegime]:
    return self._last_regime
```

---

### 2. `_cmd_balance` Calls `fetch_balance()` With Wrong Arguments

**File:** `monitoring/telegram_alerts.py:334`  
**Bug:** The `/balance` command calls `bot.collector.client.fetch_balance({"type": "margin"})`, but `BitfinexClient.fetch_balance()` (inherited from ccxt) doesn't accept positional params this way.

```python
# Broken — ccxt expects different signature
raw = bot.collector.client.fetch_balance({"type": "margin"})
```

**Impact:** `/balance` command always fails with TypeError. User sees an error response instead of their wallet balance.

**Fix:** The `BitfinexClient` already has a dedicated `fetch_margin_balance()` method (bitfinex_client.py:471-478). Use that, or fix the call signature:
```python
raw = bot.collector.client.fetch_balance({'type': 'margin'})
# OR
raw = bot.collector.client.fetch_margin_balance()
```

---

### 3. Trailing Stop Returns Raw Percentage as Stop Price

**File:** `risk/manager.py:307-318`  
**Bug:** When trailing stop is NOT yet activated, `get_trailing_stop()` returns the raw config value `stop_loss_pct` (e.g., `2.0`) instead of computing an actual price. The `update_position()` method stores this directly and compares `current_price <= sl` where `sl=2.0`.

```python
# line 307 — returns "2.0" (percentage number) as if it were a price
return float(config.get("stop_loss_pct", 2.0))
```

**Impact:** For any trade on BTC at ~$77K, a stop_loss value of `2.0` means the position would be closed **immediately** on the next price check, since $77K ≫ $2.0. Every position with trailing stop would instantly trigger.

**Fix:** Return the computed stop price:
```python
if not self._trailing_high.get(position["id"]):
    return entry_price * (1.0 - float(config.get("stop_loss_pct", 2.0)) / 100.0)
```

---

### 4. Live Credentials on Disk (World-Readable)

**File:** `/mnt/hermes-data/.hermes/hermes-agent/trading-bot/.env`  
**Issue:** The `.env` file contains:
- BITFINEX_API_KEY (live)
- BITFINEX_API_SECRET (live)
- TELEGRAM_BOT_TOKEN (live)
- DEEPSEEK_API_KEY (live)

File permissions are NOT hardened (default umask leaves them world-readable).

**Impact:** Any process on this server can read the API keys and drain the $526 margin wallet, send Telegram messages as the bot, or use the DeepSeek API key.

**Fix:**
```bash
chmod 600 /mnt/hermes-data/.hermes/hermes-agent/trading-bot/.env
```

---

### 5. Dashboard API Binds to 0.0.0.0

**File:** `dashboard/api_server.py:521`  
**Issue:** `uvicorn.run(app, host="0.0.0.0", port=8999)` binds to all network interfaces. Combined with:
- CORS `["*"]` with `allow_credentials=True` (anti-pattern)
- 12-char hex password (47 bits — weak)
- No rate limiting on login
- Password written to disk at `dashboard/.dashboard_password`

**Impact:** Anyone on the network can brute-force the dashboard password. If breached, they can see live wallet balances, trade history, and bot config (which after env var resolution contains the resolved API keys).

**Fix:** Bind to `127.0.0.1`, use `token_urlsafe(32)` for password, and delete password file after startup.

---

## 🟠 High (P1 — Fix Before Next Deploy)

### 6. SL/TP Hits Never Close Positions in Paper Mode

**File:** `main.py:608-628`  
**Bug:** `_check_positions()` detects that stop-loss or take-profit is hit and logs it, but **never removes the position** from `self.executor.open_positions`. The position lingers indefinitely.

```python
if updated.get('closed'):
    self.daily_pnl += pnl
    # ... logs and sends Telegram ...
    # BUT NEVER removes position from executor!
```

**Impact:** Paper positions accumulate forever. The bot thinks it has many open positions, blocking new trades due to max_open_positions limits, even though all "open" positions would have been stopped out.

**Fix:** Add `self.executor.close_position(pos)` or `self.executor.open_positions.remove(pos)` after detecting closure.

---

### 7. Fee Not Included in Balance Availability Check

**File:** `execution/engine.py:519-528`  
**Bug:** The buy order checks if `cost <= available_quote`, but fees are deducted separately AFTER the balance check. If `cost == available_quote`, the trade proceeds and the fee deduction makes the balance go negative.

```python
# line 521 — only checks cost
if side == "buy":
    if cost <= self._paper_balance[quote]["free"]:
        self._paper_balance[quote]["free"] -= cost
        self._paper_balance[quote]["total"] -= cost
        self._paper_balance[quote]["free"] -= fee   # <-- AFTER check
```

**Impact:** Paper balance goes negative when trading at full capital allocation. This cascades — subsequent trades see "enough" free balance because it's negative, leading to runaway position sizing.

**Fix:** Check `cost + fee <= available_quote` instead of just `cost`.

---

### 8. Correlation-Blocked Trades Reported as False BUY/SELL

**File:** `main.py:429-443`  
**Bug:** When correlation check blocks a trade (`corr_ok == False`), the code `return decision` — but `decision` still has `signal=BUY/SELL` from the pre-correlation analysis. The cycle summary and logs show a BUY/SELL signal even though no trade was placed.

**Impact:** False positive reporting. User sees "BUY" in logs/Telegram and wonders why no order was placed. With sentiment + LLM layers layered on top, it's hard to debug which layer blocked what.

**Fix:** Return a HOLD signal with the correlation reason appended:
```python
if not corr_ok:
    decision["signal"] = "HOLD"
    decision["reason"] += f" | CorrBlocked({corr_val:.2f})"
    return decision
```

---

### 9. Daily PnL Reset Can Be Missed

**File:** `main.py:634-651`  
**Bug:** `_check_daily_reset()` checks `now.hour == 0 and now.minute == 0` — exact midnight match. If the bot sleeps through midnight (e.g., 300s sleep starting at 23:57), midnight is missed entirely.

**Impact:** PnL accumulates across days without reset. Drawdown limits become meaningless after 2+ days. Daily loss limit of $50 would never trigger because PnL is a running total.

**Fix:** Use a `self._last_reset_date` comparison:
```python
today = datetime.now().date()
if self._last_reset_date is None or today > self._last_reset_date:
    self.daily_pnl = 0.0
    self._last_reset_date = today
```

---

### 10. Live `open_positions` Only Fetches Single Symbol

**File:** `execution/engine.py:741-760`  
**Bug:** `_get_live_positions()` hardcodes a single symbol lookup:
```python
symbol = self.trading_config.get("symbol", "BTC/USDT")  # ignores multi-symbol
```

**Impact:** In live mode with multi-symbol config (BTC+ETH+SOL), only BTC positions are tracked. ETH and SOL trades would be invisible to the position checker and SL/TP monitoring.

**Fix:** Fetch positions for ALL configured symbols, or use the exchange's `/positions` endpoint.

---

## 🟡 Medium (P2 — Worth Fixing)

### 11. Telegram Chat ID Not Whitelisted

**File:** `monitoring/telegram_alerts.py:170-183`  
Anyone who discovers the bot token can send commands via the Telegram API. The bot processes all commands from any chat.

**Fix:** Add a whitelist check: only process commands from `self.chat_id` (the configured home chat).

---

### 12. Telegram Polling Every 5 Seconds is Wasteful

**File:** `main.py:593-594`  
At 5-second intervals for 25+ hours = ~18,000 HTTP calls to Telegram just for polling. Should poll once per cycle before the sleep block.

**Fix:** Move polling out of the sleep loop to a single call per cycle.

---

### 13. Duplicate OHLCV Fetches Per Cycle

**File:** `main.py:396-410`  
`analyze_market()` fetches 1h OHLCV data, then `execute_trade_cycle()` fetches it AGAIN for regime detection. Same data, two API calls.

**Fix:** Pass the already-fetched DataFrame from `analyze_market` into regime detection.

---

### 14. ML Weight Hardcoded at 0.3

**File:** `main.py:232`  
`ml_weight = 0.3` regardless of model accuracy. A poorly performing model (50% accuracy) gets the same weight as a well-tuned one (63%).

**Fix:** Compute weight from training accuracy: `ml_weight = min(0.4, max(0.1, accuracy - 0.5))`.

---

### 15. `_flatten_ta()` Duplicated Across Files

`main.py:124-170` and `dry_run.py:50-78` contain identical code. Should be a shared utility function.

---

### 16. `_order_history` and `_trade_history` Grow Unbounded

**Files:** `execution/engine.py:77-78`  
These lists grow forever. Use `collections.deque(maxlen=1000)` or trim periodically.

---

### 17. No Log Rotation

**File:** `monitoring/logger.py`  
The bot log file (`logs/bot.log`) grows without rotation. After 25+ hours it's already 1.6MB. On a server running for months this could reach gigabytes.

**Fix:** Use `logging.handlers.RotatingFileHandler`.

---

### 18. Grid Strategy Recomputes Levels Every Cycle

**File:** `strategies/selector.py:443-447`  
Grid lines are recalculated every 2-minute cycle but only change when price/BB levels shift significantly. Cache the levels and recompute only on significant price change.

---

## 🟢 Low (P3 — Nice to Have)

| # | Issue | File | Detail |
|---|-------|------|--------|
| 19 | Dashboard password strength | `api_server.py:43` | `token_hex(6)` = 47 bits. Use `token_urlsafe(24)` = 192 bits |
| 20 | CORS allow-credentials + allow-origins `*` | `api_server.py:291` | Security anti-pattern |
| 21 | No HTTPS on dashboard | `api_server.py:521` | Credentials sent in cleartext |
| 22 | `allowed_updates` uses `json.dumps` | `telegram_alerts.py:157` | Should pass list directly, not JSON-encoded string |
| 23 | `dry_run.py` duplicates `_flatten_ta` | `dry_run.py:50-78` | Extract to shared utility |
| 24 | `config/default.yaml` has comments only | N/A | No schema validation |
| 25 | No unit tests | N/A | Critical for a money-handling bot |
| 26 | `setup.py` / `setup_live.py` not version-controlled | N/A | Not in git (new files?) |

---

## Architecture Assessment

```
Layer 1: Data Collection       ✅ Clean caching, retry logic
Layer 2: Technical Analysis    ✅ 20+ indicators, multi-timeframe
Layer 3: ML Prediction         ✅ Per-symbol ensemble, 59-63% accuracy
Layer 4: Strategies            ✅ 3 strategies with weighted voting
Layer 5: Sentiment Filter      ✅ RSS keyword scoring, cache
Layer 6: LLM Review            ✅ Expert system prompt, rich data
Layer 7: Risk Management       ⚠️  Trailing stop bug (P0)
Layer 8: Execution             ⚠️  Fee check bug (P1), positions not cleaned (P1)
Layer 9: Monitoring            ⚠️  Telegram command bugs (P0)
```

The architecture is **well-layered and modular**. Each layer has clear responsibilities. The main issues are in **implementation details within layers 7-9**, not in the architecture itself.

---

## Top 5 Recommended Fixes (Priority Order)

1. **Add `@property current_regime`** to MarketRegimeDetector → fixes `/status`
2. **Fix `_cmd_balance` call** → use `fetch_margin_balance()` method
3. **Fix trailing stop price calculation** → return computed price, not percentage
4. **`chmod 600 .env`** → protect live credentials
5. **Remove closed positions from executor** → prevents position pileup

---

## Metrics

| Metric | Value |
|--------|-------|
| Total Python files | 19 |
| Total LOC | 7,407 |
| Git commits | 8 (3-day dev cycle) |
| Critical bugs | 5 |
| High bugs | 5 |
| Medium bugs | 8 |
| Low/quality | 8 |
| Security issues | 2 critical, 3 high |
| Zero security issues | No eval(), exec(), pickle, or shell injection |
| Test coverage | 0% (no tests directory) |
| Logging | Comprehensive (every module has logger) |
| Error handling | Good (all external calls wrapped in try/except) |
| Documentation | Good (docstrings, changelog, skill references) |

---

*Review performed using Hermes Agent with DeepSeek-chat model. Full codebase scanned — all 7,407 lines read and analyzed across 3 parallel review tracks (security, correctness, performance).*
