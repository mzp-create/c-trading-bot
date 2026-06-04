# Dual-Instance Trading Bot Plan
## Long + Short Instances with 50/50 Capital Split

---

## Executive Summary

Split the trading bot into **two independent instances**:
| Instance | Direction | Capital | Target | Config |
|----------|-----------|---------|--------|--------|
| Bot A (Long) | LONG only | 50% ($263.40) | $25/day | `config/long.yaml` |
| Bot B (Short) | SHORT only | 50% ($263.40) | $25/day | `config/short.yaml` |

Both run simultaneously, same pairs (BTC/ETH/SOL), same exchange account.

---

## Phase 1: Architecture Changes

### 1.1 New Directory Structure
```
trading-bot/
├── config/
│   ├── default.yaml      # Original (kept for compatibility)
│   ├── long.yaml         # Long-only instance config
│   └── short.yaml        # Short-only instance config
├── instances/            # NEW: Runtime data separation
│   ├── long/             # Long bot state
│   │   ├── data/
│   │   ├── models/
│   │   ├── logs/
│   │   └── .nonce        # Instance-specific nonce file
│   └── short/            # Short bot state
│       ├── data/
│       ├── models/
│       ├── logs/
│       └── .nonce        # Instance-specific nonce file
├── main.py               # Add --instance flag
├── execution/
│   └── engine.py         # Support position direction filtering
└── market_data/
    └── bitfinex_client.py # Fix nonce race condition
```

### 1.2 Instance-Aware Main Entry
```python
# main.py -- new argument
parser.add_argument('--instance', choices=['long', 'short'], default='long',
                   help='Trading instance type (determines direction bias)')
parser.add_argument('--nonce-file', help='Path to nonce file (for multi-instance)')
```

---

## Phase 2: Signal System Modification

### 2.1 Direction Filter in Strategy Selector
Add `trade_direction` parameter to `StrategySelector`:

```python
class StrategySelector:
    def __init__(self, config: dict, trade_direction: str = "both"):
        """
        trade_direction: "long" | "short" | "both"
        - "long": Only generate BUY signals, SELL becomes HOLD
        - "short": Only generate SELL signals, BUY becomes HOLD
        - "both": Normal operation (original behavior)
        """
        self.trade_direction = trade_direction
    
    def _filter_by_direction(self, signal: dict) -> dict:
        """Filter signal based on instance direction."""
        if self.trade_direction == "both":
            return signal
        
        sig_type = signal.get("signal", "HOLD")
        
        if self.trade_direction == "long" and sig_type == "SELL":
            return {**signal, "signal": "HOLD", 
                   "reason": signal.get("reason", "") + " | BLOCKED:long_only"}
        
        if self.trade_direction == "short" and sig_type == "BUY":
            return {**signal, "signal": "HOLD",
                   "reason": signal.get("reason", "") + " | BLOCKED:short_only"}
        
        return signal
```

### 2.2 Modified Signal Flow
```
Raw TA Signals → Strategy.generate() → Direction Filter → Confidence Check → Execution
                                    ↓
                              (blocks opposite direction)
```

---

## Phase 3: Bitfinex Nonce Fix (CRITICAL)

### 3.1 Current Issue
- Single `.bfx_nonce` file in `data/ohlcv/` directory
- Race condition when both instances write simultaneously
- File lock may fail under load

