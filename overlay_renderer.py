#!/usr/bin/env python3
"""
Dynamic Overlay Renderer
Generates text files for FFmpeg drawtext filter to display dynamic content on stream:
- Latest comments and AI responses
- Viewer count and stream stats
- Custom messages
"""

import os
import time
import math
import io
import re
import errno
import random
import threading
import logging
import fcntl
from typing import Optional, List, Dict, Any

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("OverlayRenderer")

OVERLAY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlays")
os.makedirs(OVERLAY_DIR, exist_ok=True)

AVATAR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "avatars")
os.makedirs(AVATAR_DIR, exist_ok=True)

ZODIAC_ANIMALS = [
    {"num": 1,  "eng": "rat",     "zodiac": "Tý",   "animal": "Chuột"},
    {"num": 2,  "eng": "ox",      "zodiac": "Sửu",  "animal": "Trâu"},
    {"num": 3,  "eng": "tiger",   "zodiac": "Dần",  "animal": "Hổ"},
    {"num": 4,  "eng": "cat",     "zodiac": "Mão",  "animal": "Mèo"},
    {"num": 5,  "eng": "dragon",  "zodiac": "Thìn", "animal": "Rồng"},
    {"num": 6,  "eng": "snake",   "zodiac": "Tỵ",   "animal": "Rắn"},
    {"num": 7,  "eng": "horse",   "zodiac": "Ngọ",  "animal": "Ngựa"},
    {"num": 8,  "eng": "goat",    "zodiac": "Mùi",  "animal": "Dê"},
    {"num": 9,  "eng": "monkey",  "zodiac": "Thân", "animal": "Khỉ"},
    {"num": 10, "eng": "rooster", "zodiac": "Dậu",  "animal": "Gà"},
    {"num": 11, "eng": "dog",     "zodiac": "Tuất", "animal": "Chó"},
    {"num": 12, "eng": "pig",     "zodiac": "Hỏi",  "animal": "Heo"},
]

ZODIAC_CATEGORIES = ["trung-quoc", "chien-binh", "hoang-gia", "cute-2023"]

