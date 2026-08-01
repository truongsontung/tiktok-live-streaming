#!/usr/bin/env python3
"""
Extract TikTok Session ID from browser cookies.
Works with: chromium-browser cookies or Playwright automation.
"""

import os
import sys
import json
import time
import sqlite3
import shutil
import tempfile
import zipfile
from pathlib import Path

def get_tiktok_cookies_from_chromium():
    """Try to read TikTok cookies from chromium browser."""
    cookie_paths = [
        os.path.expanduser("~/.config/chromium/Default/Cookies"),
        os.path.expanduser("~/.config/chromium/Default/Cookies"),
        os.path.expanduser("~/.cache/chromium/Default/Cookies"),
        os.path.expanduser("~/.var/app/org.chromium.Chromium/profile/default/Cookies"),
        os.path.expanduser("~/.var/app/org.chromium.Chromium/config/chromium/Default/Cookies"),
        os.path.expanduser("~/.var/app/org.chromium.Chromium/config/chromium/Default/cookies"),
    ]
    
    for cookie_path in cookie_paths:
        if os.path.exists(cookie_path):
            try:
                # Copy DB to temp location (chromium locks it)
                tmp_db = tempfile.mktemp(suffix=".db")
                shutil.copy2(cookie_path, tmp_db)
                
                conn = sqlite3.connect(tmp_db)
                cursor = conn.cursor()
                
                # Query cookies for tiktok.com
                cursor.execute("""
                    SELECT name, value, host_key 
                    FROM cookies 
                    WHERE host_key LIKE '%tiktok%' 
                    AND name IN ('sessionid', 'sessionid_ss', 'sid_api', 'sid_tt')
                """)
                
                cookies = cursor.fetchall()
                conn.close()
                os.unlink(tmp_db)
                
                if cookies:
                    print("\n=== TikTok Cookies Found ===")
                    for name, value, host in cookies:
                        print(f"  {name}: {value[:50] if len(value) > 50 else value}")
                        if name == "sessionid" and value:
                            return value
                    # sessionid empty (encrypted) — try sid_tt as fallback
                    for name, value, host in cookies:
                        if value and name in ('sid_tt', 'sessionid_ss', 'sid_api'):
                            print(f"  Using {name} as fallback session")
                            return value
            except Exception as e:
                print(f"Error reading {cookie_path}: {e}")
    
    # SQLite failed or sessionid encrypted — fallback to Playwright
    print("Falling back to Playwright for session extraction...")
    return get_tiktok_cookies_from_playwright()

def get_tiktok_cookies_from_playwright(session_id=None):
    """Use Playwright to extract cookies from an already-logged-in session."""
    from playwright.sync_api import sync_playwright
    
    with sync_playwright() as p:
        # Try to use existing chromium with user data dir
        user_data_dir = os.path.expanduser("~/.var/app/org.chromium.Chromium/config/chromium")
        if not os.path.exists(user_data_dir):
            user_data_dir = os.path.expanduser("~/.config/chromium")
        
        if os.path.exists(user_data_dir):
            browser = p.chromium.launch_persistent_context(
                user_data_dir,
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
            )
        else:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
        
        page = browser.new_page()
        
        try:
            # Navigate to TikTok
            page.goto("https://www.tiktok.com/", wait_until="networkidle", timeout=30000)
            time.sleep(2)
            
            # Check if logged in
            try:
                user_avatar = page.query_selector('div[data-e2e="user-avatar"], img[data-e2e="user-avatar"]')
                if user_avatar:
                    print("✓ Đã đăng nhập TikTok!")
                else:
                    print("⚠ Chưa đăng nhập - vui lòng đăng nhập trước")
            except:
                pass
            
            # Get all cookies
            cookies = page.context.cookies("https://www.tiktok.com")
            
            print("\n=== TikTok Cookies (via Playwright) ===")
            session_id = None
            for cookie in cookies:
                if cookie['name'] in ('sessionid', 'sessionid_ss', 'sid_api', 'sid_tt'):
                    print(f"  {cookie['name']}: {cookie['value'][:50]}...")
                    if cookie['name'] == 'sessionid':
                        session_id = cookie['value']
            
            if not session_id:
                # Get all cookies for reference
                print("\n  Tất cả cookies:")
                for cookie in cookies:
                    if 'tiktok' in cookie.get('domain', ''):
                        print(f"  {cookie['name']}: {cookie['value'][:30]}...")
            
            browser.close()
            return session_id
            
        except Exception as e:
            print(f"Lỗi: {e}")
            browser.close()
            return None

if __name__ == "__main__":
    import time
    
    print("Đang tìm Session ID từ trình duyệt...")
    print("1. Thử đọc cookie từ Chromium...")
    
    session_id = get_tiktok_cookies_from_chromium()
    
    if session_id:
        print(f"\n✅ Tìm thấy Session ID: {session_id[:50]}...")
        print(f"\nSession ID đầy đủ: {session_id}")
    else:
        print("\nKhông tìm thấy cookie từ Chromium.")
        print("\n2. Thử dùng Playwright trích xuất từ trình duyệt đang chạy...")
        
        session_id = get_tiktok_cookies_from_playwright()
        
        if session_id:
            print(f"\n✅ Tìm thấy Session ID: {session_id[:50]}...")
            print(f"\nSession ID đầy đủ: {session_id}")
        else:
            print("\n❌ Không thể tự động trích xuất Session ID.")
            print("\n📝 Cách lấy thủ công:")
            print("1. Mở trình duyệt chromium trên VPS")
            print("2. Truy cập https://www.tiktok.com/ và đăng nhập")
            print("3. Nhấn F12 → Tab Application → Cookies")
            print("4. Tìm cookie có tên 'sessionid' → copy giá trị")
            print("5. Hoặc chạy: javascript:navigator.cookie in console -> document.cookie")
            print("6. Dán giá trị vào form 'TikTok Session ID' trong tab Cấu Hình")
            sys.exit(1)
    
    # Save to file for easy access
    with open("/home/vps2/tiktok_live/tiktok_session.txt", "w") as f:
        f.write(session_id)
    print(f"\nĐã lưu Session ID vào: /home/vps2/tiktok_live/tiktok_session.txt")
