#!/bin/bash
# Stop both long and short trading instances

cd "$(dirname "$0")"

echo "=========================================="
echo "Stopping Dual-Instance Trading Bots"
echo "=========================================="

stop_instance() {
    local instance=$1
    local pid_file="instances/$instance/pid"
    
    if [ -f "$pid_file" ]; then
        PID=$(cat "$pid_file")
        if kill -0 "$PID" 2>/dev/null; then
            # Guard against PID recycling: only kill if this PID is actually our
            # bot. A stale pid file pointing at a reused PID could otherwise kill
            # an unrelated process. The pkill fallback below still catches us.
            if ! ps -p "$PID" -o args= 2>/dev/null | grep -qE "main\.py.*--instance $instance"; then
                echo "  PID $PID is not the $instance bot (recycled?) — skipping direct kill"
                rm -f "$pid_file"
                return
            fi
            echo "Stopping $instance instance (PID: $PID) — graceful, waiting for position flatten..."
            kill "$PID" 2>/dev/null || true   # SIGTERM -> _handle_shutdown -> close_all_positions()
            # Wait for the bot to finish its current cycle and run its graceful
            # _shutdown() (which flattens open positions) before any SIGKILL. A
            # cycle (multi-symbol data fetch + LLM) can take ~15s, then the close
            # path adds a few more; SIGKILL before that would STRAND an open
            # position on the exchange. Poll up to GRACE seconds.
            local grace=30 waited=0
            while kill -0 "$PID" 2>/dev/null && [ "$waited" -lt "$grace" ]; do
                sleep 1
                waited=$((waited + 1))
            done
            if kill -0 "$PID" 2>/dev/null; then
                echo "  ! $instance still alive after ${grace}s — sending SIGKILL."
                echo "    WARNING: graceful flatten may NOT have completed — VERIFY no"
                echo "    open position remains:  python check_bfx_balance.py"
                kill -9 "$PID" 2>/dev/null || true
            else
                echo "  ✓ $instance stopped gracefully after ${waited}s"
            fi
        else
            echo "  $instance not running (stale PID file)"
        fi
        rm -f "$pid_file"
    else
        echo "  $instance PID file not found"
    fi
}

stop_instance "long"
stop_instance "short"

# Clean up any remaining processes
echo ""
echo "Cleaning up remaining processes..."
pkill -f "main.py.*--instance long" 2>/dev/null || true
pkill -f "main.py.*--instance short" 2>/dev/null || true

echo ""
echo "=========================================="
echo "All instances stopped"
echo "=========================================="
