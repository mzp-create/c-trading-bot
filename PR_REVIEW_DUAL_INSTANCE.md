# PR Review: Dual-Instance Trading Bot Migration

## Branch Strategy
```
main (current live bot)
  ↓
feature/dual-instance-long-short
  ↓
PR #1 → Code Review → Testing → Merge to main
```

---

## PR 1: Core Architecture Changes

### Files Changed
| File | Lines | Change Type | Risk Level |
|------|-------|-------------|------------|
| `main.py` | +45/-12 | Add --instance, --nonce-file args | Medium |
| `strategies/selector.py` | +38/-5 | Add direction filter | Medium |
| `execution/engine.py` | +52/-8 | Direction validation, short support | High |
| `market_data/bitfinex_client.py` | +89/-25 | Nonce isolation, atomic writes | Critical |
| `risk/manager.py` | +28/-3 | Cross-instance exposure check | Medium |

---

## Code Review Checklist

### 1. CLI & Entry Point (main.py)
```python
# REVIEW: Check these additions
def main():
    parser.add_argument('--instance', choices=['long', 'short', 'both'], 
                       default='both', help='Trading direction bias')
    parser.add_argument('--nonce-file', type=str,
                       help='Path to instance-specific nonce file')
```

**Review Items:**
- [ ] Default behavior preserves backward compatibility (`--instance both`)
- [ ] `--nonce-file` is optional (falls back to instance-based naming)
- [ ] Instance name validated before use in file paths
- [ ] Help text explains the options clearly

### 2. Direction Filter (strategies/selector.py)
```python
# REVIEW: Verify filter logic
def _filter_by_direction(self, signal: dict) -> dict:
    if self.trade_direction == "both":
        return signal
    
    sig_type = signal.get("signal", "HOLD")
    
    # BLOCKING logic - ensure this is correct
    if self.trade_direction == "long" and sig_type == "SELL":
        return {**signal, "signal": "HOLD", 
               "reason": signal.get("reason", "") + " | [BLOCKED:long_only]"}
    
    if self.trade_direction == "short" and sig_type == "BUY":
        return {**signal, "signal": "HOLD",
               "reason": signal.get("reason", "") + " | [BLOCKED:short_only]"}
    
    return signal
```

**Review Items:**
- [ ] Signal mutation is non-destructive (preserves original reason)
- [ ] BLOCKED tag added for debugging/auditing
- [ ] Case-insensitive comparison (BUY vs buy)
- [ ] Edge case: signal is None or missing

### 3. Nonce Fix (market_data/bitfinex_client.py) - CRITICAL
```python
# REVIEW: Atomic nonce generation
def _v2_post(self, path: str, body: dict) -> list:
    nonce_dir = Path(self._nonce_path).parent
    nonce_dir.mkdir(parents=True, exist_ok=True)
    
    fd = os.open(self._nonce_path, os.O_RDWR | os.O_CREAT, 0o600)
    
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # Exclusive lock
        
        # Read current
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 64).decode().strip()
        last_nonce = int(raw) if raw else None
        
        # Generate new nonce
        ts_nonce = int(time.time() * 1_000_000)
        
        if last_nonce is None:
            new_nonce = ((ts_nonce + 999_999) // 1_000_000) * 1_000_000
        else:
            min_next = last_nonce + 1_000_000
            new_nonce = max(ts_nonce, min_next)
            new_nonce = ((new_nonce + 999_999) // 1_000_000) * 1_000_000
        
        # ATOMIC WRITE: temp file + rename
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
```

**Review Items:**
- [ ] File permissions are restrictive (0o600)
- [ ] Exclusive lock prevents race conditions
- [ ] Temp file in same filesystem as target (for atomic rename)
- [ ] PID + thread ID in temp name prevents collision
- [ ] `fsync` before rename ensures durability
- [ ] Cleanup on exception (temp file removal)
- [ ] Nonce always ends in zeros (Bitfinex requirement)
- [ ] Monotonic increase guaranteed (last + 1M minimum)

