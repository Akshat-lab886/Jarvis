#!/bin/bash
# ============================================================================
# JARVIS process controller
#   ./deploy/jarvisctl.sh start [--headless]
#   ./deploy/jarvisctl.sh stop | status | restart [--headless]
#
# 'start' runs main.py detached (nohup) with a PID file. For a true macOS
# daemon that survives reboot, use install_daemon.sh instead.
# ============================================================================
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$DIR/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
PID_FILE="$DIR/deploy/jarvis.pid"
LOG_FILE="$DIR/jarvis.log"

is_running() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

start() {
    if is_running; then
        echo "JARVIS already running (PID $(cat "$PID_FILE"))."
        exit 0
    fi
    echo "Starting JARVIS ($*)..."
    nohup "$PY" "$DIR/main.py" "$@" >>"$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 2
    if is_running; then
        echo "Started. PID $(cat "$PID_FILE"). Logs: $LOG_FILE"
    else
        echo "Failed to start — check $LOG_FILE"
        rm -f "$PID_FILE"
        exit 1
    fi
}

stop() {
    if ! is_running; then
        echo "JARVIS is not running."
        rm -f "$PID_FILE"
        exit 0
    fi
    PID="$(cat "$PID_FILE")"
    echo "Stopping JARVIS (PID $PID)..."
    # SIGTERM triggers the graceful shutdown handler in main.py
    kill -TERM "$PID" 2>/dev/null || true
    for _ in $(seq 1 20); do
        kill -0 "$PID" 2>/dev/null || break
        sleep 0.5
    done
    if kill -0 "$PID" 2>/dev/null; then
        echo "Still alive — sending SIGKILL."
        kill -9 "$PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "Stopped."
}

status() {
    if is_running; then
        echo "JARVIS is running (PID $(cat "$PID_FILE"))."
    else
        echo "JARVIS is stopped."
        exit 1
    fi
}

case "${1:-}" in
    start)   shift; start "$@" ;;
    stop)    stop ;;
    restart) shift; stop; sleep 1; start "$@" ;;
    status)  status ;;
    *)
        echo "Usage: $0 {start [--headless] | stop | status | restart [--headless]}"
        exit 2
        ;;
esac
