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
from ai_engine import ai_engine
from overlay_renderer import overlay_renderer
from live_studio_scraper import scraper

# Base paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
MEDIA_DIR = os.path.join(BASE_DIR, "media")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

os.makedirs(MEDIA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# Logger setup
log_file_path = os.path.join(LOGS_DIR, "stream.log")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file_path),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("StreamEngine")


class StreamEngine:
    def __init__(self):
        self.process = None
        self.preview_process = None
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

        # Configure callback from live client to AI engine
        live_client.on_comment_callbacks.append(self._handle_live_comment)

    def _handle_live_comment(self, comment_data: dict, comment_count: int):
        """Callback when a new comment arrives from TikTok live."""
        # Add to overlay
        overlay_renderer.add_comment(comment_data.get("user", "Viewer"), comment_data.get("comment", ""))

        # Generate AI response
        if ai_engine.enabled:
            ai_response = ai_engine.generate_response(
                comment_data.get("comment", ""),
                comment_data.get("user", "Viewer")
            )
            if ai_response:
                overlay_renderer.add_comment("🤖 AI Assistant", ai_response, is_ai_response=True)
                logger.info(f"AI Response: {ai_response}")

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

    def _convert_to_h264(self, input_path, output_path, resolution):
        w, h = resolution.split("x")
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-vf", f"scale={resolution}:force_original_aspect_ratio=decrease,pad={w}:{h}:0:0:black,fps=30",
            "-c:v", "libx264", "-preset", "superfast", "-crf", "23",
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
            codec = self._get_video_codec(video_path)
            if codec and codec not in ("h264", "avc"):
                temp_path = os.path.join(LOGS_DIR, "converted_" + os.path.basename(video_path))
                if os.path.exists(temp_path):
                    self._temp_files.append(temp_path)
                    continue
                logger.info(f"Converting {os.path.basename(video_path)} ({codec}) to H.264...")
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
        compatible_playlist = []
        for video_path in playlist:
            temp_path = os.path.join(LOGS_DIR, "converted_" + os.path.basename(video_path))
            if hasattr(self, '_temp_files') and temp_path in self._temp_files and os.path.exists(temp_path):
                compatible_playlist.append(temp_path)
            else:
                codec = self._get_video_codec(video_path)
                if codec and codec not in ("h264", "avc") and os.path.exists(temp_path):
                    compatible_playlist.append(temp_path)
                else:
                    compatible_playlist.append(video_path)

        # Write playlist with compatible files
        playlist_txt = os.path.join(LOGS_DIR, "playlist.txt")
        with open(playlist_txt, "w") as f:
            for video_path in compatible_playlist:
                f.write(f"file '{video_path}'\n")

        # Base FFmpeg command
        cmd = [
            "ffmpeg", "-y",
            "-re",
            "-f", "concat",
            "-safe", "0",
            "-stream_loop", "-1" if config.get("loop", True) else "0",
            "-i", playlist_txt
        ]

        # Use overlay renderer for dynamic overlays
        # Scale and pad: resize to fit, then pad to exact resolution (centered)
        w, h = resolution.split("x")
        filter_str = f"scale={resolution}:force_original_aspect_ratio=decrease,pad={w}:{h}:0:0:black"
        
        # Build drawtext filters using overlay renderer
        overlay_renderer.set_enabled(True)
        drawtext_args = overlay_renderer.get_ffmpeg_drawtext_args(config)
        if drawtext_args:
            filter_str += "," + ",".join(drawtext_args)

        cmd.extend([
            "-vf", filter_str,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "zerolatency",
            "-g", str(fps * 2),
            "-b:v", v_bitrate,
            "-maxrate", v_bitrate,
            "-bufsize", f"{int(v_bitrate.replace('k',''))*2}k",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", a_bitrate,
            "-ar", "44100",
            "-ac", "2",
            "-f", "flv",
            "-flvflags", "no_duration_filesize",
            target
        ])

        return cmd

    def _comment_monitor_loop(self):
        """Monitor loop for updating overlays and processing AI responses."""
        while not self._comment_stop_event.is_set():
            try:
                live_telemetry = live_client.get_telemetry()
                engine_telemetry = self.get_telemetry()
                overlay_renderer.update_from_clients(live_telemetry, engine_telemetry)
            except Exception as e:
                logger.debug(f"Comment monitor error: {e}")
            self._comment_stop_event.wait(timeout=1.0)

    def start_stream(self):
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

        # Configure AI engine if enabled
        ai_config = config.get("ai_config") or {}
        if ai_config.get("enabled", False) and ai_config.get("api_key"):
            model = ai_config.get("model", "gpt-4o-mini")
            persona = ai_config.get("persona", "assistant")
            ai_engine.configure(ai_config["api_key"], model, persona)
            ai_engine.set_enabled(True)

        # Configure overlay renderer
        overlay_renderer.set_enabled(True)

        # Configure live client if tiktok username is provided
        tiktok_username = config.get("tiktok_username", "")
        if tiktok_username and live_client.is_available():
            live_client.configure(tiktok_username)
            connected = live_client.connect_async()
            if connected:
                logger.info(f"Connected to TikTok live room for: @{tiktok_username}")
            else:
                logger.warning("Failed to connect to TikTok live room, streaming without live interaction")

        # Convert incompatible videos before starting (non-blocking)
        self._convert_incompatible(config)

        # Start comment processor thread
        self.comment_processor_thread = threading.Thread(target=self._comment_monitor_loop, daemon=True)
        self.comment_processor_thread.start()

        # Start preview process
        self._start_preview(config)

        self.monitor_thread = threading.Thread(target=self._run_loop, daemon=True)
        self.monitor_thread.start()

        # Start key refresh thread (every 5 min)
        self._key_refresh_stop = threading.Event()
        self._key_refresh_thread = threading.Thread(target=self._key_refresh_loop, daemon=True)
        self._key_refresh_thread.start()
        return True, "Streaming starting in background daemon."

    def _start_preview(self, config):
        """Start a lightweight FFmpeg process for dashboard preview."""
        try:
            playlist_txt = os.path.join(LOGS_DIR, "playlist.txt")
            if not os.path.exists(playlist_txt):
                return
            
            resolution = config.get("resolution", "1080x1920")
            w, h = resolution.split("x")
            pw, ph = min(360, int(w)), min(640, int(h))
            
            filter_str = f"scale={pw}:{ph}:force_original_aspect_ratio=decrease,pad={pw}:{ph}:0:0:black,fps=2"
            drawtext_args = overlay_renderer.get_ffmpeg_drawtext_args(config)
            if drawtext_args:
                filter_str += "," + ",".join(drawtext_args)
            
            cmd = [
                "ffmpeg", "-y",
                "-re",
                "-f", "concat", "-safe", "0",
                "-stream_loop", "-1",
                "-i", playlist_txt,
                "-vf", filter_str,
                "-update", "1",
                "-q:v", "3",
                "/tmp/preview.jpg",
            ]
            
            self.preview_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info("Preview process started (360x640 @ 2fps, continuous)")
        except Exception as e:
            logger.debug(f"Preview start error: {e}")

    def _stop_preview(self):
        if self.preview_process:
            self.preview_process.terminate()
            try:
                self.preview_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.preview_process.kill()
            self.preview_process = None

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
            self.is_running = False
            self.status = "STOPPED"

        # Stop preview process
        self._stop_preview()

        # Kill ALL FFmpeg processes to prevent stale processes
        subprocess.run(["pkill", "-f", "ffmpeg"], capture_output=True)
        time.sleep(1)

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

            # Auto-fetch fresh key on every reconnect cycle (keys are single-use)
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

            try:
                cmd = self.build_ffmpeg_command(config)
                logger.info(f"Launching FFmpeg Stream process (Target: {config.get('rtmp_url')})...")

                with self.lock:
                    self.status = "STREAMING"
                    self.is_running = True
                    self.start_time = time.time()
                    overlay_renderer.set_stream_start(self.start_time)

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

            if config.get("auto_reconnect", True):
                self.reconnect_count += 1
                logger.info(f"Auto-reconnecting stream (Attempt #{self.reconnect_count})... Waiting 5 seconds.")
                with self.lock:
                    self.status = "RECONNECTING"
                time.sleep(5)
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
                "ai_engine": ai_engine.get_telemetry(),
                "overlay_renderer": overlay_renderer.get_telemetry(),
            }
            return telemetry


# Global engine instance
engine = StreamEngine()

if __name__ == "__main__":
    print("Testing StreamEngine initialization...")
    print(json.dumps(engine.get_telemetry(), indent=2))
