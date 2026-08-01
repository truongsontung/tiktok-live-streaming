#!/usr/bin/env python3
"""Diagnostic script to check TikTok Live Studio page."""

import sys
import os
import time
import re

sys.path.insert(0, '/home/vps2/tiktok_live')

from playwright.sync_api import sync_playwright

session_file = "/home/vps2/tiktok_live/tiktok_session.txt"

if not os.path.exists(session_file):
    print("No session file found")
    sys.exit(1)

with open(session_file) as f:
    sess = f.read().strip()

print(f"Session ID: {sess[:30]}...")

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage']
    )
    context = browser.new_context(
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
    )
    context.add_cookies([
        {"name": "sessionid", "value": sess, "domain": ".tiktok.com", "path": "/",
         "httpOnly": False, "secure": True, "sameSite": "None"},
    ])

    page = context.new_page()
    page.goto('https://www.tiktok.com/live/studio', wait_until='domcontentloaded', timeout=20000)
    time.sleep(3)

    print(f"Page title: {page.title()}")
    print(f"URL: {page.url}")

    content = page.content()
    print(f"Content length: {len(content)} chars")

    # Look for stream key patterns
    rtmp_matches = re.findall(r'(rtmp://[^\s"\'<>]+)', content)
    live_key_matches = re.findall(r'(live_[a-zA-Z0-9]+)', content)

    if rtmp_matches:
        print(f"Found RTMP URLs: {rtmp_matches[:3]}")
    if live_key_matches:
        print(f"Found Live keys: {live_key_matches[:3]}")

    # Also check for any text mentioning requirements
    if '1000' in content:
        print("⚠ Page mentions '1000' - may require 1000 followers")
    if 'eligib' in content.lower():
        print("⚠ Page mentions eligibility requirements")
    if 'not.*live' in content.lower() or 'cannot.*live' in content.lower():
        print("⚠ Page mentions live is not available")

    # Save screenshot
    page.screenshot(path='/home/vps2/tiktok_live/logs/live_studio_screen.png')
    print("Screenshot saved: /home/vps2/tiktok_live/logs/live_studio_screen.png")

    # Get any API responses
    @page.on("response")
    def capture(resp):
        if resp.status == 200 and 'webcast' in resp.url:
            try:
                data = resp.json()
                if isinstance(data, dict) and 'data' in data:
                    stream_url = data['data'].get('stream_url', {})
                    if stream_url:
                        print(f"💰 Found stream_url in API response!")
                        rtmp = stream_url.get('rtmp_push_url', '')
                        if rtmp:
                            print(f"  RTMP: {rtmp[:80]}...")
                            if "/live/" in rtmp:
                                parts = rtmp.rsplit("/live/", 1)
                                rtmp_url = parts[0] + "/live/"
                                stream_key = parts[1]
                                print(f"  RTMP URL: {rtmp_url}")
                                print(f"  Stream Key: {stream_key}")
            except:
                pass

    # Wait for any network activity
    time.sleep(5)

    browser.close()
    print("Done.")
