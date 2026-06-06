#!/bin/bash
# Single-instance launcher with per-instance API-key mapping.
#   Usage: ./start_bot.sh <long|short|default> [paper|live]   (default mode: live)
#
# Maps BITFINEX_<INSTANCE>_API_KEY -> BITFINEX_API_KEY so each instance uses its
# OWN Bitfinex key (separate nonce sequence) — required when long + short run at
# the same time. Live mode requires typing LIVE to confirm.
set -euo pipefail
cd "$(dirname "$0")"

INSTANCE="${1:?usage: ./start_bot.sh <long|short|default> [paper|live]}"
MODE="${2:-live}"

set -a; source .env; set +a

case "$INSTANCE" in
  long)
    export BITFINEX_API_KEY="${BITFINEX_LONG_API_KEY:-${BITFINEX_API_KEY:-}}"
    export BITFINEX_API_SECRET="${BITFINEX_LONG_API_SECRET:-${BITFINEX_API_SECRET:-}}" ;;
  short)
    export BITFINEX_API_KEY="${BITFINEX_SHORT_API_KEY:-${BITFINEX_API_KEY:-}}"
    export BITFINEX_API_SECRET="${BITFINEX_SHORT_API_SECRET:-${BITFINEX_API_SECRET:-}}" ;;
  default)
    : ;;  # use base BITFINEX_API_KEY as-is
  *)
    echo "Unknown instance '$INSTANCE' (expected: long | short | default)" >&2
    exit 1 ;;
esac

if [ "$MODE" = "live" ]; then
  echo "⚠️  Starting instance '$INSTANCE' in LIVE mode — this places REAL orders."
  echo "    Type LIVE to confirm:"
  read -r confirm
  [ "$confirm" = "LIVE" ] || { echo "Aborted."; exit 1; }
fi

source .venv/bin/activate
exec python main.py --mode "$MODE" --instance "$INSTANCE" --config "config/$INSTANCE.yaml"
