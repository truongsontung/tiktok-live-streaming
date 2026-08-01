#!/usr/bin/env python3
"""
External TikTok Live Comment Forwarder + Auto-Reply.

Chay tren may local / IP sach / proxy de lang nghe comment thoi:
  - Tu-f dong username va api_key_secret tu server (zero config)
  - Forward comment -> server AI -> nhan ai_response + action
  - Reply len comment panel (send_room_chat) hoac pin comment len dan top

Env:
  SERVER_URL    : https://freeforyou.win/tiktok_live (default)
  BASIC_AUTH    : "admin:tiktok99"  (neu qua nginx co auth)
  USERNAME      : override (skip auto-fetch)
  WEB_PROXY     : ws proxy HTTP (neu 403 webcast)
  COMMENT_COOLDOWN, REPLY_COOLDOWN ...
"""

# ---- Shopping cart config (hardcoded product links) ----
PRODUCTS = {
    "áo thun": {
        "link": "https://freeforyou.win/shop/ao-thun",
        "price": "200k",
    },
    "size m": {
        "link": "https://freeforyou.win/shop/ao-thun?variant=M",
        "price": "200k",
    },
    "giỏ hàng": {
        "link": "https://freeforyou.win/shop/cart",
        "price": None,
    },
}


import os
# Whitelist the EulerStream sign-server host BEFORE TikTokLive is imported,
# so TikTokLive's check_authenticated_session() allows authenticated session.
os.environ.setdefault("WHITELIST_AUTHENTICATED_SESSION_ID_HOST", "api.eulerstream.com")
import sys
import time
import json
import asyncio
import logging
import urllib.request

logging.basicConfig(
    level=logging.WARNING,
    format="[%(asctime)s] [Forwarder] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("Forwarder")

# Server URL — qua nginx HTTPS proxy (server bind 127.0.0.1, không expose port 8888 trực tiếp)
#   SERVER_URL=https://freeforyou.win/tiktok_live   (cần nginx basic_auth)
SERVER_URL = os.environ.get("SERVER_URL", "https://freeforyou.win/tiktok_live").rstrip("/")
# Basic auth cho nginx proxy (format: "admin:tiktok99")
BASIC_AUTH = os.environ.get("BASIC_AUTH") or None
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


import ssl
# Allow self-signed certificates on HTTPS proxies (dev)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

def _make_headers(extra=None):
    h = {"Content-Type": "application/json"}
    if BASIC_AUTH:
        import base64
        h["Authorization"] = "Basic " + base64.b64encode(BASIC_AUTH.encode()).decode()
    if extra:
        h.update(extra)
    return h

def _fetch_json(url, data=None):
    """Fetch JSON from server via nginx proxy (HTTPS + basic auth)."""
    payload = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=payload, headers=_make_headers(), method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=8, context=SSL_CTX) as resp:
            return resp.status, json.loads(resp.read().decode())
    except Exception as e:
        logger.error(f"Fetch {url} failed: {e}")
        return None, {}

def fetch_api_secret():
    """Lay api_key_secret tu GET /api/config (gui X-API-Key cho POST comment-forward)."""
    code, data = _fetch_json(f"{SERVER_URL}/api/config")
    if code == 200 and data.get("api_key_secret"):
        return data["api_key_secret"]
    return None


def _extract_cart_link(comment: str, ai_response: str) -> str:
    """Scan comment/reply de tim tu khoa san pham -> tra ve link gio hang."""
    combined = f"{comment} {ai_response}".lower()
    for keyword, info in PRODUCTS.items():
        if keyword in combined:
            return info["link"]
    return None


def fetch_active_media():
    """Lay video dang live + product_tag/cart_link tu GET /api/live/active-media."""
    code, info = _fetch_json(f"{SERVER_URL}/api/live/active-media")
    if code == 200 and info:
        return info
    return {}


