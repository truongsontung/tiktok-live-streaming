#!/usr/bin/env python3
"""
TikTok Live Comment Relay (optional — for clean IP/proxy scenarios).

When the main server's IP is rate-limited or blocked by TikTok, running this
script on a separate clean machine/IP relays live comments to the server's
overlay renderer via HTTP:

  Comment → TikTokLive WebSocket → POST /api/overlay/comment (server) → FFmpeg drawtext

The server handles AI responses + overlay rendering natively.
This relay only forwards viewer comments + welcome messages.

Env:
  SERVER_URL : https://freeforyou.win/tiktok_live (nginx HTTPS + basic auth)
  BASIC_AUTH : "admin:tiktok99"
  USERNAME   : override (skip auto-fetch from server)
  WEB_PROXY  : HTTP proxy for TikTok web (if 403 on connect)
  WS_PROXY   : SOCKS5 proxy for TikTok WebSocket
"""

import os
import ssl
import json
import time
import sys
import base64
import logging
import urllib.request

os.environ.setdefault("WHITELIST_AUTHENTICATED_SESSION_ID_HOST", "api.eulerstream.com")
logging.basicConfig(
    level=logging.WARNING,
    format="[%(asctime)s] [Forwarder] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("Forwarder")

SERVER_URL = os.environ.get("SERVER_URL", "https://freeforyou.win/tiktok_live").rstrip("/")
BASIC_AUTH = os.environ.get("BASIC_AUTH") or None
USERNAME = os.environ.get("USERNAME", "").strip() or None
WEB_PROXY = os.environ.get("WEB_PROXY") or None
WS_PROXY = os.environ.get("WS_PROXY") or None
FORWARD_COOLDOWN = float(os.environ.get("FORWARD_COOLDOWN", "1.0"))

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def _make_headers(extra=None):
    h = {"Content-Type": "application/json"}
    if BASIC_AUTH:
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


def _send_overlay_comment(username: str, comment: str, is_ai: bool = False):
    """Forward a comment/AI response to the server's overlay renderer."""
    try:
        code, data = _fetch_json(
            f"{SERVER_URL}/api/overlay/comment",
            data={"username": username, "comment": comment, "is_ai_response": is_ai},
        )
        if code == 200:
            logger.info(f"Overlay: {username}: {comment[:60]}")
        else:
            logger.warning(f"Overlay post failed (code {code}): {str(data)[:120]}")
    except Exception as e:
        logger.warning(f"Overlay post error: {e}")


def start_listener():
    """Connect to TikTok live and relay comments to server overlay."""
    try:
        from TikTokLive import TikTokLiveClient
        from TikTokLive.events.proto_events import CommentEvent
        import asyncio
    except ImportError:
        logger.error("TikTokLive not installed. pip install TikTokLive")
        sys.exit(1)

    # Fetch username + session from server (zero config)
    api_secret = None
    try:
        code, cfg = _fetch_json(f"{SERVER_URL}/api/config")
        if code == 200 and cfg.get("api_key_secret"):
            api_secret = cfg["api_key_secret"]
    except Exception as e:
        logger.error(f"Cannot fetch config from server: {e}")

    username = USERNAME
    if not username:
        try:
            code, info = _fetch_json(f"{SERVER_URL}/api/live/session-info")
            username = (info.get("username") or "").strip() if code == 200 and info else ""
            logger.info(f"Fetched live username from server: {username or '(empty)'}")
        except Exception as e:
            logger.error(f"Cannot fetch session from server: {e}")
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

    logger.info(f"Connecting to live room: @{username} (proxy: {WEB_PROXY or WS_PROXY or 'none'})")

    try:
        client = TikTokLiveClient(unique_id=username, **kwargs)

        # Apply session credentials from server for authenticated operations
        try:
            code, info = _fetch_json(f"{SERVER_URL}/api/live/session-info")
            _sid = (info.get("tiktok_session") or "").strip() if code == 200 and info else ""
            _tt = (info.get("tiktok_tt_target_idc") or "").strip() if code == 200 and info else ""
            if _sid:
                if not _tt:
                    try:
                        for _c in list(getattr(client.web.cookies, "jar", []) or []):
                            if getattr(_c, "name", "") == "tt-target-idc" and getattr(_c, "value", ""):
                                _tt = _c.value
                                break
                    except Exception:
                        pass
                # Clear duplicate tt-target-idc cookies to avoid conflict
                try:
                    _jar = getattr(client.web.cookies, "jar", None)
                    if _jar is not None:
                        for _c in list(_jar):
                            if getattr(_c, "name", "") == "tt-target-idc":
                                try:
                                    _jar.clear(_c.domain, _c.path, _c.name)
                                except Exception:
                                    pass
                except Exception:
                    pass

                client.web.set_session(_sid, _tt or None)
                logger.info(f"Session configured (sid={_sid[:16]}..., tt_idc={_tt or 'none'})")
        except Exception as e:
            logger.warning(f"set_session failed: {e}")

        last_forward = 0.0

        @client.on(CommentEvent)
        async def on_comment(cmd):
            nonlocal last_forward
            now = time.time()
            if now - last_forward < FORWARD_COOLDOWN:
                return
            last_forward = now

            nickname = getattr(cmd.user, "nickname", "Viewer")
            comment_text = getattr(cmd, "comment", "") or getattr(cmd, "msg", "")
            if not comment_text:
                return
            logger.info(f"Got comment: @{nickname}: {comment_text}")
            _send_overlay_comment(nickname, str(comment_text), is_ai=False)

        @client.on_join
        async def on_join(cmd):
            # Fired when the client joins the room (initial viewers)
            viewer_count = getattr(cmd, 'viewer_count', None) or getattr(cmd, 'user_count', None) or 0

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
