#!/bin/bash
# Start trading instances. LONG-ONLY for now (shorting paused 2026-06-18).
# The SHORT instance is disabled below — re-enable it only after creating a
# separate Bitfinex sub-account for it (two keys on one account net each other;
# the account guard would refuse the 2nd instance anyway).
# Usage: ./start_dual.sh [--paper|--live]

set -e

cd "$(dirname "$0")"
source .venv/bin/activate
# Load per-instance keys (BITFINEX_LONG_/SHORT_*) so each instance can claim its
# own Bitfinex key below — sharing one key causes "nonce: small" races.
set -a; source .env; set +a

MODE="${1:---paper}"
MODE=$(echo "$MODE" | sed 's/--//')

if [ "$MODE" = "live" ]; then
    echo "⚠️  Starting BOTH instances in LIVE mode — this places REAL orders."
    echo "    Type LIVE to confirm:"
    read -r confirm
    [ "$confirm" = "LIVE" ] || { echo "Aborted."; exit 1; }
fi

echo "=========================================="
echo "Hermes Dual-Instance Trading Bot"
echo "Mode: ${MODE^^}"
echo "=========================================="

# Verify an instance is still alive a moment after launch. A fail-fast exit
# (e.g. KeyConflictError on a duplicate live key, or a bad config) must NOT be
# reported as "started successfully".
check_started() {
    local name="$1" pid="$2" log="$3"
    sleep 2
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        echo "  ✗ $name instance (PID ${pid:-?}) exited immediately — check $log"
        echo "    ---- last 20 lines of $log ----"
        tail -n 20 "$log" 2>/dev/null || true
        return 1
    fi
    echo "  ✓ $name instance healthy (PID $pid)"
    return 0
}

# Create instance directories
mkdir -p instances/long/{data/{ohlcv,models},logs}
mkdir -p instances/short/{data/{ohlcv,models},logs}

# Stop any existing instances
echo ""
echo "Stopping any existing instances..."
pkill -f "main.py --mode $MODE --instance long" 2>/dev/null || true
pkill -f "main.py --mode $MODE --instance short" 2>/dev/null || true
sleep 2

# Check if current bot is running and offer to stop it
CURRENT_PID=$(pgrep -f "main.py --mode live" | head -1)
if [ -n "$CURRENT_PID" ] && [ "$MODE" == "live" ]; then
    echo ""
    echo "⚠️  Existing live bot detected (PID: $CURRENT_PID)"
    echo "Stop it before starting dual instances? (y/n)"
    read -r response
    if [[ "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
        kill $CURRENT_PID
        echo "Stopped existing bot"
        sleep 2
    else
        echo "Continuing alongside existing bot..."
    fi
fi

echo ""
echo "Starting LONG instance..."
# Subshell so the LONG key export does not leak into the SHORT launch.
(
    export BITFINEX_API_KEY="${BITFINEX_LONG_API_KEY:-${BITFINEX_API_KEY:-}}"
    export BITFINEX_API_SECRET="${BITFINEX_LONG_API_SECRET:-${BITFINEX_API_SECRET:-}}"
    nohup python3 main.py --mode "$MODE" --instance long --config config/long.yaml \
        > instances/long/logs/stdout.log 2>&1 &
    echo $! > instances/long/pid
)
LONG_PID=$(cat instances/long/pid)
echo "  ✓ Long bot PID: $LONG_PID"
echo "  ✓ Logs: instances/long/logs/stdout.log"
echo "  ✓ Data: instances/long/data/"
check_started "LONG" "$LONG_PID" "instances/long/logs/stdout.log" || FAILED=1

# --- SHORT instance DISABLED (long-only focus, 2026-06-18) ---
# To re-enable: create a Bitfinex sub-account, set BITFINEX_SHORT_API_KEY/SECRET
# to its keys, then uncomment this block.
# echo ""
# echo "Starting SHORT instance..."
# # Subshell so the SHORT key export stays isolated to this instance.
# (
#     export BITFINEX_API_KEY="${BITFINEX_SHORT_API_KEY:-${BITFINEX_API_KEY:-}}"
#     export BITFINEX_API_SECRET="${BITFINEX_SHORT_API_SECRET:-${BITFINEX_API_SECRET:-}}"
#     nohup python3 main.py --mode "$MODE" --instance short --config config/short.yaml \
#         > instances/short/logs/stdout.log 2>&1 &
#     echo $! > instances/short/pid
# )
# SHORT_PID=$(cat instances/short/pid)
# echo "  ✓ Short bot PID: $SHORT_PID"
# echo "  ✓ Logs: instances/short/logs/stdout.log"
# echo "  ✓ Data: instances/short/data/"
# check_started "SHORT" "$SHORT_PID" "instances/short/logs/stdout.log" || FAILED=1

if [ "${FAILED:-0}" = "1" ]; then
    echo ""
    echo "=========================================="
    echo "⚠️  One or more instances FAILED to start — see logs above."
    echo "=========================================="
    exit 1
fi

echo ""
echo "=========================================="
echo "Long instance started successfully! (short disabled — long-only focus)"
echo "=========================================="
echo ""
echo "Monitor commands:"
echo "  Long bot:   tail -f instances/long/logs/stdout.log"
echo ""
echo "Status:       ./status_dual.sh"
echo "Stop:         ./stop_dual.sh"
echo ""
echo "Telegram alerts will show:"
echo "  🟢 LONG - for long instance trades"
echo ""
