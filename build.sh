#!/usr/bin/env bash
# Builds a standalone SonicGrabber executable for THIS machine (best compatibility with your distro).
# Needs: python3, python3-venv, python3-tk   (Debian/Ubuntu: sudo apt install python3-venv python3-tk)
set -euo pipefail
cd "$(dirname "$0")"
python3 -c "import tkinter" 2>/dev/null || { echo "tkinter missing: sudo apt install python3-tk"; exit 1; }
python3 -m venv .buildenv
. .buildenv/bin/activate
pip install -q -U pip
pip install -q -U customtkinter "yt-dlp[default]" pillow mutagen syncedlyrics certifi secretstorage pyinstaller
pyinstaller --noconfirm --clean --onefile --name SonicGrabber \
  --collect-all customtkinter --collect-all certifi --collect-all yt_dlp_ejs \
  --collect-submodules yt_dlp --collect-submodules syncedlyrics --collect-submodules mutagen \
  --hidden-import PIL._tkinter_finder \
  main.py
cp dist/SonicGrabber ./SonicGrabber
echo
echo "Built: $(pwd)/SonicGrabber   (run it, or: ./SonicGrabber --selftest)"
