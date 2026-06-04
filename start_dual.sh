#!/bin/bash
# Start both long and short trading instances
# Usage: ./start_dual.sh [--paper|--live]

set -e

cd "$(dirname "$0")"
source .venv/bin/activate

MODE="${1:---paper}"
MODE=$(echo "$MODE" | sed 's/--//')

echo "=========================================="
echo "Hermes Dual-Instance Trading Bot"
echo "Mode: ${MODE^^}"
echo "=========================================="

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
nohup python3 main.py --mode "$MODE" --instance long --config config/long.yaml \
    > instances/long/logs/stdout.log 2>&1 &
LONG_PID=$!
echo $LONG_PID > instances/long/pid
echo "  ✓ Long bot PID: $LONG_PID"
echo "  ✓ Logs: instances/long/logs/bot.log"
echo "  ✓ Data: instances/long/data/"

echo ""
echo "Starting SHORT instance..."
nohup python3 main.py --mode "$MODE" --instance short --config config/short.yaml \
    > instances/short/logs/stdout.log 2>&1 &
SHORT_PID=$!
echo $SHORT_PID > instances/short/pid
echo "  ✓ Short bot PID: $SHORT_PID"
echo "  ✓ Logs: instances/short/logs/bot.log"
echo "  ✓ Data: instances/short/data/"

echo ""
echo "=========================================="
echo "Both instances started successfully!"
echo "=========================================="
echo ""
echo "Monitor commands:"
echo "  Long bot:   tail -f instances/long/logs/bot.log"
echo "  Short bot:  tail -f instances/short/logs/bot.log"
echo ""
echo "Status:       ./status_dual.sh"
echo "Stop:         ./stop_dual.sh"
echo ""
echo "Telegram alerts will show:"
echo "  🟢 LONG - for long instance trades"
echo "  🔴 SHORT - for short instance trades"
echo ""
