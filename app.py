#!/usr/bin/env python3
"""
TikTok Live Dashboard & REST API Web Application
With Automatic TikTok Stream Key Retrieval, AI Response Engine, and Live Comment Handling.
"""

import os
import json
import secrets
import subprocess
import threading
import urllib.request
import urllib.error
import psutil
import logging
from fastapi import FastAPI, HTTPException, UploadFile, File, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, Response, JSONResponse
from pydantic import BaseModel
from typing import Optional

from stream_engine import engine, CONFIG_FILE, MEDIA_DIR, LOGS_DIR
from ai_engine import ai_engine, AIResponseEngine
from tiktok_live_client import live_client, TikTokLiveClientManager
from overlay_renderer import overlay_renderer
from live_studio_scraper import scraper

# Logger setup
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s"
)
logger = logging.getLogger("TikTokLiveApp")

app = FastAPI(title="TikTok Live Control Center")

def _get_api_secret() -> str:
    """Load secret from config.json (or env override)."""
    secret = os.environ.get("API_KEY_SECRET")
    if secret:
        return secret
    try:
        return engine.load_config().get("api_key_secret") or ""
    except Exception:
        return ""

@app.middleware("http")
async def api_auth_middleware(request: Request, call_next):
    path = request.url.path
    # Cho phép truy cập static + root + nginx health
    if (
        path == "/" or
        path.startswith("/static") or
        path.startswith("/api/stream-output")
    ):
        return await call_next(request)
    if path.startswith("/api/"):
        # Whitelist read-only endpoints (public; protected by nginx basic_auth from internet)
        if path in ("/api/status", "/api/stream-output", "/api/preview.jpg", "/api/config"):
            return await call_next(request)
        # Enforce X-API-Key only on WRITE methods (POST/PUT/DELETE/PATCH)
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            expected = _get_api_secret()
            if expected:
                provided = request.headers.get("X-API-Key")
                if provided != expected:
                    return JSONResponse(
                        {"detail": "Unauthorized: valid X-API-Key required"},
                        status_code=403,
                    )
    return await call_next(request)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")

class ConfigModel(BaseModel):
    rtmp_url: str
    stream_key: str
    mode: Optional[str] = "playlist"
    resolution: Optional[str] = "1080x1920"
    fps: Optional[int] = 30
    video_bitrate: Optional[str] = "4000k"
    audio_bitrate: Optional[str] = "128k"
    loop: Optional[bool] = True
    auto_reconnect: Optional[bool] = True
    overlay_text: Optional[str] = ""
    show_clock: Optional[bool] = True
    tiktok_username: Optional[str] = ""
    ai_enabled: Optional[bool] = False
    ai_config: Optional[dict] = None

class TikTokSessionModel(BaseModel):
    session_id: str

class TikTokUserModel(BaseModel):
    username: str
    web_proxy: Optional[str] = None
    ws_proxy: Optional[str] = None

class AIConfigModel(BaseModel):
    enabled: bool
    api_key: str
    model: Optional[str] = "gpt-4o-mini"
    persona: Optional[str] = "assistant"
    base_url: Optional[str] = None
    custom_system_prompt: Optional[str] = None

class LiveConfigModel(BaseModel):
    username: str
    overlays_enabled: Optional[dict] = None

class OverlayTextModel(BaseModel):
    text: str

class PersonaModel(BaseModel):
    persona: str


@app.get("/", response_class=HTMLResponse)
def read_root():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return "<h1>TikTok Live Automated System Server</h1>"

@app.get("/api/status")
def get_status():
    telemetry = engine.get_telemetry()
    cpu_percent = psutil.cpu_percent(interval=0.1)
    ram = psutil.virtual_memory()
    
    telemetry["system"] = {
        "cpu_percent": cpu_percent,
        "ram_percent": ram.percent,
        "ram_used_mb": round(ram.used / (1024 * 1024), 1),
        "ram_total_mb": round(ram.total / (1024 * 1024), 1)
    }
    return telemetry

@app.get("/api/preview.jpg")
def get_preview():
    preview_path = "/tmp/preview.jpg"
    if os.path.exists(preview_path):
        return FileResponse(preview_path, media_type="image/jpeg")
    return Response(status_code=204)

@app.get("/api/config")
def get_config():
    return engine.load_config()

@app.post("/api/config")
def save_config(cfg: ConfigModel):
    new = cfg.dict()
    old_path = CONFIG_FILE
    old = {}
    if os.path.exists(old_path):
        with open(old_path, "r") as f:
            old = json.load(f)
    writable = {"resolution","video_bitrate","audio_bitrate","fps","mode",
                "loop","auto_reconnect","overlay_text","show_clock","ai_enabled","ai_config"}
    for k in writable:
        v = new.get(k)
        if v is not None and v != "":
            old[k] = v
    if not old.get("api_key_secret"):
        old["api_key_secret"] = secrets.token_hex(32)
    with open(old_path, "w") as f:
        json.dump(old, f, indent=2)
    engine.load_config()
    return {"success": True, "message": "Configuration saved successfully!"}