### 3.2 Solution: Instance-Isolated Nonces + Atomic Operations
```python
class BitfinexClient:
    def __init__(self, config: dict, mode: str = "paper", nonce_file: str = None):
        # ... existing init ...
        
        # Instance-specific nonce file
        if nonce_file:
            self._nonce_path = nonce_file
        else:
            # Default with instance isolation
            instance = config.get("instance", "default")
            self._nonce_path = str(
                Path(config.get("data", {}).get("ohlcv_dir", "data/ohlcv")).parent 
                / f".bfx_nonce_{instance}"
            )
    
    def _v2_post(self, path: str, body: dict) -> list:
        """
        Fixed nonce generation with:
        1. Instance-isolated nonce files (no collision between long/short bots)
        2. Redis-style atomic increment via temp file + rename
        3. Microsecond timestamp fallback with strict monotonic increase
        4. Automatic recovery from corrupt nonce files
        """
        import os
        import tempfile
        import fcntl
        
        nonce_dir = Path(self._nonce_path).parent
        nonce_dir.mkdir(parents=True, exist_ok=True)
        
        # Use temp file in same filesystem for atomic rename
        fd = os.open(self._nonce_path, os.O_RDWR | os.O_CREAT, 0o600)
        
        try:
            # Exclusive lock (blocks other processes/threads)
            fcntl.flock(fd, fcntl.LOCK_EX)
            
            # Read current nonce
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                raw = os.read(fd, 64).decode().strip()
                last_nonce = int(raw) if raw else None
            except (ValueError, IOError):
                last_nonce = None
            
            # Generate new nonce with multiple safeguards
            # Method 1: Microsecond timestamp (Bitfinex compatible)
            ts_nonce = int(time.time() * 1_000_000)
            
            # Method 2: Counter-based if timestamp fails
            if last_nonce is None:
                # Seed with large timestamp value ending in zeros
                new_nonce = ((ts_nonce + 999_999) // 1_000_000) * 1_000_000
            else:
                # Strictly increasing: max(timestamp, last+1M)
                min_next = last_nonce + 1_000_000
                new_nonce = max(ts_nonce, min_next)
                # Ensure ends in zeros (Bitfinex requirement)
                new_nonce = ((new_nonce + 999_999) // 1_000_000) * 1_000_000
            
            # Atomic write: write to temp, fsync, then rename over original
            temp_path = f"{self._nonce_path}.tmp.{os.getpid()}.{threading.current_thread().ident}"
            with open(temp_path, 'w') as f:
                f.write(str(new_nonce))
                f.flush()
                os.fsync(f.fileno())
            
            os.rename(temp_path, self._nonce_path)
            
            nonce = str(new_nonce)
            
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        
        # Continue with signature generation...
```

### 3.3 Nonce File Layout
```
data/
├── ohlcv/
├── .bfx_nonce_long      # Long bot nonce
├── .bfx_nonce_short     # Short bot nonce
└── .bfx_nonce           # Legacy (for backward compat)
```

---

## Phase 4: Execution Engine Updates

### 4.1 Position Direction Tracking
```python
class ExecutionEngine:
    def __init__(self, config: dict, mode: str = "paper", trade_direction: str = "both"):
        self.trade_direction = trade_direction  # "long" | "short" | "both"
        
    def execute_order(self, symbol: str, side: str, amount: float, ...):
        """Execute with direction validation."""
        side = side.lower()
        
        # Direction check
        if self.trade_direction == "long" and side == "sell":
            # Allow sell only if closing existing long
            existing = self._get_position(symbol)
            if not existing or existing.get("side") != "buy":
                return {"success": False, "error": "SHORT selling not allowed in long-only mode"}
        
        if self.trade_direction == "short" and side == "buy":
            # Allow buy only if closing existing short
            existing = self._get_position(symbol)
            if not existing or existing.get("side") != "sell":
                return {"success": False, "error": "LONG buying not allowed in short-only mode"}
        
        # Continue with execution...
```

### 4.2 Margin Trading Short Support
Bitfinex margin supports short natively via negative amounts:
```python
def _live_execute_order(...):
    if default_type == "margin":
        body = {
            "type": "MARKET" if type == "market" else "LIMIT",
            "symbol": exchange_symbol,
            # LONG: positive amount, SHORT: negative amount
            "amount": str(amount) if side == "buy" else f"-{amount}",
            "price": str(price) if type == "limit" else "1",
            "flags": 65536,  # Margin flag
        }
```

---

## Phase 5: Configuration Files

### 5.1 `config/long.yaml`
```yaml
# Long-only trading instance
trading:
  instance: "long"           # NEW: Instance identifier
  trade_direction: "long"    # NEW: long | short | both
  
  symbols:
    - name: "BTC/USDT"
      symbol: "tBTCUST"
      allocation_pct: 50.0
      enabled: true
    - name: "ETH/USDT"
      symbol: "tETHUST"
      allocation_pct: 30.0
      enabled: true
    - name: "SOL/USDT"
      symbol: "tSOLUST"
      allocation_pct: 20.0
      enabled: true
  
  initial_capital: 263.4     # 50% of $526.80
  daily_target: 25.0         # $25/day target
  max_risk_per_trade: 0.02
  max_open_positions: 3      # 1 per pair max for long
  position_size_mode: "dynamic"

exchange:
  name: "bitfinex"
  default_type: "margin"
  rate_limit: 1.0
  nonce_file: "instances/long/.nonce"  # Instance-specific nonce
  api_key: "${BITFINEX_API_KEY}"
  api_secret: "${BITFINEX_API_SECRET}"

# ... rest same as default ...

data:
  ohlcv_dir: "instances/long/data/ohlcv/"
  models_dir: "instances/long/data/models/"
  trades_file: "instances/long/data/trades.csv"
  log_file: "instances/long/logs/bot.log"

monitoring:
  telegram_alerts: true
  telegram_bot_token: "${TELEGRAM_BOT_TOKEN_LONG}"  # Separate bot or same
  telegram_chat_id: "${TELEGRAM_CHAT_ID}"
```

