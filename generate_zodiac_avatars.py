#!/usr/bin/env python3
"""
Download and process 48 zodiac animal images (4 categories × 12 animals)
from inkythuatso.com into static/avatars/ as transparent PNG 200×200.
"""
import os
import re
import io
import time
import requests
from PIL import Image, ImageOps, ImageFilter

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AVATAR_DIR = os.path.join(BASE_DIR, "static", "avatars")
os.makedirs(AVATAR_DIR, exist_ok=True)

SOURCE_URL = "https://inkythuatso.com/hinh-anh-dep/anh-12-con-giap-de-thuong-4746.html"

ZODIAC_ANIMALS = [
    (1, "rat", "Tý", "Chuột"),
    (2, "ox", "Sửu", "Trâu"),
    (3, "tiger", "Dần", "Hổ"),
    (4, "cat", "Mão", "Mèo"),
    (5, "dragon", "Thìn", "Rồng"),
    (6, "snake", "Tỵ", "Rắn"),
    (7, "horse", "Ngọ", "Ngựa"),
    (8, "goat", "Mùi", "Dê"),
    (9, "monkey", "Thân", "Khỉ"),
    (10, "rooster", "Dậu", "Gà"),
    (11, "dog", "Tuất", "Chó"),
    (12, "pig", "Hỏi", "Heo"),
]

CATEGORIES = ["trung-quoc", "chien-binh", "hoang-gia", "cute-2023"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

OUTPUT_SIZE = 200


def fetch_image_urls() -> dict:
    """Fetch the HTML page and extract image URLs grouped by category."""
    resp = requests.get(SOURCE_URL, headers=HEADERS, timeout=30)
    html = resp.text

    pattern = (
        r'https://inkythuatso\.com/uploads/thumbnails/800/'
        r'2023/02/(\d+)-hinh-anh-12-con-giap-([\w-]+)-inkythuatso-(\d{2}-\d{2}-\d{2}-\d{2})\.jpg'
    )
    matches = re.findall(pattern, html)

    urls = {cat: {} for cat in CATEGORIES}
    for num_str, cat, ts in matches:
        num = int(num_str)
        url = (
            f"https://inkythuatso.com/uploads/thumbnails/800/2023/02/"
            f"{num}-hinh-anh-12-con-giap-{cat}-inkythuatso-{ts}.jpg"
        )
        if cat in urls:
            urls[cat][num] = url

    total = sum(len(v) for v in urls.values())
    print(f"Found {total} zodiac image URLs across {len(CATEGORIES)} categories")
    for cat in CATEGORIES:
        nums = sorted(urls[cat].keys())
        print(f"  {cat}: {len(nums)} images (numbers: {nums})")
    return urls


def remove_background(img: Image.Image) -> Image.Image:
    """Remove dark background from image using color-distance mask."""
    img_rgba = img.convert("RGBA")
    pixels = img_rgba.load()
    w, h = img_rgba.size

    bg_samples = [
        pixels[0, 0], pixels[w - 1, 0],
        pixels[0, h - 1], pixels[w - 1, h - 1],
        pixels[w // 2, 0], pixels[0, h // 2],
        pixels[w - 1, h // 2], pixels[w // 2, h - 1],
    ]

    avg_brightness = sum(sum(p[:3]) for p in bg_samples) / (len(bg_samples) * 3)
    bg_color = tuple(int(v) for v in [avg_brightness] * 3 + [255])

    threshold = 45
    mask = Image.new("L", (w, h), 0)
    mask_data = []
    for y in range(h):
        for x in range(w):
            r, g, b, _ = pixels[x, y]
            dist = abs(r - bg_color[0]) + abs(g - bg_color[1]) + abs(b - bg_color[2])
            alpha = min(255, max(0, (dist - threshold) * 8))
            mask_data.append(alpha)
    mask.putdata(mask_data)

    mask = mask.filter(ImageFilter.GaussianBlur(radius=1))

    result = img_rgba.copy()
    result.putalpha(mask)

    bbox = mask.getbbox()
    if bbox:
        result = result.crop(bbox)
        result = ImageOps.expand(result, border=10, fill=(0, 0, 0, 0))

    return result


def process_image(url: str, save_path: str) -> bool:
    """Download, process, and save a single avatar image."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code != 200:
            print(f"  FAIL ({resp.status_code}): {url}")
            return False

        img = Image.open(io.BytesIO(resp.content))
        img = img.convert("RGB")

        if max(img.size) > OUTPUT_SIZE * 2:
            scale = (OUTPUT_SIZE * 2) / max(img.size)
            img = img.resize((int(img.size[0] * scale), int(img.size[1] * scale)), Image.LANCZOS)

        img = remove_background(img)

        canvas = Image.new("RGBA", (OUTPUT_SIZE, OUTPUT_SIZE), (0, 0, 0, 0))
        paste_x = (OUTPUT_SIZE - img.width) // 2
        paste_y = (OUTPUT_SIZE - img.height) // 2
        canvas.paste(img, (paste_x, paste_y), img)

        canvas.save(save_path, "PNG", optimize=True)
        size_kb = os.path.getsize(save_path) / 1024
        print(f"  OK: {os.path.basename(save_path)} ({img.size[0]}x{img.size[1]} -> {OUTPUT_SIZE}x{OUTPUT_SIZE}, {size_kb:.1f}KB)")
        return True

    except Exception as e:
        print(f"  ERROR: {url} -> {e}")
        return False


def main():
    print("=" * 60)
    print("Downloading & processing 48 zodiac animal avatars (4×12)")
    print("Source: inkythuatso.com")
    print("=" * 60)

    urls = fetch_image_urls()

    total_ok = 0
    total_fail = 0

    for cat in CATEGORIES:
        print(f"\n--- Category: {cat} ---")
        for num, _english, zodiac_name, animal_name in ZODIAC_ANIMALS:
            url = urls.get(cat, {}).get(num)
            if not url:
                print(f"  SKIP: {cat} image #{num}")
                total_fail += 1
                continue

            safe_animal = animal_name.lower().replace(" ", "_")
            filename = f"zodiac_{cat}_{num:02d}_{safe_animal}.png"
            save_path = os.path.join(AVATAR_DIR, filename)

            if process_image(url, save_path):
                total_ok += 1
            else:
                total_fail += 1

            time.sleep(0.2)

    print(f"\n{'=' * 60}")
    print(f"Done! Success: {total_ok}, Failed: {total_fail}, Total: {total_ok + total_fail}")
    print(f"Saved to: {AVATAR_DIR}")
    print(f"Total files in avatar dir: {len(os.listdir(AVATAR_DIR))}")
    print("=" * 60)


if __name__ == "__main__":
    main()
