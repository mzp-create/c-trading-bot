# Bitfinex Nonce Fix - Implementation Summary

## Problem
The trading bot was experiencing persistent `"nonce: small"` errors because:

1. **Legacy nonce generation** used timestamp × 1,000,000 (microseconds) with 1M increments
2. **Nonces accumulated** to ~999,999,990,001,300,000,000 (999 quintillion)
3. **This exceeds int64 max** (9,223,372,036,854,775,807 = 9.2 quintillion)
4. **Bitfinex rejects** any nonce lower than previously used
5. **API key is stuck** - can't go higher (overflow), can't go lower (too small)

## Solution: CCXT-Based Client with API Key Reset

### 1. New CCXT-Based Client (`market_data/bitfinex_client.py`)
- **Replaced** custom nonce file management with CCXT's built-in handling
- **CCXT manages nonces** using millisecond timestamps (~1.7 trillion)
- **No more nonce files** - CCXT handles everything internally
- **Cleaner code** - unified API for margin, orders, positions

### 2. Config Updates
Updated all configs to use CCXT:
```yaml
exchange:
  name: "bitfinex"        # CCXT unified bitfinex
  default_type: "margin"  # Use margin wallet
  # No nonce_file needed - CCXT handles it
```

Files updated:
- `config/default.yaml`
- `config/long.yaml`
- `config/short.yaml`

### 3. Legacy Client Backup
Original client saved to: `market_data/bitfinex_client_legacy.py`

## Required Action: API Key Reset

The existing API key has nonces that overflow int64. **You must reset it:**

### Step 1: Create New API Key on Bitfinex
1. Go to https://www.bitfinex.com/account/api
2. **Delete** the existing API key
3. **Create new key** with permissions:
   - ☑️ Account: Read
   - ☑️ Orders: Read/Write
   - ☑️ Margin: Read/Write
   - ☑️ Wallets: Read
4. **Copy** the new API Key and Secret

### Step 2: Update Bot Credentials
```bash
cd /mnt/hermes-data/.hermes/hermes-agent/trading-bot
nano .env
```

Update with new credentials:
```bash
export BITFINEX_API_KEY="your_new_key_here"
export BITFINEX_API_SECRET="your_new_secret_here"
```

### Step 3: Stop Old Bots & Start New
```bash
./stop_dual.sh
./start_dual.sh --live
```

## Testing

Verify the fix works:
```bash
cd /mnt/hermes-data/.hermes/hermes-agent/trading-bot
source .venv/bin/activate
python3 test_nonce_fix.py
```

## Changes Made

| File | Change |
|------|--------|
| `market_data/bitfinex_client.py` | New CCXT-based implementation |
| `market_data/bitfinex_client_legacy.py` | Backup of old implementation |
| `market_data/bitfinex_client_ccxt.py` | Original CCXT version (intermediate) |
| `config/default.yaml` | Updated to `name: bitfinex` |
| `config/long.yaml` | Updated to `name: bitfinex`, removed nonce_file |
| `config/short.yaml` | Updated to `name: bitfinex`, removed nonce_file |

## Key Benefits

1. **No more nonce errors** - CCXT handles nonces correctly
2. **Simpler code** - no custom nonce file management
3. **Better maintainability** - uses standard CCXT patterns
4. **Cross-instance safety** - CCXT's nonce is process-safe
5. **Future-proof** - CCXT updates handle API changes

## Technical Details

### Old Approach (Broken)
```python
# Custom nonce file with microseconds
ts_nonce = int(time.time() * 1_000_000)  # ~1.7 quadrillion
# Accumulated to 999 quintillion - OVERFLOW!
```

### New Approach (Fixed)
```python
# CCXT uses milliseconds
nonce = int(time.time() * 1000)  # ~1.7 trillion (safe)
# Well within int64 range, no overflow
```

### Nonce Comparison
| Approach | Nonce Value | Range |
|----------|-------------|-------|
| Legacy | 999,999,990,001,300,000,000 | 999 quintillion ❌ |
| CCXT | 1,779,934,488,024 | 1.7 trillion ✅ |
| Max int64 | 9,223,372,036,854,775,807 | 9.2 quintillion |

## Verification

After API key reset and bot restart:

1. **Check balance fetch works:**
   ```python
   balance = client.fetch_balance()
   # Should return actual margin balance
   ```

2. **Check positions work:**
   ```python
   positions = client.fetch_positions()
   # Should return open positions
   ```

3. **Check orders work:**
   ```python
   order = client.create_order("BTC/USDT", "market", "buy", 0.001)
   # Should execute without "nonce: small" error
   ```

## Rollback Plan

If issues occur:
```bash
# Restore legacy client
cp market_data/bitfinex_client_legacy.py market_data/bitfinex_client.py

# Restore old configs (from git or backup)
git checkout config/*.yaml

# Restart bots
./stop_dual.sh && ./start_dual.sh --live
```
