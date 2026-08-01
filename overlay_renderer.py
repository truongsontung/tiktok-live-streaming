#!/usr/bin/env python3
"""
Dynamic Overlay Renderer
Generates text files for FFmpeg drawtext filter to display dynamic content on stream:
- Latest comments and AI responses
- Viewer count and stream stats
- Custom messages
"""

import os
import json
import time
import threading
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from collections import deque

logger = logging.getLogger("OverlayRenderer")

OVERLAY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlays")
os.makedirs(OVERLAY_DIR, exist_ok=True)

class OverlayRenderer:
    """
    Renders dynamic text overlays for the streaming engine.
    Writes text to files that FFmpeg's drawtext filter reads in real-time.
    """

    def __init__(self):
        self.enabled: bool = False
        self.enabled_overlays = {
            "clock": False,
            "comment_scroll": False,
            "ai_response": False,
            "viewer_count": False,
            "stats_panel": False,
        }
        self.max_comment_lines: int = 6
        self.comment_scroll_speed: float = 30.0  # pixels per second
        
        # Active overlay text
        self.current_overlay_text: str = ""
        self.clock_text: str = ""
        
        # Comment display data
        self.comment_queue: deque = deque(maxlen=20)
        self.active_comments: deque = deque(maxlen=10)
        
        # File paths for drawtext
        self.text_files: Dict[str, str] = {
            "clock": os.path.join(OVERLAY_DIR, "clock.txt"),
            "comment": os.path.join(OVERLAY_DIR, "comment.txt"),
            "ai_response": os.path.join(OVERLAY_DIR, "ai_response.txt"),
            "stats": os.path.join(OVERLAY_DIR, "stats.txt"),
            "title": os.path.join(OVERLAY_DIR, "title.txt"),
        }
        
        self._lock = threading.Lock()
        self._last_update: float = 0
        self._update_interval: float = 0.5  # Update overlays every 0.5s
        
        # Stats tracking
        self.stream_start_time: Optional[float] = None
        self.total_comments_displayed: int = 0
        self.total_responses_displayed: int = 0

        # Initialize overlay text files with default content
        self._init_overlay_files()

    def _init_overlay_files(self):
        """Create overlay text files with default content so FFmpeg can find them."""
        defaults = {
            "clock": "00:00:00",
            "comment": "",
            "ai_response": "",
            "stats": "Viewers: 0 | Peak: 0",
            "title": "TikTok Live Stream",
        }
        for name, content in defaults.items():
            filepath = self.text_files.get(name)
            if filepath:
                try:
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.write(content)
                except Exception as e:
                    logger.debug(f"Could not create overlay file {name}: {e}")

    def set_enabled(self, enabled: bool):
        """Enable or disable overlay rendering."""
        with self._lock:
            self.enabled = enabled
        logger.info(f"Overlay renderer {'enabled' if enabled else 'disabled'}")

    def configure_overlays(self, overlay_config: Dict[str, bool]):
        """Configure which overlays are enabled."""
        with self._lock:
            for key, value in overlay_config.items():
                if key in self.enabled_overlays:
                    self.enabled_overlays[key] = value

    def set_overlay_text(self, text: str):
        """Set the main overlay/title text."""
        with self._lock:
            self.current_overlay_text = text[:100]  # Limit length
        self._write_text_file("title", self.current_overlay_text)

    def set_main_text(self, text: str):
        """Alias for set_overlay_text."""
        self.set_overlay_text(text)

    def add_comment(self, username: str, comment: str, is_ai_response: bool = False):
        """Add a comment or AI response to the display queue."""
        ts = datetime.now().strftime("%H:%M:%S")
        entry = {
            "timestamp": ts,
            "username": username,
            "comment": comment,
            "is_ai_response": is_ai_response,
            "age": 0,
            "ttl": 15.0,  # Show for 15 seconds
        }
        
        with self._lock:
            self.active_comments.append(entry)
            if is_ai_response:
                self.total_responses_displayed += 1
            else:
                self.total_comments_displayed += 1
        
        self._update_files()

    def set_viewer_count(self, count: int, peak: int = 0):
        """Update viewer count display."""
        self._write_text_file("stats", f"Viewers: {count} | Peak: {peak}")

    def set_stream_start(self, start_time: float):
        """Set the stream start time for uptime calculation."""
        self.stream_start_time = start_time

    def _write_text_file(self, name: str, content: str):
        """Write content to a text file for FFmpeg drawtext."""
        filepath = self.text_files.get(name)
        if not filepath:
            return
        
        # Truncate content to fit overlay
        max_len = 80
        lines = content.split("\n")
        processed_lines = []
        for line in lines:
            if len(line) > max_len:
                processed_lines.append(line[:max_len])
            else:
                processed_lines.append(line)
        processed_content = "\n".join(processed_lines)
        
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(processed_content)
        except Exception as e:
            logger.debug(f"Error writing overlay file {name}: {e}")

    def _build_comment_display(self) -> str:
        """Build the comment display text."""
        if not self.active_comments:
            return ""
        
        lines = []
        current_time = time.time()
        
        # Remove expired comments
        with self._lock:
            expired = []
            for i, comment in enumerate(self.active_comments):
                comment["age"] = current_time - comment.get("start_time", current_time)
                if comment["age"] > comment["ttl"]:
                    expired.append(i)
            
            # Remove expired (in reverse order to maintain indices)
            for i in reversed(expired):
                del self.active_comments[i]
        
        # Build display lines
        with self._lock:
            for comment in list(self.active_comments)[-self.max_comment_lines:]:
                prefix = "🤖 " if comment.get("is_ai_response") else "🗨️ "
                username = comment["username"][:15]
                text = comment["comment"][:60]
                lines.append(f"{prefix}{username}: {text}")
        
        return "\n".join(lines)

    def _build_ai_response_display(self) -> str:
        """Build AI response display text."""
        with self._lock:
            for comment in reversed(list(self.active_comments)):
                if comment.get("is_ai_response"):
                    text = comment["comment"][:80]
                    return text
        return ""

    def _build_stats_display(self, stats: Optional[Dict[str, Any]] = None) -> str:
        """Build stats panel text."""
        lines = []
        
        if self.stream_start_time:
            uptime = int(time.time() - self.stream_start_time)
            hours, remainder = divmod(uptime, 3600)
            minutes, seconds = divmod(remainder, 60)
            lines.append(f"⏱️ Uptime: {hours:02d}:{minutes:02d}:{seconds:02d}")
        
        lines.append(f"💬 Comments: {self.total_comments_displayed}")
        lines.append(f"🤖 AI Responses: {self.total_responses_displayed}")
        
        if stats:
            if "viewer_count" in stats:
                lines.append(f"👀 Viewers: {stats['viewer_count']}")
            if "status" in stats:
                lines.append(f"📡 Status: {stats['status']}")
        
        return "\n".join(lines)

    def _update_clock(self):
        """Update clock display."""
        now = datetime.now()
        time_str = now.strftime("%H:%M:%S")
        date_str = now.strftime("%d/%m/%Y")
        
        if self.enabled_overlays["clock"]:
            self._write_text_file("clock", time_str)
        
        self.clock_text = time_str

    def _update_files(self):
        """Update all overlay text files."""
        current_time = time.time()
        if current_time - self._last_update < self._update_interval:
            return
        self._last_update = current_time
        
        if not self.enabled:
            return
        
        # Update clock
        self._update_clock()
        
        # Update comments
        if self.enabled_overlays["comment_scroll"]:
            comment_text = self._build_comment_display()
            self._write_text_file("comment", comment_text)
        
        # Update AI responses
        if self.enabled_overlays["ai_response"]:
            ai_text = self._build_ai_response_display()
            self._write_text_file("ai_response", ai_text)
        
        # Update title
        if self.current_overlay_text:
            self._write_text_file("title", self.current_overlay_text)

    def update_from_clients(self, live_telemetry: Optional[Dict] = None, engine_telemetry: Optional[Dict] = None):
        """Update overlay from live client and stream engine telemetry."""
        stats = {}
        if live_telemetry:
            stats["viewer_count"] = live_telemetry.get("viewer_count", 0)
            stats["status"] = live_telemetry.get("connected", False) and "LIVE" or "DISCONNECTED"
        if engine_telemetry:
            stats["status"] = engine_telemetry.get("status", stats.get("status", "UNKNOWN"))
        
        self.set_viewer_count(
            live_telemetry.get("viewer_count", 0) if live_telemetry else 0,
            live_telemetry.get("peak_viewers", 0) if live_telemetry else 0
        )
        
        stats_text = self._build_stats_display(stats)
        self._write_text_file("stats", stats_text)
        
        self._update_files()

    def get_ffmpeg_drawtext_args(self, config: Dict[str, Any]) -> List[str]:
        """
        Generate FFmpeg drawtext arguments for the overlay.
        Returns a list of filter arguments.
        """
        args = []
        
        # Base overlay text (can be static or dynamic from file)
        if config.get("overlay_text"):
            text = config["overlay_text"].replace(":", "\\:").replace("'", "\\'")
            args.append(f"drawtext=text='{text}':x=(w-tw)/2:y=30:fontsize=36:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=10:fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
        
        # Clock (dynamic from file)
        if self.enabled and config.get("show_clock", False):
            args.append(f"drawtext=textfile={self.text_files['clock']}:x=(w-tw)/2:y=w/4:fontsize=48:fontcolor=yellow:box=1:boxcolor=black@0.6:boxborderw=10:reload=1:fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
        
        # Dynamic comment overlay
        if self.enabled and self.enabled_overlays.get("comment_scroll", False):
            args.append(f"drawtext=textfile={self.text_files['comment']}:x=20:y=h-th-120:fontsize=24:fontcolor=white:box=1:boxcolor=black@0.5:boxborderw=5:reload=1:line_spacing=10:fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        
        # AI response overlay
        if self.enabled and self.enabled_overlays.get("ai_response", False):
            args.append(f"drawtext=textfile={self.text_files['ai_response']}:x=(w-tw)/2:y=h/2:fontsize=28:fontcolor=cyan:box=1:boxcolor=black@0.6:boxborderw=8:reload=1:fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
        
        # Stats panel
        if self.enabled and self.enabled_overlays.get("stats_panel", False):
            args.append(f"drawtext=textfile={self.text_files['stats']}:x=20:y=80:fontsize=20:fontcolor=white:box=1:boxcolor=black@0.5:boxborderw=5:reload=1:line_spacing=8:fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        
        # Title overlay
        if self.enabled and self.current_overlay_text:
            args.append(f"drawtext=textfile={self.text_files['title']}:x=(w-tw)/2:y=10:fontsize=28:fontcolor=white:box=1:boxcolor=royalblue@0.6:boxborderw=10:reload=1:fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
        
        return args

    def get_telemetry(self) -> Dict[str, Any]:
        """Get overlay renderer telemetry."""
        with self._lock:
            return {
                "enabled": self.enabled,
                "enabled_overlays": self.enabled_overlays,
                "active_comments": len(self.active_comments),
                "total_comments_displayed": self.total_comments_displayed,
                "total_responses_displayed": self.total_responses_displayed,
                "current_overlay_text": self.current_overlay_text,
                "clock_text": self.clock_text,
                "overlay_files": list(self.text_files.keys()),
            }


# Global instance
overlay_renderer = OverlayRenderer()
