#!/usr/bin/env bash
# restart.sh — Start server (or restart if already running).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$PROJECT_DIR/.bridge.pid"
LOGFILE="$PROJECT_DIR/bridge.log"
PORT="${BRIDGE_WEB_PORT:-8088}"
VENV_DIR="$PROJECT_DIR/venv"

# Activate virtual environment if exists
if [[ -f "$VENV_DIR/bin/activate" ]]; then
    source "$VENV_DIR/bin/activate"
fi

is_running() {
    # Check by PID file
    if [[ -f "$PIDFILE" ]]; then
        local pid
        pid=$(<"$PIDFILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return 0
        fi
        # Stale PID file
        rm -f "$PIDFILE"
    fi

    # Fallback: check by port
    local pid
    pid=$(lsof -ti :"$PORT" -sTCP:LISTEN 2>/dev/null | head -1) || true
    if [[ -n "$pid" ]]; then
        echo "$pid"
        return 0
    fi

    return 1
}

start_server() {
    echo "[$(date '+%H:%M:%S')] Starting server..."
    cd "$PROJECT_DIR"
    nohup python3 bridge.py >> "$LOGFILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PIDFILE"
    echo "[$(date '+%H:%M:%S')] Server started (PID $pid, port $PORT)"
    echo "[$(date '+%H:%M:%S')] Log: $LOGFILE"
}

stop_server() {
    local pid="$1"
    echo "[$(date '+%H:%M:%S')] Stopping server (PID $pid)..."
    kill "$pid" 2>/dev/null || true
    # Wait for shutdown (up to 5 seconds)
    for i in {1..10}; do
        if ! kill -0 "$pid" 2>/dev/null; then
            break
        fi
        sleep 0.5
    done
    # Force kill if still running
    if kill -0 "$pid" 2>/dev/null; then
        echo "[$(date '+%H:%M:%S')] Force stopping PID $pid..."
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"
    echo "[$(date '+%H:%M:%S')] Server stopped."
}

# --- Main logic ---
if pid=$(is_running); then
    echo "[$(date '+%H:%M:%S')] Server is running (PID $pid). Restarting..."
    stop_server "$pid"
    sleep 1
    start_server
else
    echo "[$(date '+%H:%M:%S')] Server not running. Starting..."
    start_server
fi
