# SonicGrabber Pro 6.1.0 — Linux build

A full redesign: new interface, a proper download queue, better tagging and fixes for YouTube's new requirements. See the [changelog](https://github.com/anshsharma-git/python-music-downloader/blob/main/CHANGELOG.md) for details.

## Install (Linux x86_64)
1. Download **SonicGrabber-Pro-6.1.0-linux-x86_64.tar.gz** below and extract it (right-click → *Extract Here*).
2. Install FFmpeg once: `sudo apt install ffmpeg`
3. Open a terminal in the extracted folder and run `./SonicGrabber`
   (if you get *Permission denied*: `chmod +x SonicGrabber`)
4. Using YouTube? If a yellow **Fix YouTube downloads** button appears in the sidebar, click it once.

**Requires** a 64-bit Linux with glibc 2.38 or newer (Ubuntu 24.04+, Linux Mint 22+, Fedora 39+, Debian 13+, Arch/Manjaro). On older systems run `./build.sh` to build it for your machine, or run from source.

**Verify the download:** `sha256sum -c SHA256SUMS.txt`