### 4. Execution Engine (execution/engine.py)
```python
# REVIEW: Direction validation before execution
def execute_order(self, symbol: str, side: str, amount: float, ...):
    side = side.lower()
    
    # Direction gate
    if self.trade_direction == "long" and side == "sell":
        existing = self._get_position(symbol)
        if not existing or existing.get("side") != "buy":
            return {"success": False, 
                   "error": "SELL not allowed in long-only mode (no long to close)"}
    
    if self.trade_direction == "short" and side == "buy":
        existing = self._get_position(symbol)
        if not existing or existing.get("side") != "sell":
            return {"success": False,
                   "error": "BUY not allowed in short-only mode (no short to cover)"}
```

**Review Items:**
- [ ] Closing positions is always allowed (sell to close long, buy to cover short)
- [ ] Error messages clearly indicate why blocked
- [ ] Position check uses correct side naming (buy/sell vs long/short)
- [ ] Short selling uses negative amount for Bitfinex margin

### 5. Short Order Format (execution/engine.py)
```python
# REVIEW: Short order via margin
def _live_execute_order(...):
    if default_type == "margin":
        body = {
            "type": "MARKET" if type == "market" else "LIMIT",
            "symbol": exchange_symbol,  # e.g., "tBTCUST"
            # LONG: positive, SHORT: negative
            "amount": str(amount) if side == "buy" else f"-{amount}",
            "price": str(price) if type == "limit" else "1",
            "flags": 65536,  # Margin flag
        }
```

**Review Items:**
- [ ] Negative amount correctly formatted for shorts
- [ ] Symbol format correct (tBTCUST not BTC/USDT)
- [ ] Margin flag (65536 = 0x10000) set correctly

---

## Testing Strategy

### Phase 1: Unit Tests (Local, No API)
```python
# test_direction_filter.py
class TestDirectionFilter(unittest.TestCase):
    def test_long_blocks_sell(self):
        selector = StrategySelector(config, trade_direction="long")
        signal = {"signal": "SELL", "confidence": 0.8, "reason": "test"}
        result = selector._filter_by_direction(signal)
        self.assertEqual(result["signal"], "HOLD")
        self.assertIn("BLOCKED", result["reason"])
    
    def test_long_allows_buy(self):
        selector = StrategySelector(config, trade_direction="long")
        signal = {"signal": "BUY", "confidence": 0.8, "reason": "test"}
        result = selector._filter_by_direction(signal)
        self.assertEqual(result["signal"], "BUY")
    
    def test_short_blocks_buy(self):
        selector = StrategySelector(config, trade_direction="short")
        signal = {"signal": "BUY", "confidence": 0.8, "reason": "test"}
        result = selector._filter_by_direction(signal)
        self.assertEqual(result["signal"], "HOLD")
    
    def test_short_allows_sell(self):
        selector = StrategySelector(config, trade_direction="short")
        signal = {"signal": "SELL", "confidence": 0.8, "reason": "test"}
        result = selector._filter_by_direction(signal)
        self.assertEqual(result["signal"], "SELL")

# test_nonce_atomic.py
class TestNonceAtomic(unittest.TestCase):
    def test_nonce_monotonic_increase(self):
        client = BitfinexClient(config, nonce_file="/tmp/test_nonce")
        nonce1 = client._generate_nonce()
        nonce2 = client._generate_nonce()
        nonce3 = client._generate_nonce()
        
        self.assertGreater(int(nonce2), int(nonce1))
        self.assertGreater(int(nonce3), int(nonce2))
    
    def test_nonce_ends_in_zeros(self):
        client = BitfinexClient(config, nonce_file="/tmp/test_nonce")
        nonce = client._generate_nonce()
        self.assertEqual(int(nonce) % 1_000_000, 0)
    
    def test_concurrent_nonce_access(self):
        """Simulate race condition - should not collide"""
        import threading
        
        nonces = []
        def generate():
            client = BitfinexClient(config, nonce_file="/tmp/test_nonce_concurrent")
            for _ in range(10):
                nonces.append(client._generate_nonce())
        
        threads = [threading.Thread(target=generate) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # All nonces should be unique
        self.assertEqual(len(nonces), len(set(nonces)))
```

