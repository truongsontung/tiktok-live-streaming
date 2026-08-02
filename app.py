#!/usr/bin/env python3
"""
TikTok Live Dashboard & REST API Web Application
With Automatic TikTok Stream Key Retrieval and Live Comment Handling.
"""

import os
import json
import gzip
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
from contextlib import asynccontextmanager

from stream_engine import engine, CONFIG_FILE, MEDIA_DIR, LOGS_DIR
from tiktok_live_client import live_client, TikTokLiveClientManager
from overlay_renderer import overlay_renderer
from live_studio_scraper import scraper

# Logger setup
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s"
)
logger = logging.getLogger("TikTokLiveApp")

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    try:
        engine.stop_stream()
    except Exception as e:
        logger.warning(f"Shutdown cleanup error: {e}")

app = FastAPI(title="TikTok Live Control Center", lifespan=lifespan)

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

THUMBS_DIR = os.path.join(STATIC_DIR, "thumbs")
os.makedirs(THUMBS_DIR, exist_ok=True)

CHUNK_SIZE = 16 * 1024 * 1024  # 16MB per chunk for chunked/resumable uploads

# Background thumb extraction (only runs if file doesn't exist)
def _ensure_thumbnail(filename: str):
    thumb_path = os.path.join(THUMBS_DIR, filename + ".jpg")
    if os.path.exists(thumb_path):
        return thumb_path
    video_path = os.path.join(MEDIA_DIR, filename)
    if not os.path.exists(video_path):
        return None
    subprocess.run([
        "ffmpeg", "-y", "-i", video_path,
        "-ss", "00:00:01", "-vframes", "1", "-vf", "scale=240:320:force_original_aspect_ratio=decrease,pad=240:320:0:0:black",
        "-q:v", "3", thumb_path
    ], capture_output=True, timeout=30)
    return thumb_path if os.path.exists(thumb_path) else None

def _process_uploaded_file(filename: str):
    """Start background conversion of an uploaded file: H.264 codec + thumbnail."""
    target_path = os.path.join(MEDIA_DIR, filename)
    _cfg = engine.load_config()
    _target_res = _cfg.get("resolution", "720x1280")
    _tw, _th = _target_res.split("x")

    def _bg_convert():
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", target_path],
                capture_output=True, text=True, timeout=15
            ).stdout.strip().split(",")
            _needs_convert = False
            if len(probe) >= 1:
                _vcodec = probe[0].strip()
                if _vcodec not in ("h264", "avc", "h264 "):
                    _needs_convert = True
            if _needs_convert:
                converted_name = "converted_" + filename
                converted_path = os.path.join(MEDIA_DIR, converted_name)
                subprocess.run([
                    "ffmpeg", "-y", "-i", target_path,
                    "-vf", f"scale={_tw}:{_th}:force_original_aspect_ratio=decrease,pad={_tw}:{_th}:0:0:black,fps=30",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "27",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                    converted_path
                ], capture_output=True, timeout=1800)
                if os.path.exists(converted_path) and os.path.getsize(converted_path) > 100000:
                    os.replace(converted_path, target_path)
                    logger.info(f"Video converted to H.264: {filename}")
            else:
                logger.info(f"Video already H.264 @ {_target_res}, skipping conversion: {filename}")
            _ensure_thumbnail(filename)
        except Exception as e:
            logger.warning(f"Background convert failed for {filename}: {e}")

    threading.Thread(target=_bg_convert, daemon=True).start()

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
    overlay_enabled: Optional[bool] = False
    overlay_config: Optional[dict] = None
    tiktok_username: Optional[str] = ""

class TikTokSessionModel(BaseModel):
    session_id: str

class TikTokUserModel(BaseModel):
    username: str

class LiveConfigModel(BaseModel):
    username: str
    overlays_enabled: Optional[dict] = None

