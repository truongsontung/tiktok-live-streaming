#!/usr/bin/env python3
"""
Interactive Login Helper v2
Uses Xvfb + Playwright to open a browser for manual TikTok login.
After login, automatically extracts the session ID.
"""

import os
import sys
import time
import json
import tempfile
import shutil

try:
    from playwright.sync_api import sync_playwright
    from playwright._impl._api_structures import ProxySettings
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

SESSION_FILE = "/home/vps2/tiktok_live/tiktok_session.txt"
USER_DATA_DIR = "/home/vps2/tiktok_live/tiktok_profile"

def save_session_id(session_id):
    """Save session ID to file and return."""
    with open(SESSION_FILE, "w") as f:
        f.write(session_id)
    print(f"\n✅ Session ID đã được lưu vào: {SESSION_FILE}")
    print(f"   Nội dung: {session_id[:30]}...")
    return session_id

def main():
    if not PLAYWRIGHT_AVAILABLE:
        print("Lỗi: Playwright chưa cài đặt. Chạy: pip install playwright && python -m playwright install chromium")
        sys.exit(1)

    print("=" * 60)
    print("  TikTok Interactive Login (v2 - Xvfb + Playwright)")
    print("=" * 60)
    print()
    print("Hệ thống sẽ mở trình duyệt trong môi trường ảo (Xvfb)")
    print("Trình duyệt sẽ tự động mở trang TikTok để đăng nhập")
    print()

    # Ensure user data dir exists
    os.makedirs(USER_DATA_DIR, exist_ok=True)

    with sync_playwright() as p:
        print("🔧 Đang khởi tạo trình duyệt...")

        # Launch chromium with persistent context
        # This saves cookies to USER_DATA_DIR for future use
        browser = p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-web-security',
                '--disable-blink-features=AutomationControlled',
                '--disable-gpu',
                '--window-size=1280,800',
            ],
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
        )

        page = browser.new_page()
        page.set_viewport_size({"width": 1280, "height": 800})

        print("🌐 Đang truy cập TikTok...")

        # Navigate to TikTok login page
        page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        # Check if already logged in
        try:
            # Look for login button or user avatar
            login_btn = page.query_selector('a[data-e2e="login"], button[data-e2e="login"], button:has-text("Log in")')
            user_avatar = page.query_selector('a[data-e2e="user-profile"], div[data-e2e="user-avatar"], img[alt*="avatar"]')

            if user_avatar:
                print("✅ Đã đăng nhập sẵn!")
                # Proceed to extract session ID
            elif login_btn:
                print("\n⚠ Chưa đăng nhập - cần đăng nhập thủ công")
                print("\n📋 HƯỚNG DẪN ĐĂNG NHẬP:")
                print("1. Click vào nút 'Log in' ở góc trên bên phải")
                print("2. Chọn 'Use phone/email/username' hoặc 'Use QR code'")
                print("3. Nhập tài khoản và mật khẩu TikTok của bạn")
                print("4. Nếu có CAPTCHA, vui lòng hoàn thành")
                print("5. Sau khi đăng nhập xong (thấy avatar tên người dùng), quay lại đây")
                print("6. Nhấn Enter để tiếp tục")
                print()

                # Wait for login
                input()

                # Reload page to check login status
                page.reload(wait_until="networkidle", timeout=30000)
                time.sleep(3)

                user_avatar = page.query_selector('a[data-e2e="user-profile"], div[data-e2e="user-avatar"]')
                if user_avatar:
                    print("✅ Đăng nhập thành công!")
                else:
                    print("❌ Vẫn chưa đăng nhập thành công. Thử lại?")
                    browser.close()
                    sys.exit(1)
            else:
                print("Không xác định được trạng thái đăng nhập")
        except Exception as e:
            print(f"Lỗi kiểm tra đăng nhập: {e}")

        # Wait for cookies to be set
        time.sleep(3)

        # Get all cookies
        cookies = page.context.cookies("https://www.tiktok.com")

        print(f"\n=== Cookies tìm thấy ({len(cookies)}) ===")
        session_id = None

        for cookie in cookies:
            name = cookie['name']
            value = cookie['value']
            print(f"  {name}: {value[:60]}...")

            if name == 'sessionid':
                session_id = value
            elif name == 'sessionid_ss' and not session_id:
                session_id = value

        # Also try document.cookie
        try:
            doc_cookies = page.evaluate("() => document.cookie")
            if doc_cookies:
                print(f"\nDocument cookies: {doc_cookies[:200]}")
                # Parse for sessionid
                for pair in doc_cookies.split(';'):
                    pair = pair.strip()
                    if pair.startswith('sessionid='):
                        session_id = pair.split('=', 1)[1]
                        break
        except Exception as e:
            print(f"Không thể đọc document.cookie: {e}")

        # If still no session ID found, try localStorage
        if not session_id:
            print("\n🔍 Thử tìm trong localStorage...")
            try:
                local_items = page.evaluate("""() => {
                    const items = {};
                    for (let i = 0; i < localStorage.length; i++) {
                        const key = localStorage.key(i);
                        if (key && (key.toLowerCase().includes('session') || key.toLowerCase().includes('login'))) {
                            items[key] = localStorage.getItem(key);
                        }
                    }
                    return items;
                }""")
                if local_items:
                    print("Tìm thấy localStorage items:")
                    for k, v in local_items.items():
                        print(f"  {k}: {v[:60]}...")
            except Exception as e:
                print(f"Lỗi localStorage: {e}")

        browser.close()

        if session_id:
            print(f"\n{'=' * 60}")
            print(f"✅ Session ID tìm thấy thành công!")
            print(f"Session ID: {session_id}")
            print(f"{'=' * 60}")

            save_session_id(session_id)

            print(f"\n📝 Cách dùng:")
            print(f"1. Truy cập: https://freeforyou.win/tiktok_live/")
            print(f"2. Tab Cấu Hình → Auto Fetch")
            print(f"3. Dán: {session_id[:40]}...")
            print(f"4. Nhấn 'TỰ ĐỘNG LẤY STREAM KEY'")
        else:
            print(f"\n❌ Không tìm thấy sessionid cookie!")
            print(f"\n💡 Nguyên nhân và cách giải quyết:")
            print(f"1. Đăng nhập chưa hoàn tất (CAPTCHA, xác minh 2FA)")
            print(f"2. TikTok chặn đăng nhập từ VPS (IP datacenter)")
            print(f"3. Cookie được lưu ở định dạng khác")
            print(f"\n🔄 Gợi ý:")
            print(f"  - Đăng nhập lại và đảm bảo thấy avatar/tên user ở góc trên")
            print(f"  - Hoặc lấy session ID từ trình duyệt trên máy cá nhân")
            print(f"  - Hoặc dùng Cách 2: lấy stream key thủ công từ live.studio")
            sys.exit(1)


if __name__ == "__main__":
    main()