def _extract_tt_target_idc(resp) -> Optional[str]:
    """Extract tt-target-idc cookie value from urllib HTTP response headers."""
    try:
        cookies = resp.headers.get_all("Set-Cookie") or []
        for cookie in cookies:
            for part in cookie.split(";"):
                part = part.strip()
                if part.startswith("tt-target-idc="):
                    val = part.split("=", 1)[1].strip()
                    if val:
                        return val
    except Exception:
        pass
    return None


@app.post("/api/tiktok/fetch-stream-key")
def fetch_tiktok_stream_key(req: TikTokSessionModel):
    sess_id = req.session_id.strip()
    if not sess_id:
        raise HTTPException(status_code=400, detail="Vui lòng nhập TikTok Session ID!")
    
    # Handle full cookie string or just sessionid
    if "sessionid=" in sess_id:
        cookie_str = sess_id  # Use full cookie string
        # Extract just the sessionid value for the cookie
        for item in sess_id.split(";"):
            item = item.strip()
            if item.startswith("sessionid=") and not item.startswith("sessionid_ss="):
                sess_id = item.split("=", 1)[1].strip()
                break
    else:
        cookie_str = f"sessionid={sess_id}; sessionid_ss={sess_id}; sid_api={sess_id}; sid_tt={sess_id}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Cookie": cookie_str,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,vi-VN;q=0.8,vi;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://www.tiktok.com/",
        "Origin": "https://www.tiktok.com",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Connection": "keep-alive",
    }

    url = "https://webcast.tiktok.com/webcast/room/create/"
    
    post_data = "device_platform=web&aid=1988&app_language=en&app_name=tiktok_web&browser_name=Mozilla&browser_version=5.0&channel=googleios&api_service_version=2&live_type=1&stream_type=push&mode=web&quality=normal"
    
    try:
        req_obj = urllib.request.Request(url, data=post_data.encode(), headers=headers)
        with urllib.request.urlopen(req_obj, timeout=15) as resp:
            raw = resp.read().decode()
            data = json.loads(raw)
            
            stream_info = data.get("data", {}).get("stream_url", {})
            # Prefer RTMPS (SSL) over plain RTMP for TikTok streaming
            push_urls = stream_info.get("push_urls", [])
            rtmp_push_url = push_urls[0] if push_urls else stream_info.get("rtmp_push_url", "")

            if rtmp_push_url:
                if "/live/" in rtmp_push_url:
                    parts = rtmp_push_url.rsplit("/live/", 1)
                    rtmp_url = parts[0] + "/live/"
                    stream_key = parts[1]
                else:
                    parts = rtmp_push_url.rsplit("/", 1)
                    rtmp_url = parts[0] + "/"
                    stream_key = parts[1]

                current_cfg = engine.load_config()
                current_cfg["rtmp_url"] = rtmp_url
                current_cfg["stream_key"] = stream_key
                _tt = _extract_tt_target_idc(resp)
                if _tt:
                    current_cfg["tiktok_tt_target_idc"] = _tt
                    logger.info(f"Extracted tt-target-idc: {_tt}")
                with open(CONFIG_FILE, "w") as f:
                    json.dump(current_cfg, f, indent=2)

                return {
                    "success": True,
                    "rtmp_url": rtmp_url,
                    "stream_key": stream_key,
                    "message": "Stream Key & Server URL extracted successfully!"
                }
            else:
                prompts = data.get("data", {}).get("prompts", "Account may not have Live Studio/RTMP enabled or Session ID is invalid.")
                raise HTTPException(status_code=400, detail=f"Unable to retrieve stream key. Hãy kiểm tra:\n1. Session ID còn hạn không?\n2. Tài khoản đã bật tính năng Live chưa?\n3. Tài khoản có đủ 1000 followers và >18 tuổi?\nChi tiết: {prompts}")
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode()[:200]
        except:
            pass
        # Fallback: try browser automation if direct API fails
        logger.warning(f"Direct API failed with HTTP {e.code}, trying browser automation...")
        if scraper.available:
            result = scraper.fetch_stream_key_with_session(sess_id)
            if result:
                current_cfg = engine.load_config()
                current_cfg["rtmp_url"] = result["rtmp_url"]
                current_cfg["stream_key"] = result["stream_key"]
                if result.get("tt_target_idc"):
                    current_cfg["tiktok_tt_target_idc"] = result["tt_target_idc"]
                with open(CONFIG_FILE, "w") as f:
                    json.dump(current_cfg, f, indent=2)
                return {
                    "success": True,
                    "rtmp_url": result["rtmp_url"],
                    "stream_key": result["stream_key"],
                    "message": "Stream Key lấy qua browser automation (Live Studio) thành công!"
                }
        raise HTTPException(status_code=400, detail=f"TikTok API error ({e.code}): Session ID hết hạn/không đúng.\nChi tiết: {error_body[:100]}\n\n💡 Gợi ý: Hãy thử Cách 2 - lấy stream key thủ công từ https://tiktok.com/live/studio")
    except urllib.error.URLError as e:
        raise HTTPException(status_code=400, detail=f"Không thể kết nối tới TikTok API: {str(e)}\nKiểm tra kết nối mạng.")
    except Exception as e:
        # Fallback: try browser automation if direct API fails
        logger.warning(f"Direct API failed, trying browser automation: {e}")
        if scraper.available:
            result = scraper.fetch_stream_key_with_session(sess_id)
            if result:
                current_cfg = engine.load_config()
                current_cfg["rtmp_url"] = result["rtmp_url"]
                current_cfg["stream_key"] = result["stream_key"]
                if result.get("tt_target_idc"):
                    current_cfg["tiktok_tt_target_idc"] = result["tt_target_idc"]
                with open(CONFIG_FILE, "w") as f:
                    json.dump(current_cfg, f, indent=2)
                return {
                    "success": True,
                    "rtmp_url": result["rtmp_url"],
                    "stream_key": result["stream_key"],
                    "message": "Stream Key lấy qua Live Studio browser automation thành công!"
                }
        raise HTTPException(status_code=400, detail=f"Lỗi khi lấy stream key:\n{str(e)}\n\n💡 Gợi ý: Hãy thử Cách 2 - lấy stream key thủ công từ https://tiktok.com/live/studio")


