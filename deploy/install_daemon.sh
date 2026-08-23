#!/bin/bash
# ============================================================================
# Install JARVIS as a macOS launchd user daemon (auto-start on login,
# auto-restart on crash).
#
#   ./deploy/install_daemon.sh          # installs + starts headless daemon
#   ./deploy/install_daemon.sh remove   # unloads and removes the plist
#
# Renders com.jarvis.assistant.plist from its .in template with this
# machine's absolute paths, then loads it into ~/Library/LaunchAgents.
# ============================================================================
set -eu

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$DIR/deploy/com.jarvis.assistant.plist.in"
PLIST_NAME="com.jarvis.assistant.plist"
TARGET="$HOME/Library/LaunchAgents/$PLIST_NAME"
PY="$DIR/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
LOG_FILE="$DIR/jarvis.log"

if [ "${1:-}" = "remove" ]; then
    echo "Unloading and removing $TARGET ..."
    launchctl bootout "gui/$(id -u)/com.jarvis.assistant" 2>/dev/null || \
        launchctl unload "$TARGET" 2>/dev/null || true
    rm -f "$TARGET"
    echo "Daemon removed."
    exit 0
fi

[ -f "$TEMPLATE" ] || { echo "Template missing: $TEMPLATE"; exit 1; }
mkdir -p "$HOME/Library/LaunchAgents"

# Stop any jarvisctl-managed instance first to avoid port conflicts.
if [ -f "$DIR/deploy/jarvis.pid" ]; then
    OLD_PID="$(cat "$DIR/deploy/jarvis.pid" 2>/dev/null || true)"
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Stopping existing JARVIS (PID $OLD_PID) first..."
        kill -TERM "$OLD_PID" 2>/dev/null || true
        sleep 2
    fi
    rm -f "$DIR/deploy/jarvis.pid"
fi

sed -e "s|@PROJECT_DIR@|$DIR|g" \
    -e "s|@PYTHON_BIN@|$PY|g" \
    -e "s|@LOG_FILE@|$LOG_FILE|g" \
    "$TEMPLATE" > "$TARGET"

echo "Loaded config: $TARGET"
launchctl bootstrap "gui/$(id -u)" "$TARGET" 2>/dev/null || \
    launchctl load "$TARGET"

sleep 3
if pgrep -f "$DIR/main.py --headless" >/dev/null 2>&1; then
    echo "✅ JARVIS daemon is running headless."
    echo "   Logs: $LOG_FILE"
    echo "   Remove with: $0 remove"
else
    echo "⚠️  Daemon registered but not confirmed running yet — check $LOG_FILE"
fi
