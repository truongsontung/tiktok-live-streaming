#!/bin/bash
# TikTok VNC Login Helper
# Starts a virtual display with VNC for interactive TikTok login

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DISPLAY_NUM=99
VNC_PASSWORD="tiktok99"

# Kill existing processes
pkill -f "Xvfb :$DISPLAY_NUM" 2>/dev/null
pkill -f "x11vnc.*$DISPLAY_NUM" 2>/dev/null
pkill -f "openbox" 2>/dev/null
sleep 1

echo "======================================================"
echo "  TikTok VNC Login Environment"
echo "======================================================"
echo ""
echo "VNC Server: vnc://$(hostname -I | awk '{print $1}'):$((5900 + DISPLAY_NUM))"
echo "VNC Password: $VNC_PASSWORD"
echo "Display: :$DISPLAY_NUM"
echo ""
echo "Hướng dẫn:"
echo "  1. Mở VNC client (RealVNC, TigerVNC, hoặc trình duyệt)"
echo "  2. Kết nối tới: $(hostname -I | awk '{print $1}'):$((5900 + DISPLAY_NUM))"
echo "  3. Nhập mật khẩu: $VNC_PASSWORD"
echo "  4. Trong môi trường VNC, mở chromium-browser và đăng nhập TikTok"
echo "  5. Sau khi đăng nhập xong, chạy: ./tiktok-live session-extract"
echo ""

# Start Xvfb
Xvfb :$DISPLAY_NUM -screen 0 1280x800x24 &
XVFB_PID=$!
sleep 1

# Start x11vnc with password
x11vnc -display :$DISPLAY_NUM -passwd $VNC_PASSWORD -forever -shared -rfbport $((5900 + DISPLAY_NUM)) -o /dev/null 2>/dev/null &
VNC_PID=$!
sleep 1

# Start Openbox window manager
DISPLAY=:$DISPLAY_NUM openbox &
OPENBOX_PID=$!
sleep 1

# Launch chromium-browser
DISPLAY=:$DISPLAY_NUM chromium-browser \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-web-security \
  --disable-blink-features=AutomationControlled \
  --disable-features=IsolateOrigins,site-per-process \
  --no-first-run \
  --no-default-browser-check \
  --user-data-dir=/home/vps2/.config/chromium \
  "https://www.tiktok.com" 2>/dev/null &
CHROME_PID=$!

echo "Xvfb PID: $XVFB_PID"
echo "VNC PID: $VNC_PID"  
echo "Openbox PID: $OPENBOX_PID"
echo "Chromium PID: $CHROME_PID"
echo ""
echo "✅ Môi trường VNC đang chạy. Kết nối ngay để đăng nhập!"
echo "ℹ️ Nhấn Ctrl+C để dừng."
echo ""

# Wait for processes
wait $CHROME_PID 2>/dev/null

# Cleanup
kill $XVFB_PID 2>/dev/null
kill $VNC_PID 2>/dev/null
kill $OPENBOX_PID 2>/dev/null
