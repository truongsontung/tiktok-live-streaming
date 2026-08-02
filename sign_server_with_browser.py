#!/usr/bin/env python3
"""
Integrated Sign Server + Cookie Refresher
Gộp http://127.0.0.1:9801 proxy + Playwright cookie auto-refresh
thành một thành phần duy nhất.

Chạy embedded trong app.py:
    from sign_server_with_browser import SignServerWithBrowser
    sign_server = SignServerWithBrowser(port=9801)
    sign_server.start()  # Blocking: chạy Playwright lấy cookies đầu tiên (~50s)
    sign_server.start_background_refresh()  # Non-blocking: refresh mỗi 25 phút
    # ... use TikTokLive ...
    sign_server.shutdown()  # Cleanup
"""
import os
import sys
import json
import time
import random
import logging
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import httpx

# Pre-import protobuf at module level (tránh delay ~11s trên mỗi request thread)
from TikTokLive.proto import ProtoMessageFetchResult

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [SIGN-SERVER] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("SignServer")

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tiktok_live", "config.json"
)
with open(CONFIG_PATH) as f:
    CFG = json.load(f)

SESSION_ID = CFG.get("tiktok_session", "")
TT_TARGET_IDC_CFG = CFG.get("tiktok_tt_target_idc", "useast1a")
TIKTOK_USERNAME = CFG.get("tiktok_username", "").lstrip("@")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36 Edg/147.0.3912.86"
)

TIKTOK_CHECK_ALIVE_URL = "https://webcast.tiktok.com/webcast/room/check_alive/"

# Cookie cache + lock
_cookie_lock = threading.Lock()
_cookie_cache = {
    "msToken": "",
    "ttwid": "",
    "tt_target_idc": TT_TARGET_IDC_CFG,
    "ws_url": "",
    "extracted_at": 0,
}


