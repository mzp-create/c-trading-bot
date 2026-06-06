#!/bin/bash
# Convenience wrapper: start the LONG instance via the shared launcher.
#   Usage: ./start_long.sh [paper|live]   (default: live)
set -euo pipefail
cd "$(dirname "$0")"
exec ./start_bot.sh long "${1:-live}"