class OverlayTextModel(BaseModel):
    text: str


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
                "loop","auto_reconnect","             overlay_text","show_clock","overlay_enabled","overlay_config","ai_enabled","ai_config"}
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
            raw = resp.read()
            encoding = resp.headers.get("Content-Encoding", "")
            if "gzip" in encoding:
                raw = gzip.decompress(raw)
            raw = raw.decode()
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
            raw_err = e.read()
            if "gzip" in e.headers.get("Content-Encoding", ""):
                raw_err = gzip.decompress(raw_err)
            error_body = raw_err.decode()[:200]
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
                thumb_url = None
                duration = None
                _ensure_thumbnail(fname)
                thumb_path = os.path.join(THUMBS_DIR, fname + ".jpg")
                if os.path.exists(thumb_path):
                    thumb_url = f"static/thumbs/{fname}.jpg"
                try:
                    dur_ret = subprocess.run(
                        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", fpath],
                        capture_output=True, text=True, timeout=10
                    )
                    duration = float(dur_ret.stdout.strip()) if dur_ret.stdout.strip() else None
                except Exception:
                    pass
                files.append({"name": fname, "size_mb": size_mb, "thumb_url": thumb_url, "duration": duration})
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
async def upload_media(request: Request, file: UploadFile = File(...)):
    max_size = 10 * 1024 * 1024 * 1024  # 10GB
    content_length = request.headers.get("Content-Length", "unknown")
    logger.info(f"Upload: filename={file.filename}, Content-Length={content_length}")

    filename = file.filename
    target_path = os.path.join(MEDIA_DIR, filename)

    file_size = 0
    try:
        with open(target_path, "wb") as buffer:
            while True:
                chunk = await file.read(10 * 1024 * 1024)  # 10MB chunks
                if not chunk:
                    break
                file_size += len(chunk)
                if file_size > max_size:
                    buffer.close()
                    os.remove(target_path)
                    raise HTTPException(status_code=413, detail="File quá lớn (max 10GB). Vui lòng nén video hoặc cắt ngắn.")
                buffer.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        if os.path.exists(target_path):
            os.remove(target_path)
        logger.error(f"Upload error for {filename}: {e}")
        raise HTTPException(status_code=500, detail=f"Lỗi tải lên: {str(e)}")

    _process_uploaded_file(filename)
    return {"success": True, "filename": filename, "size_mb": round(file_size / 1048576, 2), "converted": False, "message": "Đã lưu file, đang kiểm tra định dạng nền..."}

@app.post("/api/media/upload-chunk")
async def upload_chunk(request: Request):
    filename = request.query_params.get("filename")
    chunk_index = int(request.query_params.get("chunkIndex", 0))
    total_chunks = int(request.query_params.get("totalChunks", 1))
    upload_target = request.query_params.get("target", "media")  # "media" or "avatar"

    if not filename:
        raise HTTPException(status_code=400, detail="Thiếu filename")
    safe_filename = os.path.basename(filename)

    if upload_target == "avatar":
        target_dir = os.path.join(STATIC_DIR, "avatars")
    else:
        target_dir = MEDIA_DIR
    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, safe_filename)

    chunk_data = await request.body()
    if chunk_index == 0:
        f = open(target_path, "wb")
    else:
        f = open(target_path, "r+b")
    f.seek(chunk_index * CHUNK_SIZE)
    f.write(chunk_data)
    f.close()
    file_size = os.path.getsize(target_path)
    logger.info(f"Upload chunk {chunk_index}/{total_chunks} for {safe_filename} ({len(chunk_data)} bytes, target={upload_target})")

    response_data = {"success": True, "chunkIndex": chunk_index, "file_size": file_size}
    if chunk_index == total_chunks - 1:
        response_data["completed"] = True
        response_data["filename"] = safe_filename
        response_data["size_mb"] = round(file_size / 1048576, 2)
        if upload_target == "media":
            response_data["message"] = "Đã lưu file, đang kiểm tra định dạng nền..."
            _process_uploaded_file(safe_filename)
        else:
            response_data["message"] = "Đã tải avatar lên!"
            overlay_renderer.load_avatar_pool()
    return response_data


@app.get("/api/avatars")
def list_avatars():
    """List all available avatar images."""
    avatar_dir = os.path.join(STATIC_DIR, "avatars")
    avatars = []
    if os.path.exists(avatar_dir):
        for f in sorted(os.listdir(avatar_dir)):
            if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                fpath = os.path.join(avatar_dir, f)
                avatars.append({
                    "name": f,
                    "url": f"static/avatars/{f}",
                    "size_bytes": os.path.getsize(fpath),
                })
    return {"avatars": avatars, "count": len(avatars)}

