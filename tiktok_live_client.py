#!/usr/bin/env python3
"""
TikTok Live WebSocket Client
Connects to TikTok live rooms and listens for real-time events (comments, gifts, viewers).
"""

import os
os.environ.setdefault("WHITELIST_AUTHENTICATED_SESSION_ID_HOST", "api.eulerstream.com")

import asyncio
import json
import time
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any, Callable
from collections import deque

try:
    from TikTokLive import TikTokLiveClient
    from TikTokLive.events.custom_events import FollowEvent
    from TikTokLive.events.proto_events import CommentEvent, GiftEvent, LikeEvent, RoomUserSeqEvent
    TIKTOK_LIVE_AVAILABLE = True
except ImportError:
    TIKTOK_LIVE_AVAILABLE = False

import logging

logger = logging.getLogger("TikTokLiveClient")

class TikTokLiveClientManager:
    """
    Manages connection to a TikTok live room via WebSocket.
    Collects comments, gifts, follows, likes, and viewer counts.
    Provides thread-safe access to recent events for the streaming engine.
    """

    MAX_RECENT_EVENTS = 100

    def __init__(self):
        self.client: Optional[TikTokLiveClient] = None
        self.room_id: Optional[str] = None
        self.username: Optional[str] = None
        self.is_connected: bool = False
        self.is_connecting: bool = False
        self.last_error: str = ""
        self.web_proxy: Optional[str] = None
        self.ws_proxy: Optional[str] = None
        self._session_id: Optional[str] = None
        self._tt_target_idc: Optional[str] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None
        self._lock = threading.Lock()

        # Event storage (thread-safe deques)
        self.comments: deque = deque(maxlen=self.MAX_RECENT_EVENTS)
        self.gifts: deque = deque(maxlen=self.MAX_RECENT_EVENTS)
        self.follows: deque = deque(maxlen=self.MAX_RECENT_EVENTS)
        self.likes: deque = deque(maxlen=self.MAX_RECENT_EVENTS)
        self.viewer_count: int = 0
        self.peak_viewers: int = 0

        # Callbacks
        self.on_comment_callbacks: List[Callable] = []
        self.on_gift_callbacks: List[Callable] = []
        self.on_connect_callbacks: List[Callable] = []
        self.on_disconnect_callbacks: List[Callable] = []
        self.on_viewer_join_callbacks: List[Callable] = []

        # Stats
        self.total_comments: int = 0
        self.total_gifts: int = 0
        self.total_follows: int = 0
        self.connect_start_time: Optional[float] = None

    def is_available(self) -> bool:
        """Check if TikTokLive library is installed."""
        return TIKTOK_LIVE_AVAILABLE

    def configure(self, username: str, web_proxy: str = None, ws_proxy: str = None,
                  session_id: str = None, tt_target_idc: str = None):
        """Configure the client with a TikTok username/handle and optional session."""
        with self._lock:
            username = username.strip().lstrip("@")
            self.username = username
            self.web_proxy = web_proxy
            self.ws_proxy = ws_proxy
            self._session_id = session_id
            self._tt_target_idc = tt_target_idc
            try:
                kwargs = {}
                if web_proxy:
                    kwargs["web_proxy"] = web_proxy
                if ws_proxy:
                    kwargs["ws_proxy"] = ws_proxy
                self.client = TikTokLiveClient(unique_id=username, **kwargs)
            except Exception as e:
                self.last_error = f"TikTokLiveClient init failed: {e}"
                logger.error(self.last_error)
                self.client = None
                return
            self._register_event_handlers()
            if session_id:
                self._apply_session(session_id, tt_target_idc)

    def _apply_session(self, session_id: str, tt_target_idc: str = None):
        """Apply TikTok session credentials to the underlying client for authed API calls.
        Clears duplicate tt-target-idc cookies to avoid 'Multiple cookies' error.
        NOTE: Called AFTER WebSocket connection is established (not during connect)."""
        try:
            _jar = getattr(self.client.web.cookies, "jar", None)
            if _jar is not None:
                for _c in list(_jar):
                    if getattr(_c, "name", "") == "tt-target-idc":
                        try:
                            _jar.clear(_c.domain, _c.path, _c.name)
                        except Exception:
                            pass

            # Set session cookies WITHOUT tt-target-idc first (avoids dup with TikTok responses)
            self.client.web.cookies.set("sessionid", session_id or "", ".tiktok.com")
            self.client.web.cookies.set("sessionid_ss", session_id or "", ".tiktok.com")
            self.client.web.cookies.set("sid_tt", session_id or "", ".tiktok.com")

            # Set a single canonical tt-target-idc cookie
            if tt_target_idc:
                if _jar is not None:
                    for _c in list(_jar):
                        if getattr(_c, "name", "") == "tt-target-idc":
                            try:
                                _jar.clear(_c.domain, _c.path, _c.name)
                            except Exception:
                                pass
                import http.cookiejar as _cbj
                _ck = _cbj.Cookie(
                    version=0, name="tt-target-idc", value=tt_target_idc,
                    port=None, port_specified=False,
                    domain=".tiktok.com", domain_specified=True, domain_initial_dot=True,
                    path="/", path_specified=True,
                    secure=True, expires=None,
                    discard=False, comment=None, comment_url=None,
                    rest={}, rfc2109=False,
                )
                if _jar is not None:
                    _jar.set_cookie(_ck)

            self.client.web.params['user_is_login'] = "true" if session_id else "false"
            logger.info(f"Session credentials applied (sid={session_id[:16]}..., tt_idc={tt_target_idc or 'none'})")
        except Exception as e:
            logger.warning(f"set_session failed: {e}")

    def _register_event_handlers(self):
        """Register handlers for TikTok live events."""
        if not self.client or not TIKTOK_LIVE_AVAILABLE:
            return

        @self.client.on(CommentEvent)
        async def on_comment(cmd):
            ts = datetime.now().strftime("%H:%M:%S")
            comment_data = {
                "timestamp": ts,
                "user": cmd.user.nickname,
                "comment": cmd.comment,
                "profile_pic": cmd.user.profile_picture.url if cmd.user.profile_picture else None,
                "is_verified": getattr(cmd.user, 'verified', False),
                "timestamp_unix": time.time()
            }
            with self._lock:
                self.comments.append(comment_data)
                self.total_comments += 1
                current_count = self.total_comments

            # Fire callbacks (render overlay/comment scroll).
            # NOTE: AI reply được thực hiện bởi comment_forwarder.py (hệ thống tương tác riêng biệt),
            # giữ nguyên thiết kế separation-of-concerns để dễ nâng cấp.
            for cb in self.on_comment_callbacks:
                try:
                    cb(comment_data, current_count)
                except Exception as e:
                    logger.error(f"Error in comment callback: {e}")

    def inject_comment(self, username: str, comment: str, trigger_ai: bool = True):
        """Inject an external comment (e.g. from proxy-forwarder) as if received live."""
        if not username or not comment:
            return False
        comment_data = {
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "user": username,
            "comment": comment,
            "profile_pic": None,
            "is_verified": False,
            "timestamp_unix": time.time()
        }
        with self._lock:
            self.comments.append(comment_data)
            self.total_comments += 1
            current_count = self.total_comments
        if trigger_ai:
            for cb in self.on_comment_callbacks:
                try:
                    cb(comment_data, current_count)
                except Exception as e:
                    logger.error(f"Error in comment callback: {e}")
        return True

        @self.client.on(GiftEvent)
        async def on_gift(cmd):
            gift_data = {
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "user": cmd.gift_user.nickname,
                "gift_name": cmd.gift.name,
                "gift_value": cmd.gift.value,
                "gift_count": cmd.gift.count,
                "diamonds": cmd.gift.value * cmd.gift.count,
                "timestamp_unix": time.time()
            }
            with self._lock:
                self.gifts.append(gift_data)
                self.total_gifts += 1
            for cb in self.on_gift_callbacks:
                try:
                    cb(gift_data, self.total_gifts)
                except Exception as e:
                    logger.error(f"Error in gift callback: {e}")

        @self.client.on(FollowEvent)
        async def on_follow(cmd):
            follow_data = {
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "user": cmd.follow_user.nickname,
                "timestamp_unix": time.time()
            }
            with self._lock:
                self.follows.append(follow_data)
                self.total_follows += 1
            logger.info(f"New follow from: {cmd.follow_user.nickname}")

        @self.client.on(LikeEvent)
        async def on_like(cmd):
            like_data = {
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "user": cmd.like_user.nickname,
                "total_likes": cmd.like_count,
                "timestamp_unix": time.time()
            }
            with self._lock:
                self.likes.append(like_data)
            logger.info(f"Like from: {cmd.like_user.nickname}")

        @self.client.on(RoomUserSeqEvent)
        async def on_viewer_count(cmd):
            join_callbacks = []
            with self._lock:
                vc = getattr(cmd, 'viewer_count', None) or getattr(cmd, 'user_count', None)
                if vc is not None:
                    prev = self.viewer_count
                    self.viewer_count = vc
                    if self.peak_viewers < vc:
                        self.peak_viewers = vc
                    if vc > prev:
                        join_callbacks = list(self.on_viewer_join_callbacks)
            for cb in join_callbacks:
                try:
                    cb(self.viewer_count)
                except Exception as e:
                    logger.error(f"Error in viewer join callback: {e}")

    def connect_async(self):
        """Start the async event loop in a background thread."""
        if not TIKTOK_LIVE_AVAILABLE:
            self.last_error = "TikTokLive library not installed. Run: pip install TikTokLive"
            logger.error(self.last_error)
            return False

        if not self.username:
            self.last_error = "Username not configured. Call configure(username) first."
            logger.error(self.last_error)
            return False

        if self.is_connecting or self.is_connected:
            return True

        self.is_connecting = True
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self._thread.start()

        # Wait for connection
        for _ in range(50):  # Wait up to 5 seconds
            if self.is_connected:
                for cb in self.on_connect_callbacks:
                    try:
                        cb()
                    except Exception as e:
                        logger.error(f"Error in connect callback: {e}")
                return True
            if self._stop_event and self._stop_event.is_set():
                return False
            time.sleep(0.1)

        return self.is_connected

    def _run_async_loop(self):
        """Run the asyncio event loop in a thread."""
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def _connect():
                try:
                    self.is_connecting = True
                    await self.client.start()
                    self.is_connected = True
                    self.connect_start_time = time.time()
                    logger.info(f"Connected to TikTok live: @{self.username}")
                except Exception as e:
                    self.last_error = str(e)
                    self.is_connected = False
                    logger.error(f"Failed to connect to TikTok live: {e}")

            async def _disconnect():
                if self.client:
                    await self.client.stop()
                self.is_connected = False
                logger.info("Disconnected from TikTok live")

            def check_disconnect():
                if self._stop_event and self._stop_event.is_set():
                    asyncio.run_coroutine_threadsafe(_disconnect(), self._loop)
                    self._loop.stop()

            self._loop.create_task(_connect())
            self._loop.call_later(0.5, check_disconnect)
            self._loop.run_forever()
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"Async loop error: {e}")
        finally:
            self.is_connecting = False
            self.is_connected = False
            for cb in self.on_disconnect_callbacks:
                try:
                    cb()
                except Exception as e:
                    logger.error(f"Error in disconnect callback: {e}")

    def disconnect(self):
        """Disconnect from TikTok live room."""
        if self._stop_event:
            self._stop_event.set()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self.is_connected = False
        self.is_connecting = False
        logger.info("TikTok live client disconnected")

    def reconnect(self):
        """Reconnect to TikTok live."""
        self.disconnect()
        time.sleep(1)
        return self.connect_async()

    def get_recent_comments(self, count: int = 10) -> List[Dict[str, Any]]:
        """Get the most recent comments."""
        with self._lock:
            return list(self.comments)[-count:]

    def get_recent_gifts(self, count: int = 10) -> List[Dict[str, Any]]:
        """Get the most recent gifts."""
        with self._lock:
            return list(self.gifts)[-count:]

    def get_telemetry(self) -> Dict[str, Any]:
        """Get current telemetry status."""
        with self._lock:
            uptime = 0
            if self.is_connected and self.connect_start_time:
                uptime = int(time.time() - self.connect_start_time)

            return {
                "connected": self.is_connected,
                "connecting": self.is_connecting,
                "username": self.username,
                "viewer_count": self.viewer_count,
                "peak_viewers": self.peak_viewers,
                "uptime_seconds": uptime,
                "total_comments": self.total_comments,
                "total_gifts": self.total_gifts,
                "total_follows": self.total_follows,
                "last_error": self.last_error,
                "recent_comments": list(self.comments)[-10:],
                "recent_gifts": list(self.gifts)[-5:],
            }


# Global instance
live_client = TikTokLiveClientManager()
