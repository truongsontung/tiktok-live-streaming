#!/usr/bin/env python3
"""
External TikTok Live comment forwarder.

Chay trên một máy/instance có IP sạch (hoặc qua proxy HTTP) để lắng nghe
real-time comments từ TikTok WebSocket, sau đó forward tới server chính qua
endpoint POST /api/live/comment-forward.

Lý do tách biệt:
  - VPS thường bị webcast.tiktok.com handshake 403 → WebSocket listener lỗi.
  - Chạy forwarder trên máy local/proxy → nhận comment thật → gửi về server.

Cấu hình (environment variables):
  USERNAME        : TikTok username đang live (bắt buộc)
  SERVER_URL      : http://[vps-ip]:8888  (mặc định http://127.0.0.1:8888)
  WEB_PROXY       : http://user:pass@ip:port  (nếu dùng proxy)
  WS_PROXY        : ws://user:pass@ip:port
  FORWARD_COOLDOWN: số giây tối thiểu giữa 2 forward (tránh flood)

Usage:
  python3 comment_forwarder.py
  USERNAME=songoku_superboy SERVER_URL=http://127.0.0.1:8888 python3 comment_forwarder.py
"""

import os
import sys
import time
import json
import asyncio
import logging
import urllib.request

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [Forwarder] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("Forwarder")

# Server URL - forwarder tự fetch session từ server (zero config username)
SERVER_URL = os.environ.get("SERVER_URL", "http://127.0.0.1:8888").rstrip("/")
# Optional username override (bypass auto-fetch)
USERNAME = os.environ.get("USERNAME", "").strip() or None
WEB_PROXY = os.environ.get("WEB_PROXY") or None
WS_PROXY = os.environ.get("WS_PROXY") or None
FORWARD_COOLDOWN = float(os.environ.get("FORWARD_COOLDOWN", "1.0"))

# TikTokLive import
try:
    from TikTokLive import TikTokLiveClient
    from TikTokLive.events.proto_events import CommentEvent
    TIKTOK_AVAILABLE = True
except ImportError as e:
    TIKTOK_AVAILABLE = False
    logger.error(f"TikTokLive not installed: {e}")


def forward_comment(username: str, comment: str) -> dict:
    payload = json.dumps({"username": username, "comment": comment}).encode()
    url = f"{SERVER_URL}/api/live/comment-forward"
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                logger.warning(f"Server returned HTTP {resp.status}")
                return {}
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.error(f"Forward failed to {url}: {e}")
        return {}


def start_listener():
    if not TIKTOK_AVAILABLE:
        logger.error("TikTokLive library not installed. Exit.")
        sys.exit(1)

    # Auto-fetch live username from server (zero config):
    #   SERVER_URL=http://[vps-host]:8888 python3 comment_forwarder.py
    username = USERNAME
    if not username:
        try:
            with urllib.request.urlopen(f"{SERVER_URL}/api/live/session-info", timeout=5) as resp:
                info = json.loads(resp.read().decode())
            username = (info.get("username") or "").strip()
            logger.info(f"Fetched live username from server: {username or '(trống)'}")
        except Exception as e:
            logger.error(f"Cannot fetch session from {SERVER_URL}/api/live/session-info: {e}")
            logger.info("Falling back to USERNAME env var.")
        if not username:
            logger.error("No live username found. Set USERNAME env or ensure server has tiktok_username in config.")
            sys.exit(1)

    username = username.strip().lstrip("@")
    kwargs = {}
    if WEB_PROXY:
        kwargs["web_proxy"] = WEB_PROXY
    if WS_PROXY:
        kwargs["ws_proxy"] = WS_PROXY

    logger.info(f"Connecting to live room: @{username} (proxy: {WEB_PROXY or 'none'})")
    try:
        client = TikTokLiveClient(unique_id=username, **kwargs)
    except Exception as e:
        logger.error(f"TikTokLiveClient init failed: {e}")
        sys.exit(1)

    last_forward = 0.0
    last_reply = 0.0
    REPLY_COOLDOWN = float(os.environ.get("REPLY_COOLDOWN", "5.0"))

    @client.on(CommentEvent)
    async def on_comment(event):
        nonlocal last_forward, last_reply
        now = time.time()
        if now - last_forward < FORWARD_COOLDOWN:
            return
        last_forward = now

        nickname = getattr(event.user, "nickname", "Viewer")
        comment_text = getattr(event, "comment", "") or getattr(event, "msg", "")
        if not comment_text:
            return
        logger.info(f"Got comment: @{nickname}: {comment_text}")

        result = forward_comment(nickname, str(comment_text))
        ai_response = result.get("ai_response") if isinstance(result, dict) else None
        if ai_response:
            # Reply the AI-generated response back onto TikTok comment panel
            if now - last_reply < REPLY_COOLDOWN:
                logger.info("Reply on cooldown, skipping")
                return
            last_reply = now
            try:
                await asyncio.wait_for(client.comment(ai_response), timeout=5)
                logger.info(f"Replied: {ai_response}")
            except Exception as e:
                logger.error(f"Reply failed: {e}")

    try:
        client.run()
    except KeyboardInterrupt:
        logger.info("Interrupted, stopping.")
    except Exception as e:
        logger.error(f"Listener error: {e}")
        time.sleep(3)
        logger.info("Retrying in 3s...")


if __name__ == "__main__":
    while True:
        try:
            start_listener()
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as e:
            logger.error(f"Loop error: {e}")
        time.sleep(3)
