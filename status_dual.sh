#!/bin/bash
# Check status of both long and short trading instances

cd "$(dirname "$0")"

show_instance_status() {
    local instance=$1
    local emoji=$2
    local pid_file="instances/$instance/pid"
    local log_file="instances/$instance/logs/bot.log"
    
    echo ""
    echo "=========================================="
    echo "$emoji $instance Instance"
    echo "=========================================="
    
    # Check if running
    if [ -f "$pid_file" ]; then
        PID=$(cat "$pid_file")
        if kill -0 "$PID" 2>/dev/null; then
            echo "Status:     🟢 RUNNING"
            echo "PID:        $PID"
            echo "Uptime:     $(ps -o etime= -p $PID 2>/dev/null || echo 'unknown')"
        else
            echo "Status:     🔴 STOPPED (stale PID file)"
            rm -f "$pid_file"
        fi
    else
        echo "Status:     🔴 STOPPED"
    fi
    
    # Show recent activity
    if [ -f "$log_file" ]; then
        echo ""
        echo "Recent activity:"
        tail -5 "$log_file" 2>/dev/null | grep -E "(PnL|Trade|Position|Target|ERROR)" || \
            echo "  (No recent trades)"
        
        # Show last cycle summary if available
        LAST_CYCLE=$(grep -E "Cycle summary|Daily PnL" "$log_file" | tail -1)
        if [ -n "$LAST_CYCLE" ]; then
            echo ""
            echo "Last update:"
            echo "  $LAST_CYCLE"
        fi
    else
        echo "Log file:   Not found"
    fi
    
    # Show data directory size
    if [ -d "instances/$instance/data" ]; then
        echo ""
        echo "Data size:  $(du -sh instances/$instance/data 2>/dev/null | cut -f1)"
    fi
}

echo ""
echo "🤖 Hermes Dual-Instance Trading Bot Status"
echo "=========================================="
echo "Timestamp:  $(date)"

show_instance_status "long" "🟢"
show_instance_status "short" "🔴"

echo ""
echo "=========================================="
echo "Commands:"
echo "  Start:  ./start_dual.sh [--paper|--live]"
echo "  Stop:   ./stop_dual.sh"
echo "=========================================="
echo ""
