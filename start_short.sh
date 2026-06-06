#!/bin/bash
# Convenience wrapper: start the SHORT instance via the shared launcher.
#   Usage: ./start_short.sh [paper|live]   (default: live)
set -euo pipefail
cd "$(dirname "$0")"
exec ./start_bot.sh short "${1:-live}"