@app.post("/api/tiktok/extract-session")
def extract_session():
    """Auto-extract TikTok session ID from Chromium browser profile."""
    import time
    session_id = None
    method = None
    try:
        from extract_session import get_tiktok_cookies_from_chromium, get_tiktok_cookies_from_playwright
        start = time.time()
        session_id = get_tiktok_cookies_from_chromium()
        if session_id:
            method = "chromium_profile"
        if not session_id:
            session_id = get_tiktok_cookies_from_playwright()
            if session_id:
                method = "playwright"
        elapsed = round(time.time() - start, 1)
    except Exception as e:
        return {"success": False, "message": f"Lỗi: {str(e)}", "elapsed": elapsed if 'elapsed' in dir() else 0}

    if session_id:
        config = engine.load_config()
        config["tiktok_session"] = session_id
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
        logger.info(f"Session ID extracted via {method} in {elapsed}s")
        return {
            "success": True,
            "session_id": "••••••••" + session_id[-24:] if len(session_id) > 24 else "••••" + session_id[-4:],
            "full_session": session_id,
            "method": method,
            "elapsed_seconds": elapsed
        }
    return {"success": False, "message": "Không tìm thấy session ID. Đảm bảo đã đăng nhập TikTok trên Chromium/Firefox.", "elapsed": round(time.time() - start, 1)}


@app.get("/api/tiktok/stream-key-status")
def get_stream_key_status():
    """Check stream key configuration status."""
    config = engine.load_config()
    has_key = bool(config.get("stream_key", "").strip())
    return {
        "has_stream_key": has_key,
        "rtmp_url": config.get("rtmp_url", ""),
        "stream_key": "••••••••" + config.get("stream_key", "")[-4:] if has_key else "",
        "tt_target_idc": config.get("tiktok_tt_target_idc", ""),
        "scraper_available": scraper.available,
        "scraper_status": scraper.get_telemetry(),
    }


@app.post("/api/stream/start")
def start_stream():
    success, msg = engine.start_stream()
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": msg}

@app.post("/api/stream/stop")
def stop_stream():
    success, msg = engine.stop_stream()
    return {"success": True, "message": msg}

@app.post("/api/stream/restart")
def restart_stream():
    engine.stop_stream()
    success, msg = engine.start_stream()
    return {"success": True, "message": "Stream restarted successfully!"}

@app.get("/api/media")
def list_media():
    files = []
    if os.path.exists(MEDIA_DIR):
        for fname in sorted(os.listdir(MEDIA_DIR)):
            fpath = os.path.join(MEDIA_DIR, fname)
            if os.path.isfile(fpath) and not fname.startswith("."):
                size_mb = round(os.path.getsize(fpath) / (1024 * 1024), 2)
                files.append({"name": fname, "size_mb": size_mb})
    return {"files": files}

@app.post("/api/media/select")
def select_media(filename: str):
    """Select which video file to use for streaming."""
    current_cfg = engine.load_config()
    current_cfg["active_media"] = filename
    with open(CONFIG_FILE, "w") as f:
        json.dump(current_cfg, f, indent=2)
    return {"success": True, "active_media": filename}

