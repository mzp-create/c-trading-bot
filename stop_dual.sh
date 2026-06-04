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
            echo "Stopping $instance instance (PID: $PID)..."
            kill "$PID"
            sleep 2
            if kill -0 "$PID" 2>/dev/null; then
                echo "  Force killing $instance..."
                kill -9 "$PID" 2>/dev/null || true
            fi
            echo "  ✓ $instance stopped"
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
pkill -f "main.py --instance long" 2>/dev/null || true
pkill -f "main.py --instance short" 2>/dev/null || true

echo ""
echo "=========================================="
echo "All instances stopped"
echo "=========================================="