@app.delete("/api/avatars/{filename}")
def delete_avatar(filename: str):
    """Delete an avatar image."""
    avatar_dir = os.path.join(STATIC_DIR, "avatars")
    safe_name = os.path.basename(filename)
    avatar_path = os.path.join(avatar_dir, safe_name)
    if not os.path.exists(avatar_path):
        raise HTTPException(status_code=404, detail=f"Avatar '{safe_name}' không tìm thấy")
    os.remove(avatar_path)
    overlay_renderer._avatar_cache.pop(avatar_path, None)
    overlay_renderer.load_avatar_pool()
    return {"success": True, "message": f"Đã xóa avatar: {safe_name}"}


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

        # Convert to H.264 at target resolution only if source isn't already compatible
        try:
            _cfg = engine.load_config()
            _target_res = _cfg.get("resolution", "720x1280")
            _target_w, _target_h = _target_res.split("x")

            _codec = subprocess.run(
                ["ffprobe", "-v", "quiet", "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", target_path],
                capture_output=True, text=True, timeout=10
            ).stdout.strip().split(",")

            _needs_convert = False
            if len(_codec) >= 1:
                _vcodec = _codec[0].strip()
                # Only check codec — FFmpeg stream handles scaling/padding
                if _vcodec not in ("h264", "avc", "h264 "):
                    _needs_convert = True  # Non-H.264 → must convert

            if _needs_convert:
                converted_name = "converted_" + raw_filename
                converted_path = os.path.join(MEDIA_DIR, converted_name)
                subprocess.run([
                    "ffmpeg", "-y", "-i", target_path,
                    "-vf", f"scale={_target_w}:{_target_h}:force_original_aspect_ratio=decrease,pad={_target_w}:{_target_h}:0:0:black,fps=30",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "27",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                    converted_path
                ], capture_output=True, timeout=1800)

                if os.path.exists(converted_path) and os.path.getsize(converted_path) > 100000:
                    os.replace(converted_path, target_path)
                    logger.info(f"TikTok video downloaded + converted: {raw_filename}")
                else:
                    logger.warning(f"TikTok conversion failed, using raw: {raw_filename}")
            else:
                logger.info(f"TikTok video already H.264 @ {_target_res}, skipping conversion: {raw_filename}")

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

    _cfg = engine.load_config()
    _target_res = _cfg.get("resolution", "720x1280")
    _tw, _th = _target_res.split("x")

    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", target_path],
        capture_output=True, text=True, timeout=10
    ).stdout.strip().split(",")

    if len(probe) >= 1:
        _vcodec = probe[0].strip()
        if _vcodec in ("h264", "avc", "h264 "):
            return {"success": True, "already_compatible": True, "message": f"Video đã H.264, không cần convert"}

    converted_path = os.path.join(MEDIA_DIR, "converted_" + filename)
    subprocess.run([
        "ffmpeg", "-y", "-i", target_path,
        "-vf", f"scale={_tw}:{_th}:force_original_aspect_ratio=decrease,pad={_tw}:{_th}:0:0:black",
        "-c:v", "libx264", "-preset", "slow", "-crf", "27",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", converted_path],
        capture_output=True, timeout=300)
    if os.path.exists(converted_path) and os.path.getsize(converted_path) > 100000:
        os.replace(converted_path, target_path)
        return {"success": True, "message": f"Đã convert sang H.264 {_target_res} (CRF 27)"}
    return {"success": False, "error": "Convert thất bại"}

@app.get("/api/logs")
def get_logs():
    log_file = os.path.join(LOGS_DIR, "stream.log")
    if os.path.exists(log_file):
        with open(log_file, "r") as f:
            lines = f.readlines()
            return {"logs": lines[-100:]}
    return {"logs": ["No logs recorded yet."]}

# ===== TIKTOK LIVE CLIENT APIS =====# ===== TIKTOK LIVE CLIENT APIS =====

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
    live_client.configure(username, None, None)
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
    }

@app.post("/api/live/comment-forward")
def forward_comment(req: ForwardCommentModel):
    """
    Receive a comment forwarded by an external listener (comment_forwarder.py)
    on a clean IP / via proxy. Server ADDS user comment to overlay scroll.
    """
    if not live_client.is_available():
        raise HTTPException(status_code=500, detail="TikTokLive library not available on server")
    # Inject comment → triggers _handle_live_comment callback → overlay rendering
    live_client.inject_comment(req.username, req.comment, trigger_ai=True)

    return {
        "success": True,
        "message": f"Comment forwarded by @{req.username}",
    }

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

@app.post("/api/overlay/welcome")
def add_overlay_welcome(username: str = ""):
    """Add a welcome message for a new viewer."""
    overlay_renderer.add_welcome_message(username or "New viewer")
    return {"success": True, "message": "Welcome message added to overlay"}

@app.post("/api/overlay/config")
def configure_overlays(enabled: bool, comment_scroll: bool = True, ai_response: bool = True, 
                       viewer_count: bool = True, stats_panel: bool = True, clock: bool = True,
                       welcome: bool = True):
    """Configure which overlays are enabled."""
    config = {
        "clock": clock,
        "comment_scroll": comment_scroll,
        "ai_response": ai_response,
        "viewer_count": viewer_count,
        "stats_panel": stats_panel,
        "welcome": welcome,
    }
    overlay_renderer.configure_overlays(config)
    return {"success": True, "message": "Overlay configuration updated"}


if __name__ == "__main__":
    import signal
    import uvicorn
    def _graceful_exit(signum, frame):
        try:
            engine.stop_stream()
        except Exception:
            pass
        subprocess.run(["pkill", "-f", "ffmpeg"], capture_output=True)
        exit(0)
    signal.signal(signal.SIGTERM, _graceful_exit)
    signal.signal(signal.SIGINT, _graceful_exit)
    uvicorn.run(app, host="127.0.0.1", port=8888)