class PlaylistModel(BaseModel):
    playlist: list[str] = []

@app.post("/api/media/playlist")
def set_playlist(req: PlaylistModel):
    """Set the playlist of videos for streaming."""
    current_cfg = engine.load_config()
    current_cfg["media_playlist"] = req.playlist
    if req.playlist:
        current_cfg["active_media"] = req.playlist[0]
    with open(CONFIG_FILE, "w") as f:
        json.dump(current_cfg, f, indent=2)
    return {"success": True, "playlist": req.playlist}

@app.get("/api/media/playlist")
def get_playlist():
    """Get the current playlist."""
    current_cfg = engine.load_config()
    return {"playlist": current_cfg.get("media_playlist", [])}

@app.post("/api/media/upload")
async def upload_media(file: UploadFile = File(...)):
    file_size = 0
    chunk_size = 10 * 1024 * 1024  # 10MB chunks
    max_size = 600 * 1024 * 1024   # 600MB
    target_path = os.path.join(MEDIA_DIR, file.filename)
    
    with open(target_path, "wb") as buffer:
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            file_size += len(chunk)
            if file_size > max_size:
                buffer.close()
                os.remove(target_path)
                raise HTTPException(status_code=413, detail="File quá lớn (max 600MB). Nén video hoặc cắt ngắn.")
            buffer.write(chunk)

    # Response immediately after file saved - convert runs fully async (non-blocking)
    import threading as _threading

    def _bg_convert():
        try:
            detected_codec = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "stream=codec_name", "-of", "csv=p=0", target_path],
                capture_output=True, text=True, timeout=15
            ).stdout.strip()
            if detected_codec not in ("h264", "avc"):
                converted_name = "converted_" + file.filename
                converted_path = os.path.join(MEDIA_DIR, converted_name)
                subprocess.run([
                    "ffmpeg", "-y", "-i", target_path,
                    "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:0:0:black,fps=30",
                    "-c:v", "libx264", "-preset", "superfast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                    converted_path
                ], capture_output=True, timeout=1800)
                if os.path.exists(converted_path) and os.path.getsize(converted_path) > 100000:
                    os.replace(converted_path, target_path)
                    logger.info(f"Video converted to H.264: {file.filename}")
        except Exception as e:
            logger.warning(f"Background convert failed for {file.filename}: {e}")

    _threading.Thread(target=_bg_convert, daemon=True).start()

    # Auto-tag product_tag tu key tuong ung trong ten file upload
    _filename_tag = {
        "áo thun size m": "áo thun", "ao thun size m": "áo thun",
        "áo thun": "áo thun", "ao thun": "áo thun",
        "cạo râu điện": "cạo râu", "cạo râu": "cạo râu",
    }
    _fname = (file.filename or "").lower().replace("_", " ").replace("-", " ")
    matched_tag = None
    for kw, tag in sorted(_filename_tag.items(), key=lambda x: -len(x[0])):
        if kw in _fname:
            matched_tag = tag
            break
    if matched_tag:
        _tags_path = os.path.join(LOGS_DIR, "video_tags.json")
        tags = {}
        if os.path.exists(_tags_path):
            try:
                tags = json.load(open(_tags_path))
            except Exception:
                tags = {}
        if file.filename not in tags:
            tags[file.filename] = {"product_tag": matched_tag, "cart_link": None, "keywords": matched_tag}
        with open(_tags_path, "w") as f:
            json.dump(tags, f, indent=2)
        logger.info(f"Auto-tagged {file.filename} -> product_tag={matched_tag}")

    return {"success": True, "filename": file.filename, "size_mb": round(file_size / 1048576, 2), "converted": False, "message": "Đã lưu file, đang convert nền..."}


class TikTokUrlModel(BaseModel):
    url: str


def _tiktok_to_playlist(filename: str):
    """Add a downloaded video to media_playlist in config."""
    config = engine.load_config()
    playlist = config.get("media_playlist", [])
    if filename not in playlist:
        playlist.append(filename)
    config["media_playlist"] = playlist
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


