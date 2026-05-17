#!/bin/bash
# Hermes Crypto Trading Bot — Start Script
# Usage:
#   ./start.sh paper     # Paper trading (safe)
#   ./start.sh live      # Live trading (REAL MONEY!)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Activate venv
VENV="$SCRIPT_DIR/venv"
if [ ! -d "$VENV" ]; then
    VENV="/home/ubuntu/.hermes/hermes-agent/venv"
fi
source "$VENV/bin/activate"

MODE="${1:-paper}"

echo "=========================================="
echo "🤖 Hermes Crypto Trading Bot"
echo "Mode: $MODE"
echo "=========================================="

if [ "$MODE" = "live" ]; then
    # Load credentials from .env (in the CURRENT shell)
    # This needs to use set -a to export
    if [ -f .env ]; then
        echo "Loading credentials..."
        set -a
        source .env
        set +a
    else
        echo "❌ No .env file found!"
        exit 1
    fi

    # Verify keys are actually loaded
    if [ -z "$BITFINEX_API_KEY" ] || [ -z "$BITFINEX_API_SECRET" ]; then
        echo "❌ API keys not found in .env file!"
        echo "Make sure .env contains:"
        echo "  export BITFINEX_API_KEY='your_key'"
        echo "  export BITFINEX_API_SECRET='your_secret'"
        exit 1
    fi

    echo "✅ Bitfinex API key loaded: ${BITFINEX_API_KEY:0:8}..."
    
    # One last confirmation
    echo ""
    echo "⚠️  ⚠️  ⚠️  WARNING  ⚠️  ⚠️  ⚠️"
    echo "You are about to trade with REAL \$526.80!"
    echo "Max daily loss:  \$20"
    echo "Max drawdown:    15%"
    echo "Stop loss:       2% per trade"
    echo ""
    read -p "Type 'LIVE' to confirm: " confirm
    if [ "$confirm" != "LIVE" ]; then
        echo "Aborted."
        exit 0
    fi
fi

# Run bot — env vars are inherited by Python
if [ "$MODE" = "paper" ]; then
    exec python main.py --mode paper
elif [ "$MODE" = "live" ]; then
    exec python main.py --mode live
else
    echo "Usage: $0 {paper|live}"
    exit 1
fi
