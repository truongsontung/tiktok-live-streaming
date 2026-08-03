#!/bin/bash
# Download YouTube video/audio
# Usage: ./yt.sh URL [URL2 URL3...]

if [ -z "$1" ]; then
    echo "Usage: $0 <youtube-url> [url2 url3...]"
    exit 1
fi

# Auto copy latest cookies from Downloads
LATEST=$(ls -t ~/Downloads/cookies*.txt 2>/dev/null | head -1)
if [ -n "$LATEST" ]; then
    cp "$LATEST" ~/tiktok_live/cookies.txt
    echo "Copied cookies from $LATEST"
else
    echo "Error: No cookies.txt found in ~/Downloads/"
    echo "Export cookies from Chromium using Get cookies.txt extension"
    exit 1
fi

cd ~/tiktok_live

for url in "$@"; do
    echo ">>> Downloading: $url"
    yt-dlp \
        --cookies cookies.txt \
        --js-runtimes node \
        -f "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/best[ext=mp4]/best" \
        -o "media/%(title)s.%(ext)s" \
        --merge-output-format mp4 \
        "$url"
    echo ""
done
