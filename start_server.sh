#!/bin/bash
# TikTok Live Server Startup Script
# Starts the web dashboard server in a tmux session

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION_NAME="tiktok-live"
PORT="${1:-8888}"

# Check if already running
if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "[TikTok Live] Server is already running in tmux session '$SESSION_NAME' on port $PORT"
    echo "  Stop with: tmux kill-session -t $SESSION_NAME"
    exit 0
fi

echo "[TikTok Live] Starting web server on port $PORT..."
tmux new-session -d -s "$SESSION_NAME" "cd $BASE_DIR && $BASE_DIR/venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port $PORT"

sleep 2

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "[TikTok Live] Server started successfully!"
    echo "  Access: http://127.0.0.1:$PORT"
    echo "  Dashboard: https://freeforyou.win/tiktok_live/"
    echo "  Status API: http://127.0.0.1:$PORT/api/status"
    echo "  CLI: $BASE_DIR/tiktok-live web $PORT"
else
    echo "[TikTok Live] Failed to start server. Check logs:"
    echo "  $BASE_DIR/logs/server.log"
    exit 1
fi