@app.post("/api/media/url")
async def download_tiktok_video(req: TikTokUrlModel):
    """Download TikTok video from URL and add to playlist."""
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL không được để trống")

    # Generate unique filename
    safe_name = url.replace("https://", "").replace("http://", "")
    safe_name = "".join(c if c.isalnum() else "_" for c in safe_name)[:60]
    raw_filename = f"tiktok_{safe_name}.mp4"
    target_path = os.path.join(MEDIA_DIR, raw_filename)

    def _bg_download():
        video_url = None
        download_attempts = [
            "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
            "best",
            "mp4",
        ]
        try:
            # Method 1: try yt-dlp first (retry with different formats)
            from yt_dlp import YoutubeDL
            import random, time as dl_time
            for attempt, fmt in enumerate(download_attempts):
                ydl_opts = {
                    "format": fmt,
                    "outtmpl": target_path,
                    "quiet": True,
                    "no_warnings": True,
                    "socket_timeout": 90,
                    "retries": 2,
                    "impersonate": "chrome",
                }
                try:
                    with YoutubeDL(ydl_opts) as ydl:
                        ydl.download([url])
                    if os.path.exists(target_path) and os.path.getsize(target_path) > 100000:
                        logger.info(f"TikTok video downloaded via yt-dlp (format {attempt+1}): {raw_filename}")
                        video_url = target_path
                        break
                    if os.path.exists(target_path):
                        os.remove(target_path)
                except Exception as yt_err:
                    logger.warning(f"yt-dlp attempt {attempt+1} failed: {yt_err}")
                    if os.path.exists(target_path):
                        os.remove(target_path)
                dl_time.sleep(random.uniform(3, 6))
        except Exception:
            pass

        # Method 2: Playwright fallback (intercept network for video URL)
        if not video_url:
            try:
                from playwright.sync_api import sync_playwright
                import requests as req_lib

                with sync_playwright() as p:
                    user_data_dir = os.path.expanduser("~/.var/app/org.chromium.Chromium/config/chromium")
                    if not os.path.exists(user_data_dir):
                        user_data_dir = os.path.expanduser("~/.config/chromium")

                    launch_kwargs = dict(headless=True, args=['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage'])
                    if os.path.exists(user_data_dir):
                        browser = p.chromium.launch_persistent_context(user_data_dir, **launch_kwargs)
                    else:
                        browser = p.chromium.launch(**launch_kwargs)

                    page = browser.new_page()
                    video_src = None

                    def _catch_response(response):
                        nonlocal video_src
                        if video_src:
                            return
                        rurl = response.url
                        if rurl and (".mp4" in rurl or "video" in rurl.lower() or "play_addr" in rurl.lower()):
                            if "mime_type=video" in rurl or ".mp4" in rurl:
                                video_src = rurl

                    page.on("response", _catch_response)
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(8000)
                    browser.close()

                    # Also parse page HTML for embedded video URLs
                    if not video_src:
                        content = page.content() if 'page' in dir() else ""
                        import re
                        matches = re.findall(r'https://[^\s"\']+\.mp4[^\s"\']*', content)
                        if matches:
                            video_src = matches[0]

                    if video_src:
                        logger.info(f"Playwright found video URL, downloading...")
                        resp = req_lib.get(video_src, stream=True, timeout=120, headers={
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
                        })
                        if resp.status_code == 200:
                            with open(target_path, "wb") as f:
                                for chunk in resp.iter_content(chunk_size=1024*1024):
                                    if chunk:
                                        f.write(chunk)
                            logger.info(f"Video downloaded via Playwright fallback")
            except Exception as pw_err:
                logger.error(f"Playwright fallback also failed: {pw_err}")

        if not os.path.exists(target_path):
            logger.error(f"Failed to download video from URL: {url}")
            return

        # Convert to H.264 1080x1920 30fps for concat compatibility
        try:
            converted_name = "converted_" + raw_filename
            converted_path = os.path.join(MEDIA_DIR, converted_name)
            subprocess.run([
                "ffmpeg", "-y", "-i", target_path,
                "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:0:0:black,fps=30",
                "-c:v", "libx264", "-preset", "superfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                converted_path
            ], capture_output=True, timeout=1800)

            if os.path.exists(converted_path) and os.path.getsize(converted_path) > 100000:
                # Replace original with H.264 version, use same raw_filename
                os.replace(converted_path, target_path)
                logger.info(f"TikTok video downloaded + converted: {raw_filename}")
            else:
                logger.warning(f"TikTok conversion failed, using raw: {raw_filename}")

            _tiktok_to_playlist(raw_filename)
        except Exception as e:
            logger.error(f"TikTok download error: {e}")

    threading.Thread(target=_bg_download, daemon=True).start()
    return {"success": True, "filename": raw_filename, "message": "Đang tải video từ URL... sẽ tự động thêm vào playlist khi hoàn tất!"}

@app.delete("/api/media/{filename}")
def delete_media(filename: str):
    target_path = os.path.join(MEDIA_DIR, filename)
    if os.path.exists(target_path):
        os.remove(target_path)
        return {"success": True, "message": f"File {filename} deleted."}
    raise HTTPException(status_code=404, detail="File not found")