### 5.2 `config/short.yaml`
```yaml
# Short-only trading instance
trading:
  instance: "short"
  trade_direction: "short"   # Only short signals
  
  symbols:
    - name: "BTC/USDT"
      symbol: "tBTCUST"
      allocation_pct: 50.0
      enabled: true
    - name: "ETH/USDT"
      symbol: "tETHUST"
      allocation_pct: 30.0
      enabled: true
    - name: "SOL/USDT"
      symbol: "tSOLUST"
      allocation_pct: 20.0
      enabled: true
  
  initial_capital: 263.4     # 50% of $526.80
  daily_target: 25.0
  max_risk_per_trade: 0.02
  max_open_positions: 3
  position_size_mode: "dynamic"

exchange:
  name: "bitfinex"
  default_type: "margin"
  rate_limit: 1.0
  nonce_file: "instances/short/.nonce"
  api_key: "${BITFINEX_API_KEY}"
  api_secret: "${BITFINEX_API_SECRET}"

data:
  ohlcv_dir: "instances/short/data/ohlcv/"
  models_dir: "instances/short/data/models/"
  trades_file: "instances/short/data/trades.csv"
  log_file: "instances/short/logs/bot.log"

monitoring:
  telegram_alerts: true
  telegram_bot_token: "${TELEGRAM_BOT_TOKEN_SHORT}"
  telegram_chat_id: "${TELEGRAM_CHAT_ID}"
```

---

## Phase 6: Startup Scripts

### 6.1 `start_dual.sh`
```bash
#!/bin/bash
# Start both long and short instances

cd "$(dirname "$0")"
source .venv/bin/activate

# Create instance directories
mkdir -p instances/long/{data/{ohlcv,models},logs}
mkdir -p instances/short/{data/{ohlcv,models},logs}

# Kill any existing instances
pkill -f "main.py --mode live --instance long" 2>/dev/null
pkill -f "main.py --mode live --instance short" 2>/dev/null
sleep 2

echo "Starting LONG instance..."
nohup python3 main.py --mode live --instance long --config config/long.yaml \
    > instances/long/logs/stdout.log 2>&1 &
LONG_PID=$!
echo $LONG_PID > instances/long/pid
echo "Long bot PID: $LONG_PID"

echo "Starting SHORT instance..."
nohup python3 main.py --mode live --instance short --config config/short.yaml \
    > instances/short/logs/stdout.log 2>&1 &
SHORT_PID=$!
echo $SHORT_PID > instances/short/pid
echo "Short bot PID: $SHORT_PID"

echo "Both instances started. Monitor with:"
echo "  tail -f instances/long/logs/bot.log"
echo "  tail -f instances/short/logs/bot.log"
```

### 6.2 `stop_dual.sh`
```bash
#!/bin/bash
# Stop both instances gracefully

if [ -f instances/long/pid ]; then
    kill $(cat instances/long/pid) 2>/dev/null
    rm instances/long/pid
fi

if [ -f instances/short/pid ]; then
    kill $(cat instances/short/pid) 2>/dev/null
    rm instances/short/pid
fi

pkill -f "main.py --mode live --instance"
echo "Both instances stopped"
```

### 6.3 `status_dual.sh`
```bash
#!/bin/bash
# Check status of both instances

echo "=== LONG Instance ==="
if [ -f instances/long/pid ] && kill -0 $(cat instances/long/pid) 2>/dev/null; then
    echo "Status: RUNNING (PID: $(cat instances/long/pid))"
else
    echo "Status: STOPPED"
fi
tail -5 instances/long/logs/bot.log 2>/dev/null | grep -E "(PnL|Target|Trade)" || echo "No recent activity"

echo ""
echo "=== SHORT Instance ==="
if [ -f instances/short/pid ] && kill -0 $(cat instances/short/pid) 2>/dev/null; then
    echo "Status: RUNNING (PID: $(cat instances/short/pid))"
else
    echo "Status: STOPPED"
fi
tail -5 instances/short/logs/bot.log 2>/dev/null | grep -E "(PnL|Target|Trade)" || echo "No recent activity"
```

---

## Phase 7: Risk Management Updates

