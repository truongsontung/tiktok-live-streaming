#!/usr/bin/env python3
"""
TikTok Live Automated Streaming Engine
Handles FFmpeg RTMP streaming, video loops, auto-reconnects, overlay text, and status telemetry.
Integrates TikTok Live WebSocket client for real-time comment handling and AI response engine.
"""

import os
import sys
import json
import time
import glob
import subprocess
import threading
import logging
from datetime import datetime

from tiktok_live_client import live_client
from sign_server_with_browser import get_tt_target_idc
from overlay_renderer import overlay_renderer
from live_studio_scraper import scraper

# Check if sign server is available (for tt_target_idc)
try:
    from sign_server_with_browser import SignServerWithBrowser, _cookie_cache
    _sign_server_available = True
except ImportError:
    _sign_server_available = False

# Base paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
MEDIA_DIR = os.path.join(BASE_DIR, "media")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

os.makedirs(MEDIA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# Logger setup — basicConfig is a no-op if root already has handlers,
# so we always add the FileHandler explicitly to ensure stream.log is written.
log_file_path = os.path.join(LOGS_DIR, "stream.log")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
)
_fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s")
_fh = logging.FileHandler(log_file_path)
_fh.setFormatter(_fmt)
_root = logging.getLogger()
if not any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == log_file_path for h in _root.handlers):
    _root.addHandler(_fh)
logger = logging.getLogger("StreamEngine")


