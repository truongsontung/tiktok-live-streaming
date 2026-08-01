#!/bin/bash
cd /home/vps2/tiktok_live || exit 1

echo "[restart] stopping server..."
pkill -9 -f "venv/bin/python3 app.py" 2>/dev/null
sleep 2

echo "[restart] waiting for port 8888 to release..."
tries=0
while ! python3 -c "import socket; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1',8888)); s.close()" 2>/dev/null; do
  tries=$((tries+1))
  if [ $tries -ge 15 ]; then echo "[restart] ERROR: port 8888 still busy after 15s"; exit 1; fi
  sleep 1
done
echo "[restart] port 8888 free"

echo "[restart] starting server (detached)..."
setsid venv/bin/python3 app.py > logs/server.log 2>&1 < /dev/null &

sleep 4
if pgrep -f "venv/bin/python3 app.py" >/dev/null; then
  echo "[restart] OK: server RUNNING"
  curl -s -m 4 -u admin:tiktok99 "http://127.0.0.1:8888/api/status" | python3 -c "import sys,json; d=json.load(sys.stdin); print('status:', d.get('status'), '| running:', d.get('is_running'))" 2>/dev/null || echo "[restart] WARN: api not responding"
else
  echo "[restart] ERROR: server failed to start"
  tail -10 logs/server.log
  exit 1
fi