### Phase 2: Integration Tests (Paper Mode)
```bash
# Test 1: Long instance only generates longs
python3 -m pytest tests/test_integration_long.py -v
# Expected: All SELL signals converted to HOLD

# Test 2: Short instance only generates shorts
python3 -m pytest tests/test_integration_short.py -v
# Expected: All BUY signals converted to HOLD

# Test 3: Dual instance simulation
python3 tests/test_dual_paper.py --duration 1h
# Expected: Both instances run, opposite directions blocked
```

### Phase 3: Dry Run (Parallel to Live)
```bash
# Terminal 1: Keep current live bot running
# Terminal 2: Start dual instances in PAPER mode
./start_dual_paper.sh

# Monitor for 24-48 hours
# Compare signals:
# - Long instance should have generated X BUYs
# - Short instance should have generated Y SELLs
# - No overlapping positions
```

### Phase 4: Limited Live Test
```bash
# Stop current bot
kill $(cat pidfile)

# Start with 10% capital each ($26 each = $52 total, vs $526)
# Config: initial_capital: 26.34

./start_dual_live_limited.sh

# Monitor:
# - Both connect to exchange
# - Both get valid nonces (no "nonce too small" errors)
# - Orders execute in correct direction
# - Positions show correctly in Bitfinex
```

---

## Validation Checklist

### Pre-Merge Requirements
- [ ] All unit tests pass
- [ ] Integration tests pass
- [ ] Paper mode dual test runs 24h without error
- [ ] Nonce stress test (1000 rapid calls) passes
- [ ] Backward compatibility verified (single instance mode works)
- [ ] Code review signed off

### Post-Merge Smoke Tests
- [ ] Config files load correctly
- [ ] Both instances start without import errors
- [ ] Telegram alerts show correct instance prefix
- [ ] Logs go to correct directories
- [ ] Nonce files created in correct locations

### Live Trading Validation
- [ ] First long position opens correctly
- [ ] First short position opens correctly
- [ ] Stop-loss triggers on long position
- [ ] Stop-loss triggers on short position
- [ ] Daily PnL tracking correct per instance
- [ ] Combined dashboard shows both

---

## Rollback Plan

### If Issues Detected
```bash
# Emergency stop both instances
./stop_dual.sh

# Restart original single bot
python3 main.py --mode live --config config/default.yaml

# The original config/default.yaml is unchanged
```

### Rollback Triggers
| Issue | Action |
|-------|--------|
| Nonce errors on both instances | Immediate rollback, investigate nonce file |
| Orders not executing | Check API permissions, rollback if needed |
| Wrong direction orders | Emergency stop, code review |
| Excessive losses (>10% in 1h) | Stop, manual review, rollback |
| Telegram not receiving alerts | Non-critical, can fix forward |

---

## Performance Metrics to Monitor

### Per Instance
```
Instance: long
- Daily PnL: $X / $25 target
- Win rate: X%
- Avg trade duration: X min
- Max drawdown: X%
- Orders executed: X
- Orders blocked: X

Instance: short
- Daily PnL: $X / $25 target
- Win rate: X%
- Avg trade duration: X min
- Max drawdown: X%
- Orders executed: X
- Orders blocked: X
```

### Combined
```
Total PnL: $X / $50 target
Total trades: X
Net exposure: $X long, $X short
Hedge ratio: X% (overlap between long/short positions)
API calls: X (check rate limit headroom)
```

---

## Review Sign-Off

| Reviewer | Role | Status | Date |
|----------|------|--------|------|
| @mzphs_247_tb_bot | Author | ⏳ Pending | - |
| Hermes Agent | Reviewer | ⏳ In Progress | - |
| (Optional) Peer | Reviewer | ⏳ Pending | - |

### Approval Criteria
- [ ] All review items addressed
- [ ] Tests passing
- [ ] Documentation updated
- [ ] Rollback plan tested