class StreamEngine:
    def __init__(self):
        self.process = None
        # self.preview_process = None  # Preview now embedded in main FFmpeg
        self.is_running = False
        self.should_stop = False
        self.status = "STOPPED"  # STOPPED, STREAMING, RECONNECTING, ERROR
        self.start_time = None
        self.reconnect_count = 0
        self.last_error = ""
        self.lock = threading.Lock()
        self.monitor_thread = None
        self.comment_processor_thread = None
        self._comment_stop_event = threading.Event()
        self._key_refresh_thread = None

        self._last_viewer_count: int = 0

        # Configure callbacks from live client
        live_client.on_comment_callbacks.append(self._handle_live_comment)
        live_client.on_viewer_join_callbacks.append(self._handle_viewer_join)
        live_client.on_gift_callbacks.append(self._handle_live_gift)

    def _handle_viewer_join(self, viewer_count: int):
        """Callback when viewer count changes (viewer join OR leave).
        Only zodiac avatar overlay is rendered — no text overlays."""
        prev = self._last_viewer_count
        delta = viewer_count - prev
        self._last_viewer_count = viewer_count
        logger.info(f"[LIVE] Viewer count: {prev} -> {viewer_count} (delta={delta})")

        if not overlay_renderer.enabled_overlays.get("avatar_overlay", False):
            logger.warning(f"[LIVE] Avatar overlay DISABLED — skipping avatar add (delta={delta})")
            return

        if delta > 0:
            logger.info(f"[LIVE] Adding {delta} new avatar(s) for joining viewers")
            for i in range(delta):
                uid = f"viewer_{time.time()}_{i}"
                overlay_renderer.add_viewer_avatar(uid)
                logger.info(f"[LIVE] Added avatar for {uid}")
        elif delta < 0:
            logger.info(f"[LIVE] Removing {abs(delta)} avatar(s) for leaving viewers")
            overlay_renderer.remove_excess_avatars(abs(delta))

    def _handle_live_gift(self, gift_data: dict, total_gifts: int):
        """Callback when a gift is received — trigger special avatar animation."""
        username = gift_data.get("user", "Viewer")
        gift_name = gift_data.get("gift_name", "")
        diamonds = gift_data.get("diamonds", 0)
        gift_count = gift_data.get('gift_count', 1)
        logger.info(f"[LIVE] Gift: {username} -> {gift_name} (x{gift_count}, {diamonds} 💎)")
        if overlay_renderer.enabled_overlays.get("avatar_overlay", False):
            logger.info(f"[LIVE] Triggering gift animation for {username}")
            overlay_renderer.trigger_gift_animation(username, gift_name)
        else:
            logger.warning(f"[LIVE] Avatar overlay DISABLED — gift animation skipped")


    def _handle_live_comment(self, comment_data: dict, comment_count: int):
        """Callback when a new comment arrives — log only (no text overlay)."""
        username = comment_data.get("user", "?")
        comment = comment_data.get("comment", "")[:50]
        logger.info(f"[LIVE] Comment from {username}: {comment}")
    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        return {}

    def get_full_rtmp_target(self, config):
        rtmp_url = config.get("rtmp_url", "").strip()
        stream_key = config.get("stream_key", "").strip()
        if not rtmp_url:
            return ""
        if not rtmp_url.endswith("/"):
            rtmp_url += "/"
        return f"{rtmp_url}{stream_key}"

    def get_media_playlist(self):
        config = self.load_config()
        extensions = ("*.mp4", "*.mkv", "*.mov", "*.avi", "*.ts", "*.flv", "*.webm")
        files = []
        for ext in extensions:
            files.extend(glob.glob(os.path.join(MEDIA_DIR, ext)))
        files.sort()
        
        # Use media_playlist from config if specified
        playlist_names = config.get("media_playlist", [])
        if playlist_names:
            playlist = []
            for name in playlist_names:
                for f in files:
                    if os.path.basename(f) == name:
                        playlist.append(f)
                        break
            return playlist if playlist else files
        
        # If active_media is specified, return only that file
        active = config.get("active_media", "")
        if active:
            for f in files:
                if os.path.basename(f) == active:
                    return [f]
        return files

    def _get_video_codec(self, video_path):
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "stream=codec_name", "-of", "csv=p=0", video_path],
                capture_output=True, text=True, timeout=10
            )
            return result.stdout.strip().split('\n')[0]
        except:
            return None

    def _get_audio_codec(self, video_path):
        """Return audio stream count; None/empty if no audio."""
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-select_streams", "a:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", video_path],
                capture_output=True, text=True, timeout=10
            )
            out = result.stdout.strip()
            return out if out else None
        except:
            return None

    def _ensure_audio(self, video_path, output_path):
        """Add silent audio track to a video if it has none (for concat compatibility)."""
        if os.path.exists(output_path):
            return
        codec_info = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=10
        ).stdout.strip().split(",")

        if len(codec_info) >= 3:
            vcodec, vw, vh = codec_info[0], int(codec_info[1]), int(codec_info[2])
        else:
            vcodec = codec_info[0] if codec_info else ""

        has_audio = self._get_audio_codec(video_path)
        if not has_audio:
            subprocess.run([
                "ffmpeg", "-y", "-i", video_path,
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                "-shortest", output_path
            ], capture_output=True, timeout=300)

    def _convert_to_h264(self, input_path, output_path, resolution):
        w, h = resolution.split("x")
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-vf", f"scale={resolution}:force_original_aspect_ratio=decrease,pad={w}:{h}:0:0:black,fps=30",
            "-c:v", "libx264", "-preset", "slow", "-crf", "27",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            output_path
        ]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
        except subprocess.TimeoutExpired:
            logger.warning(f"Convert timeout for {input_path}, using original if valid")
        except Exception as e:
            logger.warning(f"Convert failed for {input_path}: {e}")

    def _convert_incompatible(self, config):
        """Convert non-H.264 videos to temp H.264 files before streaming."""
        playlist = self.get_media_playlist()
        if not playlist:
            return
        resolution = config.get("resolution", "1080x1920")
        self._temp_files = []
        for video_path in playlist:
            temp_path = os.path.join(LOGS_DIR, "converted_" + os.path.basename(video_path))
            # Reuse existing conversion if file is less than 5 min old
            if os.path.exists(temp_path) and (time.time() - os.path.getmtime(temp_path) < 300):
                self._temp_files.append(temp_path)
                continue
            codec_info = subprocess.run(
                ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0", video_path],
                capture_output=True, text=True, timeout=10
            ).stdout.strip().split('\n')[0]
            if codec_info and codec_info not in ("h264", "avc"):
                logger.info(f"Converting {os.path.basename(video_path)} ({codec_info}) to H.264...")
                self._convert_to_h264(video_path, temp_path, resolution)
                if os.path.exists(temp_path) and os.path.getsize(temp_path) > 1000:
                    self._temp_files.append(temp_path)
                    logger.info(f"Converted successfully")
                else:
                    logger.warning(f"Convert failed for {os.path.basename(video_path)}")

    def create_fallback_test_video(self):
        """Generates a test pattern MP4 file if no user media is present."""
        test_file = os.path.join(MEDIA_DIR, "test_pattern.mp4")
        if not os.path.exists(test_file):
            logger.info("No media found. Generating test pattern video...")
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100",
                "-c:v", "libx264", "-t", "10", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                test_file
            ]
            try:
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                logger.info(f"Generated sample video: {test_file}")
            except Exception as e:
                logger.error(f"Failed to generate test pattern: {e}")
        return test_file

    def build_ffmpeg_command(self, config):
        target = self.get_full_rtmp_target(config)
        if not target:
            raise ValueError("RTMP URL or Stream Key is missing!")

        playlist = self.get_media_playlist()
        if not playlist:
            test_vid = self.create_fallback_test_video()
            if os.path.exists(test_vid):
                playlist = [test_vid]

        if not playlist:
            raise ValueError("No video media available to stream!")

        resolution = config.get("resolution", "1080x1920")
        fps = config.get("fps", 30)
        v_bitrate = config.get("video_bitrate", "4000k")
        a_bitrate = config.get("audio_bitrate", "128k")

        # Use pre-converted H.264 files if available (avoids concat codec mismatch)
        compatible_playlist = self._write_playlist(config)

        playlist_txt = os.path.join(LOGS_DIR, "playlist.txt")
        loop_enabled = config.get("loop", True)

        # Single video: use -stream_loop directly (no concat needed)
        # Multiple videos: concat filter with -i per video (handles different resolutions)
        if len(compatible_playlist) == 1:
            cmd = [
                "ffmpeg", "-y",
                "-fflags", "+genpts+igndts",
                "-re",
                "-stream_loop", "-1" if loop_enabled else "0",
                "-i", compatible_playlist[0]
            ]
        else:
            # Concat filter: frame-level concat, handles different codecs/resolutions
            # No packet-level boundary issues
            cmd = [
                "ffmpeg", "-y",
                "-fflags", "+genpts+igndts",
                "-re",
            ]
            n = len(compatible_playlist)
            for vp in compatible_playlist:
                cmd.extend(["-i", vp])

        # Use overlay renderer for dynamic overlays
        w, h = resolution.split("x")
        base_filter = f"scale={resolution}:force_original_aspect_ratio=decrease,pad={w}:{h}:0:0:black"

        # Apply overlay renderer settings from config
        overlay_renderer.set_enabled(config.get("overlay_enabled", True))
        overlay_config = config.get("overlay_config")
        if overlay_config and isinstance(overlay_config, dict):
            overlay_renderer.configure_overlays(overlay_config)

        avatar_enabled = overlay_renderer.enabled_overlays.get("avatar_overlay", False)
        fifo_path = overlay_renderer.get_overlay_fifo_path()

        # Preview dimensions (embedded in main FFmpeg for sync)
        pw, ph = min(360, int(w)), min(640, int(h))
        preview_vf = f"scale={pw}:{ph}:force_original_aspect_ratio=decrease,pad={pw}:{ph}:0:0:black,fps=1/5"

        if len(compatible_playlist) == 1:
            if avatar_enabled and fifo_path and os.path.exists(fifo_path):
                overlay_fps = overlay_renderer.get_overlay_fps()
                cmd.extend([
                    "-f", "image2pipe", "-vcodec", "png",
                    "-framerate", str(overlay_fps),
                    "-i", fifo_path
                ])
                filter_str = f"[0:v]{base_filter}[base];[1:v]scale={w}:{h}:flags=lanczos[ovr];[base][ovr]overlay=0:0:format=auto:eof_action=pass:repeatlast=1[vout];[vout]split=2[vmain][vprev_raw];[vprev_raw]{preview_vf}[vprev]"
                cmd.extend(["-filter_complex", filter_str, "-map", "[vmain]", "-map", "0:a?"])
            else:
                # Single video, no overlay: use split for preview
                filter_str = f"[0:v]{base_filter},setsar=1,format=yuv420p[vout];[vout]split=2[vmain][vprev_raw];[vprev_raw]{preview_vf}[vprev]"
                cmd.extend(["-filter_complex", filter_str, "-map", "[vmain]", "-map", "0:a?"])
        else:
            # Multi-video: concat filter with optional overlay
            n = len(compatible_playlist)
            if avatar_enabled and fifo_path and os.path.exists(fifo_path):
                overlay_fps = overlay_renderer.get_overlay_fps()
                cmd.extend([
                    "-f", "image2pipe", "-vcodec", "png",
                    "-framerate", str(overlay_fps),
                    "-i", fifo_path
                ])
                # Scale each input → concat → overlay → split for preview
                filter_str = ""
                for i in range(n):
                    filter_str += f"[{i}:v]{base_filter},fps=30,format=yuv420p,setsar=1[v{i}];[{i}:a]aresample=44100,aformat=channel_layouts=stereo[va{i}];"
                # Interleave video/audio inputs for concat filter
                concat_inputs = "".join(f"[v{i}][va{i}]" for i in range(n))
                overlay_idx = n
                filter_str += f"{concat_inputs}concat=n={n}:v=1:a=1[vconcat][aconcat];[{overlay_idx}:v]scale={w}:{h}:flags=lanczos[ovr];[vconcat][ovr]overlay=0:0:format=auto:eof_action=pass:repeatlast=1[vout];[vout]split=2[vmain][vprev_raw];[vprev_raw]{preview_vf}[vprev]"
                cmd.extend(["-filter_complex", filter_str, "-map", "[vmain]", "-map", "[aconcat]"])
            else:
                # No overlay: concat filter only
                filter_str = ""
                for i in range(n):
                    filter_str += f"[{i}:v]{base_filter},fps=30,format=yuv420p,setsar=1[v{i}];[{i}:a]aresample=44100,aformat=channel_layouts=stereo[va{i}];"
                # Interleave video/audio inputs for concat filter
                concat_inputs = "".join(f"[v{i}][va{i}]" for i in range(n))
                filter_str += f"{concat_inputs}concat=n={n}:v=1:a=1[vout][aout];[vout]split=2[vmain][vprev_raw];[vprev_raw]{preview_vf}[vprev]"
                cmd.extend(["-filter_complex", filter_str, "-map", "[vmain]", "-map", "[aout]"])

        # Preview output: JPEG from same filter pipeline (perfect sync with stream)
        # Main output encode settings
        cmd.extend([
            "-c:v:0", "libx264",
            "-preset", "superfast",
            "-tune", "zerolatency",
            "-g", str(fps),
            "-b:v:0", v_bitrate,
            "-maxrate:v:0", v_bitrate,
            "-bufsize:v:0", v_bitrate,
            "-pix_fmt:v:0", "yuv420p",
            "-c:a", "aac",
            "-b:a", a_bitrate,
            "-ar", "44100",
            "-ac", "2",
            "-f", "flv",
            "-flvflags", "no_duration_filesize",
            target,
            # Preview JPEG output (from filter_complex [vprev] label)
            "-map", "[vprev]",
            "-c:v:1", "mjpeg",
            "-update", "1",
            "-q:v", "3",
            "/tmp/preview.jpg",
        ])

        return cmd

    def _comment_monitor_loop(self):
        """Monitor loop for processing live interaction (avatars only, no text overlays)."""
        while not self._comment_stop_event.is_set():
            try:
                live_telemetry = live_client.get_telemetry()
            except Exception as e:
                logger.debug(f"Comment monitor error: {e}")
            self._comment_stop_event.wait(timeout=1.0)

    def start_stream(self):
        # Kill orphan ffmpeg RTMPS encode (leak from killed server -> double encode -> CPU full -> jitter)
        try:
            subprocess.run(["pkill", "-9", "-f", "rtmps://push-rtmp"], capture_output=True)
        except Exception:
            pass
        with self.lock:
            if self.is_running:
                return False, "Stream is already running!"
            
            # Wait for any previous _run_loop thread to fully exit
            if self.monitor_thread and self.monitor_thread.is_alive():
                self.should_stop = True
                self.monitor_thread.join(timeout=5)

            self.should_stop = False
            self._comment_stop_event.clear()
            self.reconnect_count = 0
            self.last_error = ""

        config = self.load_config()

        # Auto-extract TikTok session from browser if not configured
        tiktok_session = config.get("tiktok_session", "").strip()
        if not tiktok_session:
            try:
                from extract_session import get_tiktok_cookies_from_chromium
                tiktok_session = get_tiktok_cookies_from_chromium()
                if tiktok_session:
                    config["tiktok_session"] = tiktok_session
                    with open(CONFIG_FILE, "w") as f:
                        json.dump(config, f, indent=2)
                    logger.info("Auto-extracted TikTok session from Chromium profile!")
            except Exception as e:
                logger.warning(f"Auto-extract session failed: {e}")

        # Configure overlay renderer — enable overlays when streaming (default ON)
        overlay_enabled = config.get("overlay_enabled", True)
        overlay_renderer.set_enabled(overlay_enabled)
        
        # Configure overlay sub-types
        overlay_config = config.get("overlay_config")
        if overlay_config and isinstance(overlay_config, dict):
            overlay_renderer.configure_overlays(overlay_config)
        elif overlay_enabled:
            # Default: enable only avatar overlay (no text overlays)
            overlay_renderer.configure_overlays({
                "avatar_overlay": True,
            })

        # Start avatar overlay FIFO if enabled
        if overlay_renderer.enabled_overlays.get("avatar_overlay", False):
            overlay_renderer.start_overlay_fifo(180, 320, 30)
            overlay_renderer.load_avatar_pool()

            # Apply zodiac-specific settings from config (with fallback defaults)
            zodiac_overlay_config = overlay_config if isinstance(overlay_config, dict) else {}
            overlay_renderer._avatar_fall_duration = zodiac_overlay_config.get("zodiac_fall_duration", 1.5)
            overlay_renderer._avatar_target_height_ratio = zodiac_overlay_config.get("zodiac_target_height_ratio", 0.66)

            # Seed a few test avatars so overlay is visible immediately even if TikTok live not connected
            for i in range(3):
                overlay_renderer.add_viewer_avatar(f"seed_avatar_{i}")
                time.sleep(0.3)

        # Configure live client with TikTok credentials (integrated from comment_forwarder)
        tiktok_username = config.get("tiktok_username", "")
        tiktok_session = config.get("tiktok_session", "").strip()
        tt_target_idc = get_tt_target_idc()
        if tiktok_username and live_client.is_available():
            live_client.configure(tiktok_username, None, None, tiktok_session, tt_target_idc or None)
            connected = live_client.connect_async()
            if connected:
                logger.info(f"Connected to TikTok live room for: @{tiktok_username}")
            else:
                logger.warning("Failed to connect to TikTok live room, streaming without live interaction")

        # Reuse existing converted files — only reconvert if source changed
        self._temp_files = []
        self._convert_incompatible(config)

        # Start comment processor thread
        self.comment_processor_thread = threading.Thread(target=self._comment_monitor_loop, daemon=True)
        self.comment_processor_thread.start()

        # Preview will be started in _run_loop AFTER playlist.txt is written
        self.monitor_thread = threading.Thread(target=self._run_loop, daemon=True)
        self.monitor_thread.start()

        # Start key refresh thread (every 5 min)
        self._key_refresh_stop = threading.Event()
        self._key_refresh_thread = threading.Thread(target=self._key_refresh_loop, daemon=True)
        self._key_refresh_thread.start()
        return True, "Streaming starting in background daemon."

    def _write_playlist(self, config):
        """Write the current media playlist to logs/playlist.txt (fresh content)."""
        playlist = self.get_media_playlist()
        compatible_playlist = []
        for video_path in playlist:
            temp_path = os.path.join(LOGS_DIR, "converted_" + os.path.basename(video_path))
            if temp_path in getattr(self, '_temp_files', []) and os.path.exists(temp_path):
                compatible_playlist.append(temp_path)
            else:
                codec = self._get_video_codec(video_path)
                if codec and codec not in ("h264", "avc") and os.path.exists(temp_path):
                    compatible_playlist.append(temp_path)
                else:
                    compatible_playlist.append(video_path)

        playlist_txt = os.path.join(LOGS_DIR, "playlist.txt")
        # Ensure every video has an audio track (concat demuxer needs audio on all inputs)
        final_playlist = []
        for video_path in compatible_playlist:
            if self._get_audio_codec(video_path):
                final_playlist.append(video_path)
            else:
                # No audio → create silent-audio copy in logs dir
                audio_path = os.path.join(LOGS_DIR, "audioadded_" + os.path.basename(video_path))
                if not os.path.exists(audio_path):
                    logger.info(f"Adding silent audio to: {os.path.basename(video_path)}")
                    self._ensure_audio(video_path, audio_path)
                final_playlist.append(audio_path if os.path.exists(audio_path) else video_path)

        if final_playlist:
            # Write playlist.txt (for preview) — concat filter is used in build_ffmpeg_command
            # Looping for multi-video is handled by _run_loop reconnect (exit0 → restart)
            with open(playlist_txt, "w") as f:
                for video_path in final_playlist:
                    f.write(f"file '{video_path}'\n")
        return final_playlist

    def _start_preview(self, config):
        """Preview is now embedded in the main FFmpeg command for sync.
        Kept as no-op for backward compatibility (called from _run_loop)."""
        pass

    def _stop_preview(self):
        """Preview is now embedded in main FFmpeg — no separate process to stop."""
        pass

    def stop_stream(self):
        with self.lock:
            self.should_stop = True
            self._comment_stop_event.set()
            if self.process and self.process.poll() is None:
                logger.info("Stopping FFmpeg stream process...")
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            self.is_running = False
            self.status = "STOPPED"

        # Stop preview process
        self._stop_preview()

        # Kill ALL FFmpeg processes to prevent stale processes
        subprocess.run(["pkill", "-9", "-f", "ffmpeg"], capture_output=True)
        time.sleep(1)

        # Reap any remaining zombie ffmpeg/preview processes
        if self.process:
            try: self.process.wait(timeout=2)
            except: pass

        # Stop avatar overlay FIFO
        overlay_renderer.stop_overlay_fifo()
        overlay_renderer.clear_avatars()

        # Wait for _run_loop thread to fully exit
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=5)

        # Stop key refresh thread
        if hasattr(self, '_key_refresh_stop'):
            self._key_refresh_stop.set()
        if self._key_refresh_thread and self._key_refresh_thread.is_alive():
            self._key_refresh_thread.join(timeout=5)

        # Clean up temp files
        if hasattr(self, '_temp_files'):
            for temp_file in self._temp_files:
                try:
                    os.remove(temp_file)
                except:
                    pass
        # Clean up audioadded + converted temp files
        for tmp in glob.glob(os.path.join(LOGS_DIR, "audioadded_*")) + glob.glob(os.path.join(LOGS_DIR, "converted_*")):
            try:
                os.remove(tmp)
            except:
                pass
        self._temp_files = []

        # Disconnect live client
        live_client.disconnect()
        logger.info("Stream stopped completely.")
        return True, "Stream stopped."

    def _key_refresh_loop(self):
        """Refresh stream key every 5 minutes (keys are single-use and can expire)."""
        while not self._key_refresh_stop.is_set():
            self._key_refresh_stop.wait(timeout=7200)
            if self._key_refresh_stop.is_set():
                break
            if not self.is_running:
                break
            try:
                config = self.load_config()
                tiktok_session = config.get("tiktok_session", "").strip()

                # Auto-extract session from browser if missing
                if not tiktok_session:
                    try:
                        from extract_session import get_tiktok_cookies_from_chromium
                        tiktok_session = get_tiktok_cookies_from_chromium()
                        if tiktok_session:
                            config["tiktok_session"] = tiktok_session
                            with open(CONFIG_FILE, "w") as f:
                                json.dump(config, f, indent=2)
                            logger.info("Key refresh: auto-extracted session from browser")
                    except Exception as e:
                        logger.warning(f"Key refresh: session extract failed: {e}")

                if tiktok_session and scraper and scraper.available:
                    logger.info("Scheduled key refresh: fetching new stream key...")
                    fresh = scraper.fetch_stream_key_with_session(tiktok_session)
                    if fresh and fresh.get("rtmp_url") and fresh.get("stream_key"):
                        config["rtmp_url"] = fresh["rtmp_url"]
                        config["stream_key"] = fresh["stream_key"]
                        with open(CONFIG_FILE, "w") as f:
                            json.dump(config, f, indent=2)
                        logger.info("Scheduled key refresh: new key saved!")
            except Exception as e:
                logger.warning(f"Scheduled key refresh failed: {e}")

    def _run_loop(self):
        while not self.should_stop:
            config = self.load_config()

            # Auto-fetch stream key (keys are single-use, refresh on reconnect)
            tiktok_session = config.get("tiktok_session", "").strip()
            if tiktok_session and scraper and scraper.available:
                try:
                    logger.info("Fetching fresh stream key from TikTok...")
                    fresh = scraper.fetch_stream_key_with_session(tiktok_session)
                    if fresh and fresh.get("rtmp_url") and fresh.get("stream_key"):
                        config["rtmp_url"] = fresh["rtmp_url"]
                        config["stream_key"] = fresh["stream_key"]
                        with open(CONFIG_FILE, "w") as f:
                            json.dump(config, f, indent=2)
                        logger.info("Fresh stream key fetched and saved!")
                except Exception as e:
                    logger.warning(f"Auto-fetch key failed: {e}")

            # Rebuild fresh playlist on reconnect (reuse converted/normalized files)
            self._temp_files = []

            try:
                # build_ffmpeg_command writes playlist.txt via _write_playlist
                cmd = self.build_ffmpeg_command(config)

                # Start preview AFTER playlist.txt is written
                self._start_preview(config)

                logger.info(f"Launching FFmpeg Stream process (Target: {config.get('rtmp_url')})...")

                with self.lock:
                    self.status = "STREAMING"
                    self.is_running = True
                    self.start_time = time.time()

                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )

                # Read output logs line by line
                for line in iter(self.process.stdout.readline, ''):
                    if not line:
                        break
                    line_clean = line.strip()
                    if "error" in line_clean.lower() or "failed" in line_clean.lower():
                        logger.warning(f"[FFmpeg Log] {line_clean}")

                self.process.wait()
                exit_code = self.process.returncode
                logger.info(f"FFmpeg process exited with code: {exit_code}")

            except Exception as e:
                self.last_error = str(e)
                logger.error(f"Streaming error: {e}")

            if self.should_stop:
                break

            # Reconnect on error OR when playlist finished normally (exit0) with loop enabled
            # (single video uses -stream_loop which never exits 0, so this mainly affects multi-video concat)
            should_reconnect = (exit_code != 0) or (exit_code == 0 and loop_enabled)
            if should_reconnect and config.get("auto_reconnect", True):
                self.reconnect_count += 1
                logger.info(f"Auto-reconnecting stream (Attempt #{self.reconnect_count})... Waiting 5 seconds.")
                with self.lock:
                    self.status = "RECONNECTING"
                # Re-check should_stop periodically during reconnect delay
                for _ in range(50):
                    if self.should_stop:
                        break
                    time.sleep(0.1)
                if self.should_stop:
                    break
            else:
                break

        with self.lock:
            self.is_running = False
            self.status = "STOPPED"

    def get_telemetry(self):
        with self.lock:
            uptime = 0
            if self.is_running and self.start_time:
                uptime = int(time.time() - self.start_time)

            config = self.load_config()
            playlist = [os.path.basename(p) for p in self.get_media_playlist()]

            telemetry = {
                "status": self.status,
                "is_running": self.is_running,
                "uptime_seconds": uptime,
                "reconnect_count": self.reconnect_count,
                "last_error": self.last_error,
                "pid": self.process.pid if (self.process and self.process.poll() is None) else None,
                "rtmp_url": config.get("rtmp_url", ""),
                "has_stream_key": bool(config.get("stream_key", "").strip()),
                "playlist_count": len(playlist),
                "playlist_files": playlist,
                "resolution": config.get("resolution", "1080x1920"),
                "overlay_text": config.get("overlay_text", ""),
                "live_client": live_client.get_telemetry(),
                "overlay_renderer": overlay_renderer.get_telemetry(),
            }
            return telemetry


# Global engine instance
engine = StreamEngine()

if __name__ == "__main__":
    print("Testing StreamEngine initialization...")
    print(json.dumps(engine.get_telemetry(), indent=2))
