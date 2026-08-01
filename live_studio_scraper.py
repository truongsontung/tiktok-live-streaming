#!/usr/bin/env python3
"""
Live Studio Scraper
Uses Playwright browser automation to access TikTok Live Studio
and intercept network requests to extract the stream key.
"""

import os
import json
import time
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger("LiveStudioScraper")

try:
    from playwright.sync_api import sync_playwright
    from playwright._impl._api_structures import ProxySettings
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

class LiveStudioScraper:
    """
    Browser automation for TikTok Live Studio.
    Intercepts network requests to extract stream key when direct API fails.
    """

    def __init__(self):
        self.client = None
        self.last_error = ""

    @property
    def available(self) -> bool:
        """Check if Playwright library is installed."""
        return PLAYWRIGHT_AVAILABLE

    def fetch_stream_key_with_session(self, session_id: str) -> Optional[Dict[str, str]]:
        """
        Use Playwright to navigate to TikTok Live Studio with session cookie.
        Intercept network requests to extract stream key from API response.
        """
        if not self.available:
            self.last_error = "Playwright not installed. Run: pip install playwright && python -m playwright install chromium"
            return None

        if not session_id:
            self.last_error = "Session ID is required"
            return None

        sess_id = session_id.strip()
        # Extract sessionid value if full cookie string provided
        if "sessionid=" in sess_id:
            for item in sess_id.split(";"):
                if item.strip().startswith("sessionid=") and not item.strip().startswith("sessionid_ss="):
                    sess_id = item.split("=", 1)[1].strip()
                    break

        result = {"rtmp_url": None, "stream_key": None, "tt_target_idc": None}

        try:
            with sync_playwright() as p:
                print("[Scraper] Launching browser...", flush=True)

                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        '--no-sandbox',
                        '--disable-setuid-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-blink-features=AutomationControlled',
                        '--disable-web-security',
                    ],
                )

                context = browser.new_context(
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                    viewport={'width': 1280, 'height': 800},
                    java_script_enabled=True,
                    bypass_csp=True,
                )

                # Set cookies
                cookies = [
                    {"name": "sessionid", "value": sess_id, "domain": ".tiktok.com", "path": "/", "httpOnly": False, "secure": True, "sameSite": "None"},
                    {"name": "sessionid_ss", "value": sess_id, "domain": ".tiktok.com", "path": "/", "httpOnly": False, "secure": True, "sameSite": "None"},
                    {"name": "sid_api", "value": sess_id, "domain": ".tiktok.com", "path": "/", "httpOnly": False, "secure": True, "sameSite": "None"},
                    {"name": "sid_tt", "value": sess_id, "domain": ".tiktok.com", "path": "/", "httpOnly": False, "secure": True, "sameSite": "None"},
                ]
                context.add_cookies(cookies)
                print("[Scraper] Cookies set ✓", flush=True)

                page = context.new_page()

                # Intercept network responses to find stream key
                def handle_response(response):
                    try:
                        # Extract tt-target-idc from response headers
                        if not result.get("tt_target_idc"):
                            try:
                                for hname, hval in response.headers.items():
                                    if hname.lower() == "set-cookie" and "tt-target-idc=" in hval:
                                        for part in hval.split(";"):
                                            part = part.strip()
                                            if part.startswith("tt-target-idc="):
                                                val = part.split("=", 1)[1].strip()
                                                if val:
                                                    result["tt_target_idc"] = val
                                                    break
                            except Exception:
                                pass
                        url = response.url
                        if 'webcast' in url and ('room/create' in url or 'stream' in url.lower() or 'push_url' in url.lower()):
                            try:
                                data = response.json()
                            except:
                                return

                            stream_data = data.get("data", {}).get("stream_url", {})
                            rtmp_url = stream_data.get("rtmp_push_url", "")

                            if not rtmp_url:
                                push_urls = stream_data.get("push_urls", [])
                                if push_urls:
                                    rtmp_url = push_urls[0]

                            if rtmp_url:
                                if "/live/" in rtmp_url:
                                    parts = rtmp_url.rsplit("/live/", 1)
                                    result["rtmp_url"] = parts[0] + "/live/"
                                    result["stream_key"] = parts[1]
                                else:
                                    parts = rtmp_url.rsplit("/", 1)
                                    result["rtmp_url"] = parts[0] + "/"
                                    result["stream_key"] = parts[1]
                    except Exception:
                        pass

                page.on("response", handle_response)

                # Try POST request to webcast API (works when GET returns 403)
                print("[Scraper] Trying POST to webcast API...", flush=True)
                stream_url = "https://webcast.tiktok.com/webcast/room/create/"
                post_data = "device_platform=web&aid=1988&app_language=en&app_name=tiktok_web&browser_name=Mozilla&browser_version=5.0&channel=googleios&api_service_version=2&live_type=1&stream_type=push&mode=web&quality=normal"
                try:
                    resp = page.request.post(stream_url, data=post_data, headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                    }, timeout=20000)
                    if resp.status == 200:
                        try:
                            data = resp.json()
                            stream_data = data.get("data", {}).get("stream_url", {})
                            # Prefer RTMPS (SSL) over plain RTMP for TikTok streaming
                            push_urls = stream_data.get("push_urls", [])
                            rtmp_url = push_urls[0] if push_urls else stream_data.get("rtmp_push_url", "")
                            if not rtmp_url:
                                rtmp_url = stream_data.get("rtmp_push_url", "")
                            if not rtmp_url and push_urls:
                                rtmp_url = push_urls[0]

                            if rtmp_url:
                                if "/live/" in rtmp_url:
                                    parts = rtmp_url.rsplit("/live/", 1)
                                    result["rtmp_url"] = parts[0] + "/live/"
                                    result["stream_key"] = parts[1]
                                elif "/stage/" in rtmp_url:
                                    parts = rtmp_url.rsplit("/stage/", 1)
                                    result["rtmp_url"] = parts[0] + "/stage/"
                                    result["stream_key"] = parts[1]
                                else:
                                    parts = rtmp_url.rsplit("/", 1)
                                    result["rtmp_url"] = parts[0] + "/"
                                    result["stream_key"] = parts[1]
                                print("[Scraper] RTMPS stream key extracted via POST!", flush=True)

                                # Extract tt-target-idc from response headers (needed for send_room_chat auth)
                                if not result["tt_target_idc"]:
                                    try:
                                        for hname, hval in resp.headers_list:
                                            if hname.lower() == "set-cookie" and "tt-target-idc=" in hval:
                                                for part in hval.split(";"):
                                                    part = part.strip()
                                                    if part.startswith("tt-target-idc="):
                                                        val = part.split("=", 1)[1].strip()
                                                        if val:
                                                            result["tt_target_idc"] = val
                                                            print(f"[Scraper] tt-target-idc extracted: {val}", flush=True)
                                                        break
                                            if result["tt_target_idc"]:
                                                break
                                    except Exception as _e:
                                        print(f"[Scraper] tt-target-idc extract failed: {_e}", flush=True)

                                # Fallback: get from context cookies
                                if not result["tt_target_idc"]:
                                    try:
                                        for _c in context.cookies():
                                            if _c.get("name") == "tt-target-idc" and _c.get("value"):
                                                result["tt_target_idc"] = _c["value"]
                                                print(f"[Scraper] tt-target-idc via cookies: {_c['value']}", flush=True)
                                                break
                                    except Exception:
                                        pass
                        except:
                            pass
                except Exception as e:
                    print(f"[Scraper] POST failed: {e}", flush=True)

                if not (result["rtmp_url"] and result["stream_key"]):
                    # Fallback: try navigating to Live Studio page
                    print("[Scraper] Falling back to page navigation...", flush=True)
                    page.goto("https://www.tiktok.com/live/studio", wait_until="domcontentloaded", timeout=30000)
                    time.sleep(5)

                # Try clicking buttons if still not found
                if not (result["rtmp_url"] and result["stream_key"]):
                    buttons = [
                        'button:has-text("Stream with OBS")',
                        'button:has-text("Thiết lập Stream")',
                        'button:has-text("Start Live")',
                        'button:has-text("Tạo Live")',
                        'button:has-text("Create Room")',
                        '[data-e2e="live-stream-button"]',
                        'a:has-text("Studio")',
                    ]
                    for btn_sel in buttons:
                        btn = page.query_selector(btn_sel)
                        if btn:
                            print(f"[Scraper] Clicking: {btn_sel}", flush=True)
                            btn.click()
                            time.sleep(5)
                            break

                # Try to find stream key in page HTML
                if not (result["rtmp_url"] and result["stream_key"]):
                    try:
                        import re
                        content = page.content()
                        rtmp_matches = re.findall(r'(rtmp://[^\s"\'<>]+)', content)
                        if rtmp_matches:
                            result["rtmp_url"] = rtmp_matches[0]
                            print(f"[Scraper] Found RTMP in page: {rtmp_matches[0][:60]}...", flush=True)
                    except:
                        pass

                browser.close()

                if result["rtmp_url"] and result["stream_key"]:
                    print("[Scraper] Stream key extracted successfully!", flush=True)
                    return result
                else:
                    self.last_error = "Stream key not found. Account may not have live permissions."
                    return None

        except Exception as e:
            self.last_error = str(e)
            logger.error(f"Scraper error: {e}")
            return None

    def get_telemetry(self) -> Dict[str, Any]:
        """Get scraper status."""
        return {
            "available": self.available,
            "last_error": self.last_error,
        }


# Global instance
scraper = LiveStudioScraper()