def _load_config_from_file():
    """Load config from config.json (for session_id, username)."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except:
        return {}


def _init_from_config_file():
    """Load cached cookies from /tmp/tiktok_ws_config.json if available."""
    config_file = "/tmp/tiktok_ws_config.json"
    try:
        with open(config_file) as f:
            saved = json.load(f)
        with _cookie_lock:
            _cookie_cache["ws_url"] = saved.get("ws_url", "")
            _cookie_cache["tt_target_idc"] = saved.get("tt_target_idc", TT_TARGET_IDC_CFG)
            if not _cookie_cache["msToken"]:
                _cookie_cache["msToken"] = saved.get("msToken", "")
            if not _cookie_cache["ttwid"]:
                _cookie_cache["ttwid"] = saved.get("ttwid", "")
            _cookie_cache["extracted_at"] = saved.get("timestamp", 0)
        if _cookie_cache["msToken"]:
            logger.info(f"Loaded cached cookies from {config_file}")
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"Error loading cached config: {e}")


_init_from_config_file()


def extract_cookies_via_playwright():
    """
    Chạy Playwright (system Chromium trên ARM64) để:
    1. Load TikTok live page
    2. Bắt WebSocket URL (webcast-ws.tiktok.com/ws_proxy/...)
    3. Lấy msToken, ttwid, tt-target-idc cookies

    Duration: ~50s (3s load + 40s wait for WebSocket + 7s buffer)
    """
    from playwright.async_api import async_playwright
    import asyncio

    session_id = _load_config_from_file().get("tiktok_session", "")
    username = _load_config_from_file().get("tiktok_username", "").lstrip("@")

    async def _run():
        cookies_to_set = []
        if session_id:
            cookies_to_set = [
                {"name": "sessionid", "value": session_id, "domain": ".tiktok.com", "path": "/"},
                {"name": "sid_tt", "value": session_id, "domain": ".tiktok.com", "path": "/"},
                {"name": "sessionid_ss", "value": session_id, "domain": ".tiktok.com", "path": "/"},
            ]

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                executable_path="/usr/bin/chromium-browser",
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationDetected",
                    "--disable-web-security",
                ],
            )
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1920, "height": 1080},
            )
            if cookies_to_set:
                await context.add_cookies(cookies_to_set)

            page = await context.new_page()
            await page.add_init_script("""
                window.__capturedWss = [];
                const OrigWS = window.WebSocket;
                window.WebSocket = function(url, protocols) {
                    window.__capturedWss.push(url);
                    return new OrigWS(url, protocols);
                };
                Object.assign(window.WebSocket, OrigWS);
            """)

            # Navigate to live page
            live_url = f"https://www.tiktok.com/@{username}/live" if username else "https://www.tiktok.com/"
            try:
                await page.goto(live_url, wait_until="domcontentloaded", timeout=20000)
            except:
                pass

            # Wait for SDK to load and make WebSocket connections
            await asyncio.sleep(45)

            ws_urls = await page.evaluate("window.__capturedWss || []")
            cookies = await context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}

            # Find webcast-ws URL
            ws_url = None
            for u in ws_urls:
                if "ws_proxy" in u:
                    ws_url = u
                    break
            if not ws_url:
                ws_url = _cookie_cache.get("ws_url", "")

            result = {
                "ws_url": ws_url,
                "msToken": cookie_dict.get("msToken", ""),
                "ttwid": cookie_dict.get("ttwid", ""),
                "tt_target_idc": cookie_dict.get("tt-target-idc", TT_TARGET_IDC_CFG),
            }

            await browser.close()
            return result

    return asyncio.run(_run())


def get_tt_target_idc():
    """Get the current tt_target_idc from cookie cache (Playwright-extracted or config default)."""
    with _cookie_lock:
        return _cookie_cache.get("tt_target_idc") or TT_TARGET_IDC_CFG


def get_session_id():
    """Get the current TikTok session_id from config (used for authenticated webcast API calls)."""
    return _load_config_from_file().get("tiktok_session", "")


class SignServerHandler(BaseHTTPRequestHandler):
    """HTTP handler cho /webcast/fetch (protobuf) và /webcast/sign_url (JSON)."""

    def log_message(self, format, *args):
        pass  # Suppress default logging

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/webcast/fetch":
            self._handle_fetch(params)
        elif path == "/health":
            body = json.dumps({"status": "ok", "time": time.time()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/webcast/sign_url":
            self._handle_sign_url()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_fetch(self, params):
        room_id = params.get("room_id", [None])[0]
        user_agent = params.get("user_agent", [USER_AGENT])[0] if params.get("user_agent") else USER_AGENT

        if not room_id or room_id == "0":
            room_id = self._get_room_id()

        if not room_id:
            logger.error("Cannot determine room_id")
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        logger.info(f"GET /webcast/fetch for room_id={room_id}")

        # Build protobuf response with current cookies
        with _cookie_lock:
            msToken = _cookie_cache["msToken"]
            ttwid = _cookie_cache["ttwid"]
            tt_idc = _cookie_cache["tt_target_idc"] or TT_TARGET_IDC_CFG
            ws_url = _cookie_cache["ws_url"]

        if not ws_url:
            logger.error("No WebSocket URL in cache - Playwright extraction hasn't run yet")
            self.send_response(429)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        parsed_ws = urlparse(ws_url)
        push_server = f"{parsed_ws.scheme}://{parsed_ws.netloc}{parsed_ws.path}"
        qs = parse_qs(parsed_ws.query)
        route_params = {k: v[0] for k, v in qs.items() if v}

        # Update room_id to match the current stream
        route_params["room_id"] = room_id

        pmfr = ProtoMessageFetchResult(
            push_server=push_server,
            route_params=route_params,
            cursor=str(int(time.time())),
            now=int(time.time() * 1000),
            is_first=True,
            heartbeat_duration=10000,
            need_ack=True,
        )
        protobuf_data = pmfr.SerializeToString()

        # Build X-Set-TT-Cookie header with ALL necessary cookies
        cookie_parts = []
        if SESSION_ID:
            cookie_parts.append(f"sessionid={SESSION_ID}")
            cookie_parts.append(f"sid_tt={SESSION_ID}")
            cookie_parts.append(f"sessionid_ss={SESSION_ID}")
        if tt_idc:
            cookie_parts.append(f"tt-target-idc={tt_idc}")
        if msToken:
            cookie_parts.append(f"msToken={msToken}")
        if ttwid:
            cookie_parts.append(f"ttwid={ttwid}")

        cookie_header = "; ".join(cookie_parts)
        logger.info(f"Response: push_server={push_server}, route_params={len(route_params)}, X-Bogus={route_params.get('X-Bogus','none')}, cookies={len(cookie_parts)}")

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        if cookie_header:
            self.send_header("X-Set-TT-Cookie", cookie_header)
        self.send_header("Content-Length", str(len(protobuf_data)))
        self.end_headers()
        try:
            self.wfile.write(protobuf_data)
        except BrokenPipeError:
            logger.warning("Client disconnected before response was sent")

    def _handle_sign_url(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            request_data = json.loads(body) if body else {}
        except:
            request_data = {}

        url = request_data.get("url", "")
        logger.info(f"POST /webcast/sign_url for {url[:80]}...")

        with _cookie_lock:
            msToken = _cookie_cache["msToken"]

        if msToken and "msToken" not in url:
            url = url + (("&" if "?" in url else "?")) + f"msToken={msToken}"

        response = {
            "code": 200,
            "message": "",
            "response": {
                "signedUrl": url,
                "tokens": {"msToken": msToken or ""},
                "userAgent": USER_AGENT,
                "browserName": "Mozilla",
                "browserVersion": "129.0.0.0",
            },
        }

        cookie_parts = []
        if SESSION_ID:
            cookie_parts.append(f"sessionid={SESSION_ID}")
            cookie_parts.append(f"sid_tt={SESSION_ID}")
            cookie_parts.append(f"sessionid_ss={SESSION_ID}")
        with _cookie_lock:
            tt_idc = _cookie_cache.get("tt_target_idc") or TT_TARGET_IDC_CFG
            ttwid = _cookie_cache.get("ttwid", "")
        if tt_idc:
            cookie_parts.append(f"tt-target-idc={tt_idc}")
        if msToken:
            cookie_parts.append(f"msToken={msToken}")
        if ttwid:
            cookie_parts.append(f"ttwid={ttwid}")

        response_body = json.dumps(response).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if cookie_parts:
            self.send_header("X-Set-TT-Cookie", "; ".join(cookie_parts))
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    def _get_room_id(self):
        """Get room_id from TikTok live page."""
        if not TIKTOK_USERNAME:
            return None
        try:
            resp = httpx.get(
                f"https://www.tiktok.com/@{TIKTOK_USERNAME}/live",
                headers={"User-Agent": USER_AGENT},
                cookies={"sessionid": SESSION_ID} if SESSION_ID else {},
                timeout=10,
            )
            import re
            for pattern in [r'"roomId":"(\d+)"', r'"room_id":"(\d+)"']:
                match = re.search(pattern, resp.text)
                if match:
                    return match.group(1)
        except Exception as e:
            logger.error(f"Error getting room_id: {e}")
        return None


class SignServerWithBrowser:
    """
    Integrated Sign Server + Playwright Cookie Refresher.

    - HTTP server trên port 9801
    - Tự động extract msToken/ttwid/ws_url qua Playwright (system Chromium)
    - Background refresh mỗi 25 phút
    - Thread-safe cookie cache
    """

    REFRESH_INTERVAL = 25 * 60  # 25 phút
    COOKIE_STALE_THRESHOLD = 50 * 60  # 50 phũt - tokens nên được refresh trước khi hết hạn

    def __init__(self, port=9801):
        self.port = port
        self._server = None
        self._server_thread = None
        self._refresh_thread = None
        self._stop_event = threading.Event()
        self._initial_extract_done = threading.Event()

    def _extract_and_cache(self):
        """Run Playwright to extract fresh cookies and update cache."""
        global _cookie_cache
        logger.info("Starting Playwright cookie extraction...")

        try:
            result = extract_cookies_via_playwright()
            with _cookie_lock:
                _cookie_cache.update(result)
                _cookie_cache["extracted_at"] = time.time()
                logger.info(
                    f"Cookies extracted: msToken={len(result.get('msToken',''))} chars, "
                    f"ttwid={len(result.get('ttwid',''))} chars, "
                    f"ws_url={'found' if result.get('ws_url') else 'none'}"
                )
        except Exception as e:
            logger.error(f"Playwright extraction failed: {e}")
            with _cookie_lock:
                if _cookie_cache["msToken"]:
                    logger.warning("Using stale cookies as fallback")
        finally:
            self._initial_extract_done.set()

    def start(self, blocking=True):
        """
        Start the HTTP server and run initial cookie extraction.

        Args:
            blocking: If True, chạy Playwright extraction trước khi return (~50s).
                     If False, chạy trong background thread và return ngay.
        """
        # Start HTTP server in background thread
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), SignServerHandler)
        self._server_thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._server_thread.start()
        logger.info(f"Sign Server (with Browser) running on http://127.0.0.1:{self.port}")
        logger.info(f"  TikTok username: @{TIKTOK_USERNAME}")
        logger.info(f"  SIGN_API_URL=http://127.0.0.1:{self.port}")

        # Wait for health endpoint to be ready
        time.sleep(1)

        # Run initial cookie extraction in a separate thread
        # (extract_cookies_via_playwright uses asyncio.run() which can't be called
        # from a running event loop - e.g., FastAPI lifespan)
        extract_thread = threading.Thread(target=self._extract_and_cache, daemon=True)
        extract_thread.start()

        if blocking:
            extract_thread.join(timeout=120)  # Wait up to 2 min
        else:
            # Return immediately, extraction runs in background
            pass

    def start_background_refresh(self):
        """Start background thread that refreshes cookies every 25 minutes."""
        self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._refresh_thread.start()
        logger.info(f"Background cookie refresh started (every {self.REFRESH_INTERVAL // 60} min)")

    def _refresh_loop(self):
        """Periodically refresh cookies via Playwright."""
        while not self._stop_event.is_set():
            # Wait for next refresh interval
            self._stop_event.wait(self.REFRESH_INTERVAL)
            if self._stop_event.is_set():
                break

            logger.info("Background cookie refresh triggered...")
            self._extract_and_cache()

    def wait_for_cookies(self, timeout=120):
        """Wait until initial cookie extraction is done."""
        return self._initial_extract_done.wait(timeout=timeout)

    def shutdown(self):
        """Stop the server and cleanup."""
        logger.info("Shutting down Sign Server...")
        self._stop_event.set()

        if self._server:
            self._server.shutdown()
            self._server.server_close()

        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=5)

        logger.info("Sign Server stopped")

    @property
    def is_ready(self):
        """Check if cookies have been extracted."""
        with _cookie_lock:
            return bool(_cookie_cache["msToken"]) and bool(_cookie_cache["ws_url"])

    @property
    def cookies_age_seconds(self):
        """Return age of current cookies in seconds."""
        with _cookie_lock:
            return time.time() - _cookie_cache.get("extracted_at", 0)


# Module-level singleton
_sign_server_instance = None


def get_or_start_sign_server(port=9801):
    """Get or create the singleton SignServerWithBrowser instance."""
    global _sign_server_instance
    if _sign_server_instance is None:
        _sign_server_instance = SignServerWithBrowser(port=port)
    return _sign_server_instance


if __name__ == "__main__":
    server = SignServerWithBrowser(port=9801)
    server.start(blocking=True)
    server.start_background_refresh()

    try:
        logger.info("Server running. Press Ctrl+C to stop.")
        while True:
            time.sleep(60)
            if server.is_ready:
                age = server.cookies_age_seconds
                logger.info(f"Cookies age: {age // 60}m {age % 60}s | Ready: {server.is_ready}")
    except KeyboardInterrupt:
        server.shutdown()
        logger.info("Server stopped.")
