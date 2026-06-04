# PR Summary: Dual-Instance Trading Bot (Long/Short)

## Overview
This PR adds support for running two independent trading bot instances:
- **Long Instance**: Only executes BUY signals (captures uptrends)
- **Short Instance**: Only executes SELL signals (captures downtrends)

Each instance operates with 50% of capital ($263.40) targeting $25/day.

---

## Changes Made

### 1. Core Architecture (`main.py`)
- Added `--instance` CLI argument: `long` | `short` | `default`
- Added `trade_direction` parameter to `TradingBot` class
- Instances auto-configure direction based on name (`--instance long` → `trade_direction="long"`)
- Maintains backward compatibility (default behavior unchanged)

### 2. Signal Filtering (`strategies/selector.py`)
- Added `trade_direction` parameter to `StrategySelector`
- Added `_filter_by_direction()` method that:
  - `long`: Blocks SELL signals (converts to HOLD with [BLOCKED:long_only] tag)
  - `short`: Blocks BUY signals (converts to HOLD with [BLOCKED:short_only] tag)
  - `both`: Passes all signals (backward compatible)
- Filters applied after strategy generation, before execution

### 3. Order Validation (`execution/engine.py`)
- Added `trade_direction` parameter to `ExecutionEngine`
- Added `_get_position_for_direction_check()` helper
- Validates orders before execution:
  - Long instance: Allows SELL only if closing existing long
  - Short instance: Allows BUY only if covering existing short
- Prevents accidental wrong-direction trades

### 4. Nonce Fix (`market_data/bitfinex_client.py`)
- **Critical Fix**: Instance-isolated nonce files
- Each instance uses separate `.bfx_nonce_{instance}` file
- Atomic write implementation (temp file + rename)
- Prevents nonce collision errors when running dual instances

### 5. Configuration Files
**`config/long.yaml`**:
- `trade_direction: "long"`
- `initial_capital: 263.4` (50% of total)
- `daily_target: 25.0`
- Instance-specific data directories
- Instance-specific nonce file

**`config/short.yaml`**:
- `trade_direction: "short"`
- Same capital split ($263.40)
- Same daily target ($25)
- Separate data directories
- Separate nonce file

### 6. Management Scripts
| Script | Purpose |
|--------|---------|
| `start_dual.sh` | Start both instances (paper or live) |
| `stop_dual.sh` | Gracefully stop both instances |
| `status_dual.sh` | Check status of both instances |

---

## Test Results

### Direction Filter Tests (12/12 PASS)
```
test_long_instance_blocks_sell          PASS
test_long_instance_allows_buy           PASS
test_short_instance_blocks_buy          PASS
test_short_instance_allows_sell         PASS
test_both_allows_all_signals            PASS
test_hold_passes_through                PASS
test_lowercase_signal_handling          PASS
test_missing_signal_defaults_to_hold    PASS
test_preserves_all_signal_fields        PASS
test_default_direction_is_both          PASS
test_explicit_long_direction            PASS
test_explicit_short_direction           PASS
```

### Files Changed
| File | Lines Changed | Description |
|------|---------------|-------------|
| `main.py` | +20/-3 | Instance support, CLI args |
| `strategies/selector.py` | +45/-5 | Direction filtering |
| `execution/engine.py` | +35/-2 | Order validation |
| `market_data/bitfinex_client.py` | +60/-15 | Nonce isolation |
| `config/long.yaml` | +135 (new) | Long instance config |
| `config/short.yaml` | +135 (new) | Short instance config |

---

## Migration Plan

### Phase 1: Paper Testing (Recommended: 24-48h)
```bash
./start_dual.sh --paper
# Monitor both instances
./status_dual.sh
```

### Phase 2: Limited Live (10% capital each)
```bash
# Edit configs: initial_capital: 26.34
./start_dual.sh --live
```

### Phase 3: Full Capital
```bash
# Edit configs: initial_capital: 263.4
./start_dual.sh --live
```

### Rollback (if needed)
```bash
./stop_dual.sh
python3 main.py --mode live --config config/default.yaml
```

---

## Benefits

1. **Capture Both Trends**: Long catches uptrends, short catches downtrends
2. **Natural Hedge**: In chop markets, both lose small instead of one direction bleeding
3. **Double Opportunities**: Twice the trade signals, same capital at risk
4. **Isolated Risk**: 50% capital each = same total risk, better diversification
5. **No Nonce Issues**: Separate nonce files prevent API collisions

---

## Risk Considerations

| Risk | Mitigation |
|------|------------|
| Both hit SL simultaneously | 2% risk per trade × 2 instances = 4% max daily risk |
| API rate limits | Staggered cycles, separate nonce files |
| Over-leverage | Position sizing calculated per-instance |
| Wrong-direction trade | Double validation (strategy filter + execution check) |

---

## Verification Checklist

- [x] Direction filter blocks wrong signals
- [x] Direction filter allows correct signals
- [x] Execution validates before order
- [x] Nonce generation is monotonic
- [x] Nonce files are instance-isolated
- [x] Configs load correctly
- [x] Backward compatibility maintained
- [x] Scripts are executable

---

## Post-Merge Monitoring

Watch for:
1. Telegram alerts show correct prefix (🟢 LONG / 🔴 SHORT)
2. Both instances generate trades in correct direction
3. No nonce errors in logs
4. Daily PnL tracked separately per instance
5. Combined performance meeting $50/day target

---

## Commands Quick Reference

```bash
# Start both instances (paper)
./start_dual.sh --paper

# Start both instances (live)
./start_dual.sh --live

# Check status
./status_dual.sh

# Stop both
./stop_dual.sh

# View logs
 tail -f instances/long/logs/bot.log
 tail -f instances/short/logs/bot.log
```

---

**Ready for Review** ✅
**Tested** ✅
**Backward Compatible** ✅
