#!/bin/bash
# Start LIVE trading. LONG-ONLY for now (shorting paused 2026-06-18).
# The SHORT instance is disabled below — re-enable it only after creating a
# separate Bitfinex sub-account for it (two keys on one account net each other;
# the account guard would otherwise refuse the 2nd instance anyway).
set -e
cd "$(dirname "$0")"

# Load env
set -a
source .env
set +a

mkdir -p instances/long/{data/{ohlcv,models},logs}
mkdir -p instances/short/{data/{ohlcv,models},logs}

# Kill any existing instances
pkill -f "main.py --mode live" 2>/dev/null || true
sleep 2

echo "Starting LONG instance..."
BITFINEX_API_KEY="$BITFINEX_LONG_API_KEY" BITFINEX_API_SECRET="$BITFINEX_LONG_API_SECRET" \
    .venv/bin/python3 main.py --mode live --instance long --config config/long.yaml \
    > instances/long/logs/stdout.log 2>&1 &
LONG_PID=$!
echo $LONG_PID > instances/long/pid
echo "Long PID: $LONG_PID"

# --- SHORT instance DISABLED (long-only focus, 2026-06-18) ---
# To re-enable: create a Bitfinex sub-account, set BITFINEX_SHORT_API_KEY/SECRET
# to its keys, then uncomment this block.
# echo "Starting SHORT instance..."
# BITFINEX_API_KEY="$BITFINEX_SHORT_API_KEY" BITFINEX_API_SECRET="$BITFINEX_SHORT_API_SECRET" \
#     .venv/bin/python3 main.py --mode live --instance short --config config/short.yaml \
#     > instances/short/logs/stdout.log 2>&1 &
# SHORT_PID=$!
# echo $SHORT_PID > instances/short/pid
# echo "Short PID: $SHORT_PID"

sleep 3

echo ""
echo "=== Status ==="
echo "Long:  PID $(cat instances/long/pid 2>/dev/null) - $(kill -0 $(cat instances/long/pid 2>/dev/null) 2>/dev/null && echo 'RUNNING' || echo 'FAILED')"
echo "Short: DISABLED (long-only focus)"

echo ""
echo "Logs:"
echo "  Long:  tail -f instances/long/logs/stdout.log"
