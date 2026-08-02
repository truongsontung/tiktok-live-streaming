#!/usr/bin/env python3
"""
Batch resize all avatar images to 32x32 pixels.
Overwrites original files in-place. No backup.
"""
import os
import sys
from PIL import Image

AVATAR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "avatars")
TARGET_SIZE = 32
EXTENSIONS = ('.png', '.jpg', '.jpeg', '.gif')


def resize_image(filepath):
    try:
        img = Image.open(filepath).convert('RGBA')

        # For GIF, get first frame
        if hasattr(img, 'n_frames') and img.n_frames > 1:
            img.seek(0)

        orig_w, orig_h = img.size
        if orig_w == TARGET_SIZE and orig_h == TARGET_SIZE:
            return None  # Already correct size

        # Resize keeping aspect ratio
        canvas = Image.new('RGBA', (TARGET_SIZE, TARGET_SIZE), (0, 0, 0, 0))
        img.thumbnail((TARGET_SIZE, TARGET_SIZE), Image.LANCZOS)
        offset = ((TARGET_SIZE - img.width) // 2, (TARGET_SIZE - img.height) // 2)
        canvas.paste(img, offset, img)

        # Overwrite original
        canvas.save(filepath)

        orig_size = os.path.getsize(filepath)
        return (orig_w, orig_h)
    except Exception as e:
        return f"ERROR: {e}"


def main():
    if not os.path.exists(AVATAR_DIR):
        print(f"Avatar directory not found: {AVATAR_DIR}")
        sys.exit(1)

    files = [f for f in os.listdir(AVATAR_DIR)
             if f.lower().endswith(EXTENSIONS) and os.path.isfile(os.path.join(AVATAR_DIR, f))]

    if not files:
        print("No avatar images found.")
        sys.exit(0)

    print(f"Found {len(files)} avatar images in {AVATAR_DIR}")
    print(f"Target size: {TARGET_SIZE}x{TARGET_SIZE}")
    print("-" * 50)

    resized = 0
    skipped = 0
    errors = 0

    for fname in sorted(files):
        fpath = os.path.join(AVATAR_DIR, fname)
        before_size = os.path.getsize(fpath)
        result = resize_image(fpath)
        after_size = os.path.getsize(fpath)

        if result is None:
            print(f"  SKIP  {fname} (already {TARGET_SIZE}x{TARGET_SIZE})")
            skipped += 1
        elif isinstance(result, tuple):
            orig_w, orig_h = result
            saving = before_size - after_size
            print(f"  OK    {fname}: {orig_w}x{orig_h} -> {TARGET_SIZE}x{TARGET_SIZE}  ({saving:+,} bytes)")
            resized += 1
        else:
            print(f"  FAIL  {fname}: {result}")
            errors += 1

    print("-" * 50)
    print(f"Done: {resized} resized, {skipped} skipped, {errors} errors")


if __name__ == "__main__":
    main()