@app.post("/api/media/{filename}/convert")
def convert_media(filename: str):
    target_path = os.path.join(MEDIA_DIR, filename)
    if not os.path.exists(target_path):
        raise HTTPException(status_code=404, detail="File not found")
    codec = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "stream=codec_name", "-of", "csv=p=0", target_path], capture_output=True, text=True, timeout=10)
    if codec.stdout.strip() in ("h264", "avc"):
        return {"success": True, "already_h264": True, "message": "Video đã định dạng H.264 rồi"}
    converted_path = os.path.join(MEDIA_DIR, "converted_" + filename)
    subprocess.run(["ffmpeg", "-y", "-i", target_path,
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:0:0:black",
        "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", converted_path],
        capture_output=True, timeout=120)
    if os.path.exists(converted_path) and os.path.getsize(converted_path) > 100000:
        os.replace(converted_path, target_path)
        return {"success": True, "message": "Đã convert sang H.264 1080x1920"}
    return {"success": False, "error": "Convert thất bại"}

@app.get("/api/logs")
def get_logs():
    log_file = os.path.join(LOGS_DIR, "stream.log")
    if os.path.exists(log_file):
        with open(log_file, "r") as f:
            lines = f.readlines()
            return {"logs": lines[-100:]}
    return {"logs": ["No logs recorded yet."]}


# ===== AI ENGINE APIS =====

@app.get("/api/ai/status")
def get_ai_status():
    """Get AI engine status and telemetry."""
    return ai_engine.get_telemetry()

@app.post("/api/ai/configure")
def configure_ai(config: AIConfigModel):
    """Configure AI engine with API key and settings."""
    ai_engine.configure(config.api_key, config.model, config.persona, config.base_url, config.custom_system_prompt)
    ai_engine.set_enabled(config.enabled)
    
    # Save to config file
    current_cfg = engine.load_config()
    if not current_cfg.get("api_key_secret"):
        import secrets as _secrets
        current_cfg["api_key_secret"] = _secrets.token_hex(16)
    current_cfg["ai_enabled"] = config.enabled
    current_cfg["ai_config"] = {
        "api_key": config.api_key,
        "model": config.model,
        "persona": config.persona,
        "base_url": config.base_url,
        "custom_system_prompt": config.custom_system_prompt,
        "enabled": config.enabled
    }
    with open(CONFIG_FILE, "w") as f:
        json.dump(current_cfg, f, indent=2)
    
    return {"success": True, "message": f"AI engine configured with persona: {config.persona}"}

@app.post("/api/ai/toggle")
def toggle_ai(enabled: bool):
    """Enable or disable AI engine."""
    ai_engine.set_enabled(enabled)
    return {"success": True, "message": f"AI engine {'enabled' if enabled else 'disabled'}"}

@app.post("/api/ai/persona")
def set_ai_persona(model: PersonaModel):
    """Set AI persona (coder, salesperson, assistant)."""
    ai_engine.set_persona(model.persona)
    return {"success": True, "message": f"Persona changed to: {model.persona}"}

@app.post("/api/ai/response")
def generate_ai_response(comment: str, username: str = "Viewer"):
    """Generate an AI response to a comment (for testing)."""
    if not ai_engine.enabled:
        raise HTTPException(status_code=400, detail="AI engine is not enabled")
    response = ai_engine.generate_response(comment, username)
    if response:
        return {"success": True, "response": response}
    return {"success": False, "message": "No response generated"}

@app.get("/api/ai/responses")
def get_ai_responses(count: int = 20):
    """Get cached AI responses."""
    return {"responses": ai_engine.get_cached_responses(count)}

@app.delete("/api/ai/cache")
def clear_ai_cache():
    """Clear AI response cache."""
    ai_engine.clear_cache()
    return {"success": True, "message": "AI cache cleared"}


# ===== TIKTOK LIVE CLIENT APIS =====

@app.get("/api/live/status")
def get_live_status():
    """Get TikTok live client status."""
    return live_client.get_telemetry()

@app.post("/api/live/connect")
def connect_live_client(req: TikTokUserModel):
    """Connect to a TikTok live room by username."""
    if not live_client.is_available():
        raise HTTPException(status_code=500, detail="TikTokLive library not installed")
    
    username = req.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    current_cfg = engine.load_config()
    live_client.configure(username, req.web_proxy, req.ws_proxy)
    if not live_client.client:
        raise HTTPException(status_code=500, detail=live_client.last_error or "TikTokLive client init failed")

    # Save to config (already loaded above)
    current_cfg["tiktok_username"] = username
    with open(CONFIG_FILE, "w") as f:
        json.dump(current_cfg, f, indent=2)
    
    connected = live_client.connect_async()
    if connected:
        return {"success": True, "message": f"Connected to TikTok live: @{username}"}
    else:
        return {"success": False, "message": f"Failed to connect to @{username}: {live_client.last_error}"}

@app.post("/api/live/disconnect")
def disconnect_live_client():
    """Disconnect from TikTok live room."""
    live_client.disconnect()
    return {"success": True, "message": "Disconnected from TikTok live"}

