#!/usr/bin/env python3
"""
Session ID Extractor (Standardized)
Tự động phát hiện profile Chromium đang dùng để đăng nhập TikTok
và trích xuất session ID một cách chuẩn hoá.
"""

import os
import sys
import time
import json
from typing import Optional, Tuple, List

try:
    from playwright.sync_api import sync_playwright
    from playwright._impl._api_structures import ProxySettings
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

SESSION_FILE = "/home/vps2/tiktok_live/tiktok_session.txt"

# Các profile paths tiềm năng trên hệ thống
PROFILE_CANDIDATES = [
    ("Flatpak Chromium", "/home/vps2/.var/app/org.chromium.Chromium/config/chromium"),
    ("Snap Chromium", "/home/vps2/snap/chromium/common/chromium"),
    ("Regular Chromium", "/home/vps2/.config/chromium"),
    ("Chrome", "/home/vps2/.config/google-chrome"),
    ("Playwright Profile", "/home/vps2/tiktok_live/tiktok_profile"),
    ("VNC Chromium", "/home/vps2/chromium_vnc"),
]

def detect_tiktok_profile() -> Tuple[Optional[str], Optional[List[str]]]:
    """
    Tự động tìm profile Chromium có cookies TikTok session.
    Trả về (profile_path, list_of_session_cookie_values)
    """
    if not PLAYWRIGHT_AVAILABLE:
        print("❌ Playwright chưa cài đặt.")
        return None, None

    for name, profile_path in PROFILE_CANDIDATES:
        if not os.path.exists(profile_path):
            continue

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch_persistent_context(
                    profile_path,
                    headless=True,
                    args=[
                        '--no-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-blink-features=AutomationControlled',
                    ],
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
                )

                # Check if sessionid cookie exists in this profile
                cookies = browser.cookies("https://www.tiktok.com")
                browser.close()

                session_cookies = [c for c in cookies if c['name'] in ('sessionid', 'sessionid_ss')]

                if session_cookies:
                    print(f"✅ Tìm thấy profile: {name} ({profile_path})", flush=True)
                    print(f"   Cookies: {len(cookies)} total, {len(session_cookies)} session cookies", flush=True)
                    for c in session_cookies:
                        print(f"   {c['name']}: {c['value'][:50]}...", flush=True)
                    return profile_path, [c['value'] for c in session_cookies]

        except Exception as e:
            print(f"   Skip {name}: {e}", flush=True)
            continue

    return None, None

def extract_session_id(profile_path: Optional[str] = None) -> Optional[str]:
    """
    Trích xuất session ID từ Chromium profile.
    Nếu profile_path=None, sẽ tự động detect.
    """
    if not PLAYWRIGHT_AVAILABLE:
        print("❌ Playwright chưa cài đặt.")
        return None

    # Tự động detect profile nếu chưa chỉ định
    if profile_path is None:
        profile_path, session_cookies = detect_tiktok_profile()
        if not profile_path:
            print("❌ Không tìm thấy profile Chromium nào có session cookies.", flush=True)
            return None
    else:
        if not os.path.exists(profile_path):
            print(f"❌ Profile không tồn tại: {profile_path}", flush=True)
            return None

    # Extract session ID with browser automation
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch_persistent_context(
                profile_path,
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-blink-features=AutomationControlled',
                ],
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
            )

            page = browser.new_page()

            # Bypass bot detection
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = {runtime: {},LoadTimes:()=>{}};
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
            """)

            # Navigate to TikTok to ensure cookies are loaded
            page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=20000)
            time.sleep(2)

            # Verify login status
            login_btn = page.query_selector(
                'a[data-e2e="login"], button[data-e2e="login"], '
                'button:has-text("Log in"), button:has-text("Đăng nhập")'
            )

            # Get ALL cookies and find session
            cookies = browser.cookies("https://www.tiktok.com")
            browser.close()

            session_id = None
            for c in cookies:
                if c['name'] == 'sessionid':
                    session_id = c['value']
                    break

            if not session_id:
                for c in cookies:
                    if c['name'] in ('sessionid_ss', 'sid_tt'):
                        session_id = c['value']
                        break

            if session_id:
                if login_btn is None:
                    print(f"✅ Đăng nhập thành công!", flush=True)
                else:
                    print(f"⚠️ Có session ID nhưng chưa đăng nhập đầy đủ", flush=True)
                return session_id
            else:
                print("❌ Không tìm thấy sessionid trong cookies", flush=True)
                return None

    except Exception as e:
        print(f"❌ Lỗi khi trích xuất session: {e}", flush=True)
        return None

def save_session_id(session_id: str) -> bool:
    """Lưu session ID vào file."""
    try:
        with open(SESSION_FILE, "w") as f:
            f.write(session_id)
        print(f"✅ Session ID đã lưu vào: {SESSION_FILE}", flush=True)
        return True
    except Exception as e:
        print(f"❌ Không thể lưu session: {e}", flush=True)
        return False

def test_session(session_id: str) -> Tuple[bool, str]:
    """
    Test session ID validity với webcast API.
    Returns (is_valid, message)
    """
    import urllib.request
    import urllib.error

    cookie_str = f"sessionid={session_id}; sessionid_ss={session_id}; sid_tt={session_id}"
    url = (
        "https://webcast.tiktok.com/webcast/room/create/"
        "?device_platform=web&aid=1988&app_language=en"
        "&app_name=tiktok_web&channel=googleios&api_service_version=2"
    )

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Cookie": cookie_str,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode())

        if data.get("status_code") == 0:
            live_permission = data.get("data", {}).get("owner", {}).get("hasLivePermission",
                               data.get("data", {}).get("hasLivePermission", False))
            unique_id = data.get("data", {}).get("owner", {}).get("uniqueId", "unknown")
            return True, f"✅ Session hợp lệ! User: {unique_id}, Live permission: {live_permission}"
        else:
            msg = data.get("data", {}).get("prompts", "Unknown error")
            return False, f"❌ API error: {msg}"
    except urllib.error.HTTPError as e:
        return False, f"❌ HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, f"❌ Error: {str(e)}"

def main():
    print("=" * 60)
    print("  TikTok Session ID Extractor (Standardized)")
    print("=" * 60)

    # Try to load saved session
    if os.path.exists(SESSION_FILE):
        saved = open(SESSION_FILE).read().strip()
        print(f"\n📄 Session ID đã lưu: {saved[:40]}...")

    # Extract new session
    print("\n🔍 Đang tìm profile Chromium đăng nhập TikTok...")
    session_id = extract_session_id()

    if session_id:
        print(f"\n📋 Session ID: {session_id}")

        # Test session validity
        print("\n🧪 Kiểm tra session...", flush=True)
        is_valid, msg = test_session(session_id)
        print(msg)

        # Save
        if session_id != (open(SESSION_FILE).read().strip() if os.path.exists(SESSION_FILE) else None):
            save_session_id(session_id)
        else:
            print(f"   (trùng với session đã lưu)")

        print("\n✅ Hoàn tất!")
    else:
        print("\n❌ Không thể lấy session ID. Vui lòng đăng nhập TikTok trên trình duyệt VPS.")
        print("\nHướng dẫn:")
        print("1. Mở VNC: kết nối VNC client tới 140.245.58.79:5999 (pass: tiktok99)")
        print("2. Đăng nhập TikTok trên Chromium trong VNC")
        print("3. Chạy lại: python3 session_validator.py")

if __name__ == "__main__":
    main()