class OverlayRenderer:
    """
    Renders dynamic text overlays for the streaming engine.
    Writes text to files that FFmpeg's drawtext filter reads in real-time.
    """

    def __init__(self):
        self.enabled: bool = False
        self.enabled_overlays = {
            "avatar_overlay": False,
        }
        
        self.text_files: Dict[str, str] = {
            "title": os.path.join(OVERLAY_DIR, "title.txt"),
        }
        
        self._lock = threading.RLock()
        
        # Avatar overlay (3D cartoon characters on stream)
        self.active_avatars: Dict[str, dict] = {}
        self.avatar_pool: List[str] = []
        self.zodiac_pool: List[dict] = []
        self._avatar_display_size: int = 100
        self._avatar_cache: Dict[str, Image.Image] = {}
        self._font_cache: Dict[str, Any] = {}
        self._avatar_fall_duration: float = 2.0
        self._avatar_target_height_ratio: float = 0.66

        # FIFO overlay pipe (feeds PNG frames to FFmpeg)
        self._overlay_fifo_path: Optional[str] = None
        self._overlay_thread: Optional[threading.Thread] = None
        self._overlay_stop_event: Optional[threading.Event] = None
        self._overlay_fps: int = 10
        self._overlay_width: int = 0
        self._overlay_height: int = 0

        self._init_overlay_files()

    def _init_overlay_files(self):
        """Create overlay text files with default content so FFmpeg can find them."""
        defaults = {
            "title": "",
        }
        for name, content in defaults.items():
            filepath = self.text_files.get(name)
            if filepath:
                try:
                    tmp_path = filepath + ".tmp"
                    with open(tmp_path, "w", encoding="utf-8") as f:
                        f.write(content)
                    os.replace(tmp_path, filepath)
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
        """No-op: text overlay is disabled (only avatar overlay is used)."""
        pass

    def set_main_text(self, text: str):
        """Alias for set_overlay_text."""
        self.set_overlay_text(text)

    def set_enabled_overlays(self, enabled: bool):
        """Enable or disable all overlay types at once."""
        with self._lock:
            for k in self.enabled_overlays:
                self.enabled_overlays[k] = enabled

    def add_welcome_message(self, username: str):
        """No-op: welcome message overlay is disabled."""
        pass

    def add_comment(self, username: str, comment: str, is_ai_response: bool = False, ttl: float = 15.0):
        """No-op: comment overlay is disabled."""
        pass

    def set_viewer_count(self, count: int, peak: int = 0):
        """No-op: viewer count is not displayed (only avatar overlay is used)."""
        pass

    def set_stream_start(self, start_time: float):
        """No-op: stream start time is not used (no uptime display on video)."""
        pass

    def get_ffmpeg_drawtext_args(self, config: Dict[str, Any]) -> List[str]:
        """Generate FFmpeg drawtext arguments. Returns empty list — no text on video."""
        return []

    def get_telemetry(self) -> Dict[str, Any]:
        """Get overlay renderer telemetry."""
        with self._lock:
            return {
                "enabled": self.enabled,
                "enabled_overlays": self.enabled_overlays,
                "active_avatars": len(self.active_avatars),
                "avatar_pool_count": len(self.avatar_pool),
                "zodiac_pool_count": len(self.zodiac_pool),
            }

    # ==================== AVATAR OVERLAY SYSTEM ====================

    def load_avatar_pool(self, directory: str = None):
        """Load avatar images from a directory for random assignment."""
        if directory is None:
            directory = AVATAR_DIR
        with self._lock:
            self.avatar_pool = []
            self.zodiac_pool = []
            if os.path.exists(directory):
                for f in sorted(os.listdir(directory)):
                    if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                        fpath = os.path.join(directory, f)
                        self.avatar_pool.append(fpath)
                        # Check if it's a zodiac image: zodiac_{category}_{num}_{animal}.png
                        m = re.match(r'zodiac_([\w-]+)_(\d+)_(.+)\.png', f)
                        if m:
                            cat, num_str, animal = m.groups()
                            num = int(num_str)
                            zodiac_info = next((z for z in ZODIAC_ANIMALS if z["num"] == num), None)
                            if zodiac_info:
                                self.zodiac_pool.append({
                                    "path": fpath,
                                    "category": cat,
                                    "zodiac": zodiac_info["zodiac"],
                                    "animal": zodiac_info["animal"],
                                    "num": num,
                                })
        logger.info(f"Loaded {len(self.avatar_pool)} avatars ({len(self.zodiac_pool)} zodiac) from {directory}")

    def _assign_random_zodiac(self) -> Optional[dict]:
        """Randomly select a zodiac animal from the pool."""
        if not self.zodiac_pool:
            return None
        with self._lock:
            return random.choice(self.zodiac_pool)

    def _get_avatar_image(self, avatar_path: str) -> Image.Image:
        """Load and cache avatar image, resized to display size with aspect ratio preserved."""
        if avatar_path in self._avatar_cache:
            return self._avatar_cache[avatar_path]
        try:
            img = Image.open(avatar_path).convert('RGBA')
            img.thumbnail((self._avatar_display_size, self._avatar_display_size), Image.LANCZOS)
            canvas = Image.new('RGBA', (self._avatar_display_size, self._avatar_display_size), (0, 0, 0, 0))
            canvas.paste(img, ((self._avatar_display_size - img.width) // 2,
                              (self._avatar_display_size - img.height) // 2), img)
            self._avatar_cache[avatar_path] = canvas
            return canvas
        except Exception as e:
            logger.warning(f"Failed to load avatar {avatar_path}: {e}")
            return self._generate_default_avatar(avatar_path)

    def _generate_default_avatar(self, username: str = "U") -> Image.Image:
        """Generate a default avatar: colored circle with initials."""
        img = Image.new('RGBA', (self._avatar_display_size, self._avatar_display_size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        colors = [(255, 100, 100), (100, 255, 100), (100, 100, 255),
                  (255, 215, 0), (255, 100, 255), (100, 255, 255), (255, 165, 0)]
        color = random.choice(colors)
        draw.ellipse([0, 0, self._avatar_display_size, self._avatar_display_size], fill=color)
        initials = username[:2].upper() if username and username != "U" else "TV"
        try:
            font = self._get_font("bold", 42)
            bbox = draw.textbbox((0, 0), initials, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            draw.text((self._avatar_display_size // 2 - tw // 2,
                      self._avatar_display_size // 2 - th // 2 - 5),
                     initials, fill=(255, 255, 255, 230), font=font)
        except Exception:
            pass
        return img

    def _get_font(self, weight: str = "regular", size: int = 18) -> ImageFont.ImageFont:
        """Get a cached PIL font."""
        key = f"{weight}_{size}"
        if key in self._font_cache:
            return self._font_cache[key]
        font_path = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                     if weight == "bold"
                     else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        try:
            font = ImageFont.truetype(font_path, size)
        except Exception:
            font = ImageFont.load_default()
        self._font_cache[key] = font
        return font

    def add_viewer_avatar(self, username: str, avatar_path: Optional[str] = None, duration: float = 30.0):
        """Add a viewer's avatar with random zodiac animal when they join the live stream.
        
        The avatar falls from the top of the screen to 2/3 height, then bounces
        in sync with music rhythm. A random zodiac animal (12 con giáp) is assigned.
        """
        if not username:
            return
        with self._lock:
            if username in self.active_avatars:
                self.active_avatars[username]["expires_at"] = time.time() + duration
                return

            zodiac_entry = self._assign_random_zodiac()
            if zodiac_entry:
                avatar_path = zodiac_entry["path"]
                zodiac_name = zodiac_entry["zodiac"]
                animal_name = zodiac_entry["animal"]
            else:
                if avatar_path is None and self.avatar_pool:
                    avatar_path = random.choice(self.avatar_pool)
                zodiac_name = None
                animal_name = None

            # Assign random position in lower 1/3 of screen (below 2/3 height)
            top_bound = int(self._overlay_height * self._avatar_target_height_ratio)
            bottom_bound = int(self._overlay_height * 0.90) - self._avatar_display_size
            if bottom_bound < top_bound:
                bottom_bound = top_bound
            x_pos = random.randint(60, max(self._overlay_width - self._avatar_display_size - 60, 60))
            target_y = random.randint(top_bound, max(bottom_bound, top_bound))
            join_time = time.time()

            self.active_avatars[username] = {
                "avatar_path": avatar_path,
                "zodiac_name": zodiac_name,
                "animal_name": animal_name,
                "join_time": join_time,
                "expires_at": join_time + duration,
                "x_pos": x_pos,
                "start_y": -self._avatar_display_size - 30,
                "target_y": target_y,
                "fall_start": join_time,
                "fall_duration": self._avatar_fall_duration,
                "gift_count": 0,
                "gift_until": 0,
            }
        logger.info(f"Viewer avatar added: {username} -> zodiac={zodiac_name}/{animal_name}")

    def trigger_gift_animation(self, username: str, gift_name: str = ""):
        """Trigger special animation (scale + golden glow) when a viewer sends a gift."""
        with self._lock:
            if username in self.active_avatars:
                self.active_avatars[username]["gift_count"] += 1
                self.active_avatars[username]["gift_until"] = time.time() + 5
            else:
                self.add_viewer_avatar(username, duration=10.0)
                with self._lock:
                    self.active_avatars[username]["gift_count"] = 1
                    self.active_avatars[username]["gift_until"] = time.time() + 5
        logger.info(f"Gift animation triggered for {username}: {gift_name}")

    def remove_viewer_avatar(self, username: str):
        """Remove a viewer's avatar."""
        with self._lock:
            self.active_avatars.pop(username, None)

    def remove_excess_avatars(self, count: int):
        """Remove the specified number of avatars that are closest to expiry
        (i.e., those whose viewers left the stream)."""
        with self._lock:
            if count >= len(self.active_avatars):
                self.active_avatars.clear()
                return
            # Sort by expires_at (oldest expiry first = closest to removal)
            sorted_avatars = sorted(self.active_avatars.items(), key=lambda x: x[1].get("expires_at", 0))
            for username, _ in sorted_avatars[:count]:
                self.active_avatars.pop(username, None)
            logger.info(f"Removed {count} excess avatars (viewers left)")

    def clear_avatars(self):
        """Clear all active avatars (called when stream stops)."""
        with self._lock:
            self.active_avatars.clear()

    def start_overlay_fifo(self, width: int, height: int, fps: int = 10):
        """Create FIFO pipe and start overlay generation thread."""
        self._overlay_width = width
        self._overlay_height = height
        self._overlay_fps = fps
        self._overlay_fifo_path = os.path.join(OVERLAY_DIR, "avatar_overlay.fifo")
        if os.path.exists(self._overlay_fifo_path) or os.path.islink(self._overlay_fifo_path):
            try:
                os.remove(self._overlay_fifo_path)
            except Exception:
                pass
        os.mkfifo(self._overlay_fifo_path)
        logger.info(f"Created overlay FIFO: {self._overlay_fifo_path}")
        self._overlay_stop_event = threading.Event()
        self._overlay_thread = threading.Thread(target=self._overlay_thread_main, daemon=True)
        self._overlay_thread.start()
        logger.info(f"Overlay FIFO thread started ({width}x{height}, {fps}fps)")

    def stop_overlay_fifo(self):
        """Stop overlay thread and clean up FIFO."""
        if self._overlay_stop_event:
            self._overlay_stop_event.set()
        if self._overlay_thread and self._overlay_thread.is_alive():
            self._overlay_thread.join(timeout=3)
        if self._overlay_fifo_path and os.path.exists(self._overlay_fifo_path):
            try:
                os.remove(self._overlay_fifo_path)
            except Exception:
                pass
        self._overlay_fifo_path = None
        logger.info("Overlay FIFO stopped and cleaned up")

    def get_overlay_fifo_path(self) -> Optional[str]:
        """Return the FIFO path if overlay is active."""
        return self._overlay_fifo_path

    def _overlay_thread_main(self):
        """Background thread: generate overlay PNG frames and write to FIFO."""
        frame_interval = 1.0 / self._overlay_fps
        while not self._overlay_stop_event.is_set():
            try:
                fd = os.open(self._overlay_fifo_path, os.O_WRONLY | os.O_NONBLOCK)
                flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
                fifo = os.fdopen(fd, 'wb')
                logger.info("Overlay FIFO connected to FFmpeg")
                while not self._overlay_stop_event.is_set():
                    frame = self._render_overlay_frame()
                    if frame:
                        fifo.write(frame)
                        fifo.flush()
                    time.sleep(frame_interval)
                fifo.close()
                break
            except OSError as e:
                if e.errno == errno.ENXIO:
                    time.sleep(0.2)
                elif e.errno in (errno.EPIPE, errno.EBADF):
                    logger.debug("Overlay FIFO read end closed, reconnecting...")
                    try:
                        fifo.close()
                    except Exception:
                        pass
                    time.sleep(0.5)
                else:
                    logger.warning(f"Overlay thread FIFO error: {e}")
                    time.sleep(0.5)
            except Exception as e:
                logger.warning(f"Overlay thread error: {e}")
                time.sleep(0.5)

    def _render_overlay_frame(self) -> Optional[bytes]:
        """Render a single overlay frame with avatar images, names, and animations.
        
        - New avatars fall from top (y=-size) to 2/3 screen height with ease-out.
        - After landing, bounce at 2Hz in sync with music rhythm.
        - Zodiac name + animal name displayed below each avatar.
        - Gift animation: scale + golden glow.
        """
        if self._overlay_width == 0 or self._overlay_height == 0:
            return None
        img = Image.new('RGBA', (self._overlay_width, self._overlay_height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        current_time = time.time()
        with self._lock:
            expired = [u for u, info in self.active_avatars.items()
                       if current_time > info["expires_at"]]
            for u in expired:
                del self.active_avatars[u]
            avatars = list(self.active_avatars.items())
        if not avatars:
            buf = io.BytesIO()
            img.save(buf, format='PNG', compress_level=1)
            return buf.getvalue()
        avatar_size = self._avatar_display_size
        for username, info in avatars:
            x = info.get("x_pos", 100)
            avatar_path = info.get("avatar_path")
            zodiac_name = info.get("zodiac_name")
            animal_name = info.get("animal_name")
            join_time = info.get("join_time", current_time)
            fall_start = info.get("fall_start", join_time)
            fall_duration = info.get("fall_duration", self._avatar_fall_duration)
            start_y = info.get("start_y", -avatar_size - 30)
            target_y = info.get("target_y", int(self._overlay_height * self._avatar_target_height_ratio))

            fall_elapsed = current_time - fall_start
            fall_progress = min(1.0, fall_elapsed / fall_duration) if fall_duration > 0 else 1.0
            # Ease-out interpolation: faster at start, slower near target
            eased = 1.0 - math.pow(1.0 - fall_progress, 3)
            y = int(start_y + (target_y - start_y) * eased)

            # Bounce animation (2 Hz - follows music rhythm)
            bounce_offset = int(20 * math.sin(2 * math.pi * 2.0 * current_time))
            if fall_progress >= 1.0:
                y += bounce_offset

            # Gift animation (scale + golden glow)
            scale = 1.0
            gift_glow = 0
            if current_time < info.get("gift_until", 0):
                elapsed = current_time - (info["gift_until"] - 5.0)
                scale = 1.0 + 0.3 * abs(math.sin(elapsed * 4))
                gift_glow = max(0, int(150 * math.sin(elapsed * 8)))

            if avatar_path:
                avatar_img = self._get_avatar_image(avatar_path)
            else:
                avatar_img = self._generate_default_avatar(username)

            if scale != 1.0:
                new_size = max(60, int(avatar_size * scale))
                avatar_img = avatar_img.copy().resize((new_size, new_size), Image.LANCZOS)
                y -= (new_size - avatar_size) // 2

            # Golden glow during gift animation
            if gift_glow > 0:
                cx = x + avatar_size // 2
                cy = y + avatar_size // 2
                glow_r = avatar_size // 2 + int(10 * scale)
                draw.ellipse([cx - glow_r, cy - glow_r, cx + glow_r, cy + glow_r],
                            fill=(255, 215, 0, min(gift_glow, 120)))

            img.paste(avatar_img, (x, y), avatar_img)

            # Draw zodiac + animal name below avatar
            display_name = zodiac_name if zodiac_name else username[:12]
            font = self._get_font("bold", 18)
            try:
                bbox = draw.textbbox((0, 0), display_name, font=font)
                name_w = bbox[2] - bbox[0]
            except Exception:
                name_w = len(display_name) * 12
            name_y = y + avatar_size + 5

            animal_font = self._get_font("regular", 14)
            if animal_name:
                try:
                    abbox = draw.textbbox((0, 0), animal_name, font=animal_font)
                    animal_w = abbox[2] - abbox[0]
                except Exception:
                    animal_w = len(animal_name) * 8
                animal_y = name_y + 22
            else:
                animal_w = 0
                animal_y = name_y + 22

            # Shadow (black, offset by 1)
            draw.text((x + avatar_size // 2 - name_w // 2 + 1, name_y + 1), display_name,
                     fill=(0, 0, 0, 180), font=font)
            if animal_name:
                draw.text((x + avatar_size // 2 - animal_w // 2 + 1, animal_y + 1), animal_name,
                         fill=(0, 0, 0, 160), font=animal_font)

            # Main text (gold for zodiac, white for animal)
            draw.text((x + avatar_size // 2 - name_w // 2, name_y), display_name,
                     fill=(255, 215, 0, 240), font=font)
            if animal_name:
                draw.text((x + avatar_size // 2 - animal_w // 2, animal_y), animal_name,
                         fill=(255, 255, 255, 230), font=animal_font)

        buf = io.BytesIO()
        img.save(buf, format='PNG', compress_level=1)
        return buf.getvalue()


# Global instance
overlay_renderer = OverlayRenderer()