def _fetch_tt_target_idc_from_tiktok(session_id: str) -> str:
    """Fallback: extract tt-target-idc cookie from TikTok webcast API response."""
    if not session_id:
        return ""
    try:
        cookie_str = f"sessionid={session_id}; sessionid_ss={session_id}; sid_tt={session_id}; sid_api={session_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Cookie": cookie_str,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://www.tiktok.com/",
            "Origin": "https://www.tiktok.com",
        }
        post_data = "device_platform=web&aid=1988&app_language=en&app_name=tiktok_web&browser_name=Mozilla&browser_version=5.0&channel=googleios&api_service_version=2&live_type=1&stream_type=push&mode=web&quality=normal"
        req = urllib.request.Request(
            "https://webcast.tiktok.com/webcast/room/create/",
            data=post_data.encode(), headers=headers,
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            set_cookies = resp.headers.get_all("Set-Cookie") or []
            for cookie in set_cookies:
                for part in cookie.split(";"):
                    part = part.strip()
                    if part.startswith("tt-target-idc=") and len(part) > len("tt-target-idc="):
                        return part.split("=", 1)[1].strip()
    except Exception as e:
        logger.warning(f"tt-target-idc fallback extract failed: {e}")
    return ""


def forward_comment(username: str, comment: str, api_secret: str, product_tag: str = None) -> dict:
    payload = {"username": username, "comment": comment}
    if product_tag:
        payload["product_tag"] = product_tag
    headers = {"Content-Type": "application/json"}
    if api_secret:
        headers["X-API-Key"] = api_secret
    if BASIC_AUTH:
        import base64
        headers["Authorization"] = "Basic " + base64.b64encode(BASIC_AUTH.encode()).decode()
    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"{SERVER_URL}/api/live/comment-forward", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8, context=SSL_CTX) as resp:
            if resp.status != 200:
                logger.warning(f"Server returned HTTP {resp.status}")
                return {}
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.error(f"Forward failed to {SERVER_URL}/api/live/comment-forward: {e}")
        return {}


def start_listener():
    if not TIKTOK_AVAILABLE:
        logger.error("TikTokLive library not installed. Exit.")
        sys.exit(1)

    # Auto-fetch live username + api_key_secret from server (zero config).
    # SERVER_URL via nginx HTTPS proxy, dùng BASIC_AUTH env.
    api_secret = None
    try:
        code, cfg = _fetch_json(f"{SERVER_URL}/api/config")
        if code == 200 and cfg.get("api_key_secret"):
            api_secret = cfg["api_key_secret"]
    except Exception as e:
        logger.error(f"Cannot fetch config from {SERVER_URL}/api/config: {e}")

    username = USERNAME
    if not username:
        try:
            code2, info = _fetch_json(f"{SERVER_URL}/api/live/session-info")
            username = (info.get("username") or "").strip()
            logger.info(f"Fetched live username from server: {username or '(empty)'}")
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
        # Configure session cookie tu server -> de send_room_chat reply duoc xac thuc
        try:
            _sc, _si = _fetch_json(f"{SERVER_URL}/api/live/session-info")
            _sid = ((_si or {}).get("tiktok_session") or "").strip()
            _tt = ((_si or {}).get("tiktok_tt_target_idc") or "").strip()
            if _sid:
                # Nếu server chưa có tt-target-idc, tự trích xuất từ API TikTok
                if not _tt:
                    _tt = _fetch_tt_target_idc_from_tiktok(_sid)
                    logger.info(f"tt-target-idc extracted locally: {_tt or '(failed)'}")
                client.web.set_session(_sid, _tt or None)
                logger.info(f"Session configured for send_room_chat (sid={_sid[:16]}..., tt_idc={_tt or 'none'})")
        except Exception as e:
            logger.warning(f"set_session failed (reply co the loi): {e}")
    except Exception as e:
        logger.error(f"TikTokLiveClient init failed: {e}")
        sys.exit(1)

    last_forward = 0.0
    last_reply = 0.0
    REPLY_COOLDOWN = float(os.environ.get("REPLY_COOLDOWN", "5.0"))

    # Fetch active video product_tag (AI trả lời theo sản phẩm đang live)
    active_media = fetch_active_media()
    active_tag = (active_media.get("product_tag") or "").strip()
    active_url = active_media.get("product_url") or active_media.get("cart_link")
    logger.info(f"Active media product_tag: {active_tag or '(none)'} | product_url: {active_url or '(none)'}")

    # Auto-add san pham dang active (co product_id + shop_id) vao Showcase Creator
    if active_media.get("product_id") and active_media.get("shop_id"):
        _fname = active_media.get("filename") or ""
        _r = _fetch_json(f"{SERVER_URL}/api/tiktok/showcase/add?filename={_fname}")
        logger.warning(f"Showcase add triggered for {_fname}: {_r}")

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

        result = forward_comment(nickname, str(comment_text), api_secret, product_tag=active_tag or None)
        ai_response = result.get("ai_response") if isinstance(result, dict) else None
        action = result.get("action") if isinstance(result, dict) else None
        if ai_response:
            if now - last_reply < REPLY_COOLDOWN:
                logger.info("Reply on cooldown, skipping")
                return
            last_reply = now
            # Không bao gio dan URL chu (vi pham Community Guidelines).
            # Relay: nhan duoc ai_response tu server; comment_forwarder chi la CTA tu nhien,
            # voi huong dan nguoi dung an nut gio hang mau vang native tren video.
            cart_link = ""  # luon trong so voi tranh vi pham
            reply_text = ai_response
            reply_text = ai_response if not cart_link else f"{ai_response}\nMua ngay: {cart_link}"
            try:
                await asyncio.wait_for(client.send_room_chat(reply_text), timeout=5)
                logger.warning(f"Replied (cart): {reply_text}")
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