### 7.1 Cross-Instance Position Awareness
Since both bots share the same exchange account, we need:
```python
class RiskManager:
    def __init__(self, config: dict):
        self.instance = config.get("trading", {}).get("instance", "default")
        self.trade_direction = config.get("trading", {}).get("trade_direction", "both")
    
    def check_account_exposure(self, all_positions: list) -> tuple:
        """Check combined exposure across both instances."""
        long_exposure = sum(p['amount'] * p['entry_price'] 
                          for p in all_positions if p['side'] == 'buy')
        short_exposure = sum(p['amount'] * p['entry_price']
                           for p in all_positions if p['side'] == 'sell')
        
        # Net exposure check
        total_capital = self.config['trading']['initial_capital'] * 2  # Both instances
        net_exposure = abs(long_exposure - short_exposure)
        
        if net_exposure > total_capital * 0.8:  # 80% max net exposure
            return False, f"Net exposure too high: ${net_exposure:.2f}"
        
        return True, "OK"
```

### 7.2 Hedging Detection
```python
def detect_hedge_opportunity(long_positions: list, short_positions: list) -> dict:
    """
    Detect if we have opposing positions on same symbol.
    Could be intentional hedge or accidental conflict.
    """
    for long_pos in long_positions:
        for short_pos in short_positions:
            if long_pos['symbol'] == short_pos['symbol']:
                return {
                    'symbol': long_pos['symbol'],
                    'long_size': long_pos['amount'],
                    'short_size': short_pos['amount'],
                    'net': long_pos['amount'] - short_pos['amount'],
                    'hedge_ratio': min(long_pos['amount'], short_pos['amount']) / 
                                  max(long_pos['amount'], short_pos['amount'])
                }
    return None
```

---

## Phase 8: Testing Strategy

### 8.1 Paper Mode Testing
```bash
# Terminal 1 - Long instance
python3 main.py --mode paper --instance long --config config/long.yaml

# Terminal 2 - Short instance
python3 main.py --mode paper --instance short --config config/short.yaml
```

### 8.2 Test Cases
| Test | Expected | Check |
|------|----------|-------|
| Long gets BUY signal | Executes long | ✅ |
| Long gets SELL signal | Converts to HOLD | ✅ |
| Short gets SELL signal | Executes short | ✅ |
| Short gets BUY signal | Converts to HOLD | ✅ |
| Both hit nonce simultaneously | No collision | ✅ |
| Same symbol, both directions | Both can hold position | ✅ |
| Daily target hit on one | Other continues | ✅ |

---

## Phase 9: Monitoring & Alerts

### 9.1 Separate Telegram Channels (Optional)
```yaml
# long.yaml
monitoring:
  telegram_bot_token: "${TELEGRAM_BOT_TOKEN}"
  telegram_chat_id: "${TELEGRAM_CHAT_ID_LONG}"  # Different topic/channel
  instance_prefix: "🟢 LONG"  # Prefix all messages

# short.yaml
monitoring:
  telegram_bot_token: "${TELEGRAM_BOT_TOKEN}"
  telegram_chat_id: "${TELEGRAM_CHAT_ID_SHORT}"
  instance_prefix: "🔴 SHORT"
```

### 9.2 Combined Dashboard
Web dashboard shows both instances:
- Long PnL, positions, targets
- Short PnL, positions, targets
- Combined metrics

---

## Implementation Checklist

### Core Changes
- [ ] Add `--instance` and `--nonce-file` CLI args to main.py
- [ ] Modify `TradingBot` class to accept trade_direction
- [ ] Update `StrategySelector` with direction filter
- [ ] Fix `BitfinexClient._v2_post()` nonce generation
- [ ] Update `ExecutionEngine` with direction validation

### Configuration
- [ ] Create `config/long.yaml`
- [ ] Create `config/short.yaml`
- [ ] Update `TradingBot` to read instance paths

### Infrastructure
- [ ] Create `instances/` directory structure
- [ ] Create `start_dual.sh`
- [ ] Create `stop_dual.sh`
- [ ] Create `status_dual.sh`

### Testing
- [ ] Paper mode dual test
- [ ] Nonce collision test
- [ ] Direction blocking test
- [ ] Live mode with small size

### Documentation
- [ ] Update README with dual-instance setup
- [ ] Document nonce fix
- [ ] Add troubleshooting guide

---

## Risk Considerations

| Risk | Mitigation |
|------|------------|
| Both instances hit SL simultaneously | 50% capital each, max 2% risk per trade = max 4% total risk |
| Nonce collision | Separate nonce files + atomic writes |
| API rate limits | Staggered cycles (long at :00, short at :30) |
| Over-leverage | Combined position limit check |
| Network partition | Instance isolation, independent recovery |

---

## Expected Performance

With $263.40 each, $25/day target:
- **Long instance**: ~9.5% daily return target
- **Short instance**: ~9.5% daily return target
- **Combined**: $50/day total target (same as current)

Benefits:
1. Capture moves in BOTH directions
2. Natural hedge during chop (long + short both lose small = flat)
3. Double the trade opportunities
4. Reduced drawdowns via diversification