@app.post("/api/live/reconnect")
def reconnect_live_client():
    """Reconnect to TikTok live room."""
    if not live_client.username:
        raise HTTPException(status_code=400, detail="No username configured")
    connected = live_client.reconnect()
    if connected:
        return {"success": True, "message": f"Reconnected to @{live_client.username}"}
    return {"success": False, "message": f"Failed to reconnect: {live_client.last_error}"}

@app.get("/api/live/comments")
def get_live_comments(count: int = 20):
    """Get recent comments from TikTok live."""
    return {"comments": live_client.get_recent_comments(count)}

class ForwardCommentModel(BaseModel):
    username: str
    comment: str
    product_tag: Optional[str] = None
    cart_link: Optional[str] = None

@app.get("/api/live/session-info")
def live_session_info():
    """
    Trả thông tin live session để comment_forwarder.py kết nối từ bất kỳ máy nào
    (máy local / freeyou.win / VPS) mà không cần cấu hình username thủ công.
    """
    cfg = engine.load_config()
    return {
        "username": (cfg.get("tiktok_username") or "").strip().lstrip("@"),
        "server_url": None,
        "tiktok_session": cfg.get("tiktok_session", ""),
        "tiktok_tt_target_idc": cfg.get("tiktok_tt_target_idc", ""),
        "live_client_connected": live_client.is_connected,
    }

@app.get("/api/config")
def get_public_config():
    """
    Trả cấu hình cần thiết cho frontend (auth bởi nginx basic_auth).
    Bao gồm api_key_secret để frontend gửi header X-API-Key cho các endpoint nhạy.
    Chỉ truy cập qua nginx proxy (port 8888 localhost không public).
    """
    cfg = engine.load_config()
    return {
        "api_key_secret": cfg.get("api_key_secret", ""),
        "tiktok_username": cfg.get("tiktok_username", ""),
        "ai_enabled": bool(cfg.get("ai_enabled", False)),
    }

@app.post("/api/live/comment-forward")
def forward_comment(req: ForwardCommentModel):
    """
    Receive a comment forwarded by an external listener (comment_forwarder.py)
    on a clean IP / via proxy. Server ADDS user comment to overlay scroll,
    then generates an AI response and RETURNS it to the forwarder - which will
    post the reply onto TikTok's comment panel (NOT rendered onto video).
    """
    if not live_client.is_available():
        raise HTTPException(status_code=500, detail="TikTokLive library not available on server")
    # Chỉ telemetry (không render lên video) — forwarder reply lên comment panel.
    live_client.inject_comment(req.username, req.comment, trigger_ai=False)

    # Generate AI response - returned to forwarder so it can reply on TikTok comment panel
    ai_response = ""
    if ai_engine.is_available() and ai_engine.enabled:
        ai_response = ai_engine.generate_response(req.comment, req.username, product_tag=req.product_tag) or ""

    return {
        "success": True,
        "message": f"Comment forwarded by @{req.username}",
        "ai_response": ai_response,
    }

class TagMediaModel(BaseModel):
    filename: str
    product_tag: str
    product_url: Optional[str] = None
    cart_link: Optional[str] = None
    product_id: Optional[str] = None
    shop_id: Optional[str] = None
    keywords: Optional[str] = None


@app.post("/api/media/tag")
def tag_media(model: TagMediaModel):
    path = os.path.join(LOGS_DIR, "video_tags.json")
    tags = {}
    if os.path.exists(path):
        try:
            tags = json.load(open(path))
        except Exception:
            tags = {}
    tags[model.filename] = {
        "product_tag": model.product_tag,
        "product_url": model.product_url,
        "cart_link": model.cart_link,
        "product_id": model.product_id,
        "shop_id": model.shop_id,
        "keywords": model.keywords or "",
    }
    with open(path, "w") as f:
        json.dump(tags, f, indent=2)
    return {"success": True, "tagged": model.filename, "product_tag": model.product_tag, "product_url": model.product_url}


@app.get("/api/live/active-media")
def get_active_media():
    cfg = engine.load_config()
    filename = cfg.get("active_media")
    path = os.path.join(LOGS_DIR, "video_tags.json")
    tag = {}
    if filename and os.path.exists(path):
        try:
            tag = json.load(open(path)).get(filename, {})
        except Exception:
            tag = {}
    return {
        "filename": filename,
        "product_tag": tag.get("product_tag"),
        "product_url": tag.get("product_url"),
        "product_id": tag.get("product_id"),
        "shop_id": tag.get("shop_id"),
        "cart_link": tag.get("cart_link"),
        "keywords": tag.get("keywords", ""),
    }


