#!/usr/bin/env bash
set -euo pipefail

# Hermes Trading Bot — Dashboard Launcher

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load environment variables from parent .env
set -a
source ../.env
set +a

export PATH="/mnt/hermes-data/.hermes/hermes-agent/venv/bin:$PATH"

echo "🤖 Starting Hermes Trading Bot Dashboard..."
echo "   Port: 8999"
echo ""

exec uvicorn api_server:app \
  --host 0.0.0.0 \
  --port 8999 \
  --log-level info