@app.post("/api/tiktok/showcase/add")
def add_to_showcase(filename: Optional[str] = None, product_id: Optional[str] = None,
                    shop_id: Optional[str] = None):
    """Them san pham vua roii vao Showcase cua Creator (Affiliate Creator API).
    Tu dong lay product_id/shop_id tu video tag cua active_media, hoac dung product_id truyen dau vao.
    Can TIKTOK_ACCESS_TOKEN env de xac thuc."""
    import requests as _req
    token = os.environ.get("TIKTOK_ACCESS_TOKEN", "")
    if not token:
        return {"success": False, "message": "Chua cau hinh TIKTOK_ACCESS_TOKEN (Creator API token)"}

    cfg = engine.load_config()
    active = filename or cfg.get("active_media")
    tags_path = os.path.join(LOGS_DIR, "video_tags.json")
    tag = {}
    if active and os.path.exists(tags_path):
        try:
            tag = json.load(open(tags_path)).get(active, {})
        except Exception:
            tag = {}
    pid = product_id or tag.get("product_id")
    sid = shop_id or tag.get("shop_id")
    if not pid or not sid:
        return {"success": False, "message": "Can product_id + shop_id. Gan tag: POST /api/media/tag"}

    resp = _req.post(
        "https://business-api.tiktok.com/affiliate_creator/202405/showcases/products/add",
        json={"product_id": pid, "shop_id": sid},
        headers={"X-Access-Token": token, "Content-Type": "application/json"},
        timeout=20,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text[:300]}
    if resp.status_code == 200 and data.get("data", {}).get("success") is not False:
        return {"success": True, "message": "Da them san pham vao Showcase (nhan len duoi live mau vang)", "response": data}
    return {"success": False, "message": f"Showcase add failed HTTP {resp.status_code}", "response": data}


@app.get("/api/tiktok/showcase")
def get_showcase_products():
    """Lay danh sach san pham trong Showcase cua Creator."""
    import requests as _req
    token = os.environ.get("TIKTOK_ACCESS_TOKEN", "")
    if not token:
        return {"products": [], "message": "Chua cau hinh TIKTOK_ACCESS_TOKEN"}
    resp = _req.get(
        "https://business-api.tiktok.com/affiliate_creator/202405/showcases/products",
        headers={"X-Access-Token": token, "Content-Type": "application/json"},
        timeout=20,
    )
    try:
        return {"products": resp.json().get("data", {}).get("product_list", []), "http": resp.status_code}
    except Exception:
        return {"products": [], "error": resp.text[:200]}

@app.get("/api/live/gifts")
def get_live_gifts(count: int = 10):
    """Get recent gifts from TikTok live."""
    return {"gifts": live_client.get_recent_gifts(count)}


# ===== OVERLAY RENDERER APIS =====

@app.get("/api/overlay/status")
def get_overlay_status():
    """Get overlay renderer status."""
    return overlay_renderer.get_telemetry()

@app.post("/api/overlay/enable")
def enable_overlay(enabled: bool):
    """Enable or disable overlay renderer."""
    overlay_renderer.set_enabled(enabled)
    
    # Save to config
    current_cfg = engine.load_config()
    current_cfg["overlay_enabled"] = enabled
    with open(CONFIG_FILE, "w") as f:
        json.dump(current_cfg, f, indent=2)
    
    return {"success": True, "message": f"Overlay {'enabled' if enabled else 'disabled'}"}

@app.post("/api/overlay/text")
def set_overlay_text(model: OverlayTextModel):
    """Set overlay text."""
    overlay_renderer.set_overlay_text(model.text)
    return {"success": True, "message": "Overlay text updated"}

@app.post("/api/overlay/comment")
def add_overlay_comment(username: str, comment: str, is_ai_response: bool = False):
    """Add a comment to the overlay display."""
    overlay_renderer.add_comment(username, comment, is_ai_response)
    return {"success": True, "message": "Comment added to overlay"}

@app.post("/api/overlay/config")
def configure_overlays(enabled: bool, comment_scroll: bool = True, ai_response: bool = True, 
                       viewer_count: bool = True, stats_panel: bool = True, clock: bool = True):
    """Configure which overlays are enabled."""
    config = {
        "clock": clock,
        "comment_scroll": comment_scroll,
        "ai_response": ai_response,
        "viewer_count": viewer_count,
        "stats_panel": stats_panel,
    }
    overlay_renderer.configure_overlays(config)
    return {"success": True, "message": "Overlay configuration updated"}


if __name__ == "__main__":
    import uvicorn
    # Auto-load AI config from config.json on startup
    try:
        _startup_cfg = engine.load_config()
        _ai = _startup_cfg.get("ai_config", {})
        if _ai.get("enabled") and _ai.get("api_key"):
            ai_engine.configure(
                _ai["api_key"], _ai.get("model"), _ai.get("persona"),
                _ai.get("base_url"), _ai.get("custom_system_prompt")
            )
            ai_engine.set_enabled(True)
            print("AI engine auto-configured from config.json.", flush=True)
    except Exception as e:
        print(f"Startup AI config load failed: {e}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=8888)
