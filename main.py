#!/usr/bin/env python3
"""
SonicGrabber Pro 6.1 — modern music downloader for Linux (yt-dlp + customtkinter)

Install (Debian/Ubuntu):
    sudo apt install python3-tk ffmpeg mpv      # mpv is optional (in-app playback)
    pip install -U customtkinter yt-dlp pillow mutagen syncedlyrics

Usage:      python3 main.py [-d] ["search text" | link]
Shortcuts:  Ctrl+F search · Ctrl+V paste & go (in search box) · Ctrl+1..5 switch pages
            Ctrl+A select all results · Esc clear search
"""
from __future__ import annotations

import argparse
import base64
import difflib
import glob
import hashlib
import io
import logging
import os
import queue
import re
import shutil
import platform
import ssl
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import json
import urllib.parse
import urllib.request
import zipfile
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional

if sys.version_info < (3, 9):
    sys.exit(f"SonicGrabber needs Python 3.9+ (you have {sys.version.split()[0]}).")

if getattr(sys, "frozen", False):  # packaged build: use bundled CA certificates for HTTPS
    try:
        import certifi
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    except Exception:
        pass

try:
    import customtkinter as ctk
    import tkinter as tk
    from tkinter import filedialog, messagebox
    import yt_dlp
except ImportError as _e:  # pragma: no cover
    sys.exit(
        f"Missing dependency: {_e.name}\n"
        "  sudo apt install python3-tk ffmpeg\n"
        "  pip install -U customtkinter yt-dlp pillow mutagen syncedlyrics"
    )

try:
    from PIL import Image, ImageDraw, ImageOps
    PIL_OK = True
except Exception:
    PIL_OK = False

try:
    import syncedlyrics
    LYRICS_OK = True
except Exception:
    LYRICS_OK = False

try:
    from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, TDRC, TPE2, TRCK, APIC, USLT
    from mutagen.flac import FLAC, Picture
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggopus import OggOpus
    MUTAGEN_OK = True
except Exception:
    MUTAGEN_OK = False

# ───────────────────────────── constants ─────────────────────────────
APP, VER = "SonicGrabber Pro", "6.1.0"


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "sonicgrabber"
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "sonicgrabber"
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache") / "sonicgrabber" / "thumbs"
CFG_FILE, DB_FILE, LOG_FILE = CONFIG_DIR / "config.json", DATA_DIR / "history.db", DATA_DIR / "app.log"
QUEUE_FILE = DATA_DIR / "queue.json"

PRESETS = {
    "MP3 · 320 kbps":  dict(codec="mp3",  q="320", ext="mp3"),
    "MP3 · 192 kbps":  dict(codec="mp3",  q="192", ext="mp3"),
    "M4A · AAC 256":   dict(codec="m4a",  q="256", ext="m4a"),
    "M4A · AAC 128":   dict(codec="m4a",  q="128", ext="m4a"),
    "FLAC · Lossless": dict(codec="flac", q="0",   ext="flac"),
    "Opus · Best":     dict(codec="opus", q="0",   ext="opus"),
}
SCALES = {"90%": 0.9, "100%": 1.0, "110%": 1.1, "125%": 1.25, "150%": 1.5}
SOURCES = ["YouTube", "SoundCloud"]
BROWSERS = ["None", "firefox", "chrome", "chromium", "brave", "edge", "vivaldi", "opera"]
RATES = {"Unlimited": None, "500 KB/s": 500_000, "1 MB/s": 1_000_000, "2 MB/s": 2_000_000, "5 MB/s": 5_000_000}
LIB_ORDERS = {"Newest": "ts DESC", "Oldest": "ts ASC", "Title A–Z": "title COLLATE NOCASE",
              "Artist A–Z": "artist COLLATE NOCASE, title COLLATE NOCASE", "Largest": "size DESC"}


def default_music_dir() -> str:
    try:
        out = subprocess.run(["xdg-user-dir", "MUSIC"], capture_output=True, text=True, timeout=2).stdout.strip()
        if out and Path(out) != Path.home():
            return str(Path(out) / "SonicGrabber")
    except Exception:
        pass
    return str(Path.home() / "Music" / "SonicGrabber")


DEFAULTS = {
    "output_dir": default_music_dir(), "preset": "MP3 · 320 kbps", "organize": "Flat",
    "name_style": "Artist - Title", "embed_tags": True, "embed_cover": True, "square_cover": True,
    "embed_lyrics": True, "save_lrc": False, "normalize": False, "skip_existing": True,
    "concurrency": "2", "notify": True, "appearance": "Dark", "ui_scale": "100%",
    "source": "YouTube", "songs_only": True, "result_count": "25", "recent": [],
    "browser": "None", "ratelimit": "Unlimited", "retries": "3", "musicbrainz": True,
    "sponsorblock": False, "watch_clipboard": True,
    "geometry": "1180x780",
}

# (light, dark) colour pairs — customtkinter switches automatically
BG = ("#f3f3f8", "#0b0b0e")
SIDEBAR = ("#ffffff", "#121216")
CARD = ("#ffffff", "#17171c")
CARD2 = ("#eeeef5", "#1f1f27")
HOVER = ("#e6e6f0", "#24242d")
BORDER = ("#d9d9e4", "#2b2b35")
TEXT = ("#17171c", "#f2f2f6")
DIM = ("#6c6c7a", "#8e8e9a")
ACCENT, ACCENT_H = "#7c5cfc", "#6747e8"
GREEN, GREEN_H, RED, AMBER = "#22c55e", "#16a34a", "#ef4444", "#f59e0b"

ACTIVE = ("downloading", "processing", "tagging")
FINISHED = ("done", "skipped", "cancelled")
HARD_SKIP = ("interview", "behind the scenes", "documentary", "reaction", "commentary",
             "making of", "full album", "podcast", "compilation", "1 hour", "livestream")

# ───────────────────────────── logging ─────────────────────────────
log = logging.getLogger("sg")
log.setLevel(logging.DEBUG)


class UIHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__(logging.INFO)
        self.sink = sink

    def emit(self, record):
        try:
            self.sink(f"[{datetime.now():%H:%M:%S}] {record.getMessage()}", record.levelno)
        except Exception:
            pass


# ───────────────────────────── helpers ─────────────────────────────
class Cancelled(Exception):
    pass


ANSI = re.compile(r"\x1b\[[0-9;]*m")
NOISE = re.compile(
    r"\s*[\(\[\{][^\)\]\}]*\b(official|lyrics?|lyric video|audio|video|visuali[sz]er|music video|"
    r"hd|hq|4k|remaster(?:ed)?|explicit)\b[^\)\]\}]*[\)\]\}]", re.I)


def load_cfg() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CFG_FILE.read_text("utf-8")))
    except Exception:
        pass
    if cfg["preset"] not in PRESETS:
        cfg["preset"] = DEFAULTS["preset"]
    if cfg["ui_scale"] not in SCALES:
        cfg["ui_scale"] = "100%"
    if str(cfg["concurrency"]) not in "12345":
        cfg["concurrency"] = "2"
    cfg["concurrency"] = str(cfg["concurrency"])
    return cfg


def save_cfg(cfg: dict):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CFG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), "utf-8")
        tmp.replace(CFG_FILE)
    except Exception as e:
        log.warning("Could not save settings: %s", e)


def safe_name(n: str) -> str:
    return re.sub(r'[\\/*?:"<>|\x00-\x1f]', "", n).strip().strip(".")[:150] or "Untitled"


def shorten(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def shorten_left(s: str, n: int = 38) -> str:
    return s if len(s) <= n else "…" + s[-(n - 1):]


def fmt_dur(s) -> str:
    if not s:
        return ""
    m, sec = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_size(b: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if b < 1024 or u == "GB":
            return f"{b:.0f} {u}" if u == "B" else f"{b:.1f} {u}"
        b /= 1024
    return ""


def fmt_eta(s) -> str:
    m, sec = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def is_url(t: str) -> bool:
    return t.startswith(("http://", "https://"))


def clean_err(e) -> str:
    s = ANSI.sub("", str(e)).strip().replace("ERROR: ", "")
    low = s.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return "YouTube requested a sign-in check. Update yt-dlp (Settings) or retry later."
    if "ffmpeg" in low and ("not found" in low or "not installed" in low):
        return "FFmpeg is missing — install it and retry."
    if "403" in low and "forbidden" in low:
        return "YouTube blocked this download (HTTP 403). " + (
            "Try again later, or pick your browser under Settings → Browser cookies." if JS_OPTS else
            "No JavaScript runtime found — click “Fix YouTube downloads”.")
    if "requested format is not available" in low:
        return "YouTube returned no downloadable audio for this video — see the Activity log for the reason."
    if "private video" in low or "video unavailable" in low:
        return "This video is private or unavailable."
    line = s.splitlines()[0] if s else e.__class__.__name__
    return shorten(line, 160)


def clean_title(t: str) -> str:
    t = NOISE.sub("", t or "")
    return re.sub(r"\s{2,}", " ", t).strip(" -–—|")


def derive_meta(info: dict) -> dict:
    raw = info.get("track") or info.get("title") or ""
    title = clean_title(raw) or raw or "Unknown"
    artist = info.get("artist") or info.get("creator") or ""
    if isinstance(artist, list):
        artist = ", ".join(artist)
    if not artist and not info.get("track"):
        m = re.match(r"^(.{1,60}?)\s+[-–—]\s+(.+)$", title)
        if m:
            artist, title = m.group(1).strip(), m.group(2).strip()
    if not artist:
        artist = re.sub(r"\s*-\s*Topic$", "", (info.get("channel") or info.get("uploader") or "")).strip()
    year = str(info.get("release_year") or (info.get("upload_date") or "")[:4] or "")
    return {"title": title, "artist": artist, "album": info.get("album") or "", "year": year}


def entry_url(e: dict) -> str:
    for k in ("webpage_url", "url"):
        u = e.get(k)
        if isinstance(u, str) and u.startswith("http"):
            return u
    vid = e.get("id")
    if vid and "youtube" in str(e.get("ie_key") or e.get("extractor") or "youtube").lower():
        return f"https://www.youtube.com/watch?v={vid}"
    return ""


def http_get(url: str, timeout: int = 10, ua: str = "Mozilla/5.0") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


FROZEN = bool(getattr(sys, "frozen", False))


def clean_env() -> dict:
    """Environment for child processes. PyInstaller points LD_LIBRARY_PATH at its bundled libs, which can
    break system tools (ffmpeg, mpv, xdg-open), so restore the original value."""
    env = dict(os.environ)
    if FROZEN:
        if "LD_LIBRARY_PATH_ORIG" in env:
            env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


def open_path(p):
    try:
        if sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=clean_env())
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            os.startfile(str(p))  # type: ignore[attr-defined]
    except Exception as e:
        log.warning("Could not open %s: %s", p, e)


def desktop_notify(title: str, body: str):
    exe = shutil.which("notify-send")
    if exe:
        try:
            subprocess.Popen([exe, "-a", "SonicGrabber", "-i", "audio-x-generic", title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=clean_env())
        except Exception:
            pass


def find_ffmpeg() -> Optional[str]:
    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not (ff and fp):
        return None
    if not FROZEN:
        return str(Path(ff).resolve().parent)
    # packaged build: call system ffmpeg through tiny wrappers that restore the original library path
    d = DATA_DIR / "bin"
    d.mkdir(parents=True, exist_ok=True)
    for name, real in (("ffmpeg", ff), ("ffprobe", fp)):
        w = d / name
        w.write_text('#!/bin/sh\nif [ -n "${LD_LIBRARY_PATH_ORIG+x}" ]; then export LD_LIBRARY_PATH="$LD_LIBRARY_PATH_ORIG"; '
                     f'else unset LD_LIBRARY_PATH; fi\nexec "{real}" "$@"\n', "utf-8")
        w.chmod(0o755)
    return str(d)


def ffmpeg_install_cmd() -> str:
    try:
        txt = Path("/etc/os-release").read_text().lower()
        ids = " ".join(re.findall(r"^(?:id|id_like)=(.*)$", txt, re.M)).replace('"', "")
    except Exception:
        ids = ""
    if any(k in ids for k in ("debian", "ubuntu", "mint")):
        return "sudo apt install ffmpeg"
    if any(k in ids for k in ("fedora", "rhel", "centos")):
        return "sudo dnf install ffmpeg   # needs RPM Fusion"
    if "arch" in ids:
        return "sudo pacman -S ffmpeg"
    if "suse" in ids:
        return "sudo zypper install ffmpeg"
    return "install ffmpeg with your package manager"


# ───────────────────────────── extras ─────────────────────────────
BAD_WORDS = ("live", "karaoke", "instrumental", "sped up", "slowed", "reverb", "8d", "nightcore",
             "cover", "remix", "mashup", "tutorial", "lesson", "reaction")


def rank_entry(e: dict, q: str) -> float:
    """Prefer clean studio uploads (Topic / official audio) over live, cover and remix versions."""
    t = (e.get("title") or "").lower()
    ch = (e.get("channel") or e.get("uploader") or "").lower()
    ql, score = q.lower(), 0.0
    if ch.endswith("- topic"):
        score += 3
    if "official audio" in t or "(audio)" in t:
        score += 1.5
    if "vevo" in ch or "official" in ch:
        score += 0.5
    for w in BAD_WORDS:
        if w in t and w not in ql:
            score -= 2
    return score


def transient(e) -> bool:
    s = str(e).lower()
    return not any(k in s for k in ("private video", "video unavailable", "unavailable in your", "copyright",
                                    "sign in", "confirm your age", "age-restricted", "ffmpeg",
                                    "has been removed", "members-only", "premiere"))


def ydl_common(cfg: dict) -> dict:
    o: dict = {}
    r = RATES.get(cfg.get("ratelimit", "Unlimited"))
    if r:
        o["ratelimit"] = r
    if JS_OPTS:
        o["js_runtimes"] = dict(JS_OPTS)
    return o


class _CookieLog:
    def debug(self, m): pass
    def info(self, m): log.info("Cookies: %s", m)
    def warning(self, m): log.warning("Cookies: %s", m)
    def error(self, m): log.error("Cookies: %s", m)


class CookieStore:
    """Reads the browser's cookies ONCE, in the background and with a timeout, then shares them in memory.
    (Reading them separately for every download made each job wait on the system keyring and could hang.)"""
    TIMEOUT = 35

    def __init__(self):
        self.browser, self.jar, self.state, self.error, self.gen = "None", None, "idle", "", 0
        self.event = threading.Event()
        self.event.set()

    def load(self, browser, on_done=None):
        self.gen += 1
        gen = self.gen
        self.browser, self.jar, self.error = browser, None, ""
        if browser in (None, "", "None"):
            self.state = "idle"
            self.event.set()
            return
        self.state = "loading"
        self.event.clear()
        log.info("Reading cookies from %s…", browser)

        def work():
            res: dict = {}

            def read():
                try:
                    res["jar"] = yt_dlp.cookies.extract_cookies_from_browser(browser, logger=_CookieLog())
                except Exception as e:
                    res["err"] = e

            t = threading.Thread(target=read, daemon=True)
            t.start()
            t.join(self.TIMEOUT)
            if gen != self.gen:
                return
            if t.is_alive():
                self.state = "failed"
                self.error = "timed out — the browser's password keyring may be waiting for you to unlock it"
            elif "err" in res:
                self.state, self.error = "failed", clean_err(res["err"])
            else:
                self.jar, self.state = res["jar"], "ready"
                log.info("Loaded %d cookies from %s", len(self.jar), browser)
            if self.state == "failed":
                log.warning("Browser cookies unavailable (%s) — continuing without them", self.error)
            self.event.set()
            if on_done:
                on_done(self)

        threading.Thread(target=work, daemon=True).start()

    def get(self):
        return self.jar if self.state == "ready" else None


COOKIES = CookieStore()


def new_ydl(opts: dict, cookies: bool = True):
    """YoutubeDL with the shared in-memory browser cookies attached."""
    y = yt_dlp.YoutubeDL(opts)
    jar = COOKIES.get() if cookies else None
    if jar is not None:
        for c in jar:
            y.cookiejar.set_cookie(c)
    return y


def run_timeout(fn, secs: float, what: str):
    """Run fn in a helper thread; raise TimeoutError instead of hanging forever."""
    res: dict = {}

    def w():
        try:
            res["v"] = fn()
        except BaseException as e:
            res["e"] = e

    t = threading.Thread(target=w, daemon=True)
    t.start()
    t.join(secs)
    if t.is_alive():
        raise TimeoutError(f"Timed out while {what}")
    if "e" in res:
        raise res["e"]
    return res.get("v")


# YouTube needs a JavaScript runtime to solve its download challenge; without one every download is
# rejected with "HTTP Error 403". yt-dlp accepts Deno >= 2.3, Node >= 22 or Bun >= 1.2.11.
JS_MIN = {"deno": (2, 3, 0), "node": (22, 0, 0), "bun": (1, 2, 11)}
JS_OPTS: dict = {}
JS_INFO: Optional[tuple] = None
CLIENT_FALLBACKS = [None, ["web_embedded", "web", "tv"], ["mweb", "tv"]]
# Approaches tried in turn when YouTube returns no usable audio: (player clients, send browser cookies?)
PLANS = [(None, True), (None, False), (["web_embedded", "web", "tv"], False), (["mweb", "tv"], True),
         (["android_vr", "tv"], False)]


def yt_args(clients) -> dict:
    return {"extractor_args": {"youtube": {"player_client": clients}}} if clients else {}


def js_version(path: str) -> Optional[tuple]:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=8,
                             env=clean_env()).stdout
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
        return tuple(int(x) for x in m.groups()) if m else None
    except Exception:
        return None


def find_js_runtime() -> Optional[tuple]:
    cands = [("deno", DATA_DIR / "runtime" / "deno"), ("deno", Path.home() / ".deno" / "bin" / "deno")]
    cands += [(n, Path(w)) for n in JS_MIN for w in [shutil.which(n)] if w]
    seen = set()
    for name, p in cands:
        if str(p) in seen or not p.exists():
            continue
        seen.add(str(p))
        v = js_version(str(p))
        if v and v >= JS_MIN[name]:
            return name, str(p), v
    return None


def refresh_js():
    global JS_INFO
    JS_OPTS.clear()
    JS_INFO = find_js_runtime()
    if JS_INFO:
        name, path, _ = JS_INFO
        if FROZEN:  # run it through a wrapper that restores the system library path
            d = DATA_DIR / "bin"
            d.mkdir(parents=True, exist_ok=True)
            w = d / f"js-{name}"
            w.write_text('#!/bin/sh\nif [ -n "${LD_LIBRARY_PATH_ORIG+x}" ]; then export LD_LIBRARY_PATH="$LD_LIBRARY_PATH_ORIG"; '
                         f'else unset LD_LIBRARY_PATH; fi\nexec "{path}" "$@"\n', "utf-8")
            w.chmod(0o755)
            path = str(w)
        JS_OPTS[name] = {"path": path}


MB_UA = f"SonicGrabberPro/{VER} (personal desktop app)"
_mb_lock, _mb_last = threading.Lock(), [0.0]


def mb_lookup(artist: str, title: str) -> Optional[dict]:
    """Best-effort MusicBrainz match → album, album artist, year, track no., release id."""
    if not title:
        return None
    clean = lambda x: x.replace('"', " ")
    q = f'recording:"{clean(title)}"' + (f' AND artist:"{clean(artist)}"' if artist else "")
    url = "https://musicbrainz.org/ws/2/recording?" + urllib.parse.urlencode({"query": q, "fmt": "json", "limit": 6})
    with _mb_lock:  # MusicBrainz asks for at most 1 request per second
        gap = 1.1 - (time.monotonic() - _mb_last[0])
        if gap > 0:
            time.sleep(gap)
        try:
            data = json.loads(http_get(url, 10, MB_UA))
        finally:
            _mb_last[0] = time.monotonic()
    sim = lambda a, b: difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()
    for r in data.get("recordings", []):
        if int(r.get("score", 0)) < 90 or sim(r.get("title", ""), title) < 0.85:
            continue
        credit = "".join((c.get("name") or "") + (c.get("joinphrase") or "") for c in r.get("artist-credit", []))
        if artist and artist.lower() not in credit.lower() and sim(credit, artist) < 0.6:
            continue
        rels = r.get("releases") or []
        rels = [x for x in rels if x.get("status") == "Official"] or rels
        rels.sort(key=lambda x: (0 if (x.get("release-group") or {}).get("primary-type") == "Album" else 1,
                                 x.get("date") or "9999"))
        if not rels:
            continue
        rel, trk = rels[0], ""
        try:
            trk = str(rel["media"][0]["track"][0]["number"])
        except Exception:
            pass
        return {"album": rel.get("title", ""), "albumartist": credit, "year": (rel.get("date") or "")[:4],
                "track": trk if trk.isdigit() else "", "release": rel.get("id", "")}
    return None


def mb_cover(mb: Optional[dict]) -> List[str]:
    return [f"https://coverartarchive.org/release/{mb['release']}/front-500"] if mb and mb.get("release") else []


class Player:
    """Tiny audio player using mpv (preferred) or ffplay — no extra Python packages."""

    def __init__(self, on_change):
        self.proc: Optional[subprocess.Popen] = None
        self.on_change = on_change

    @staticmethod
    def available() -> Optional[str]:
        return shutil.which("mpv") or shutil.which("ffplay")

    def play(self, src: str, title: str, ua: Optional[str] = None):
        self.stop()
        mpv = shutil.which("mpv")
        if mpv:
            cmd = [mpv, "--no-video", "--really-quiet", "--no-terminal"] + \
                  ([f"--user-agent={ua}"] if ua else []) + [src]
        else:
            cmd = [shutil.which("ffplay") or "ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"] + \
                  (["-user_agent", ua] if ua else []) + [src]
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=clean_env())
        self.proc = p
        self.on_change(title)
        threading.Thread(target=self._wait, args=(p,), daemon=True).start()

    def _wait(self, p):
        p.wait()
        if self.proc is p:
            self.proc = None
            self.on_change(None)

    def stop(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            self.on_change(None)


def make_icon(path: Path, size: int = 256):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d, w = ImageDraw.Draw(img), size
    d.rounded_rectangle((0, 0, w - 1, w - 1), radius=w // 4, fill=(124, 92, 252, 255))
    d.ellipse((w * .26, w * .56, w * .50, w * .78), fill="white")
    d.rectangle((w * .46, w * .22, w * .51, w * .68), fill="white")
    d.polygon([(w * .46, w * .22), (w * .76, w * .32), (w * .76, w * .44), (w * .46, w * .34)], fill="white")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def install_launcher() -> Path:
    icon = DATA_DIR / "icon.png"
    if PIL_OK and not icon.exists():
        make_icon(icon)
    apps = _xdg("XDG_DATA_HOME", ".local/share") / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    f = apps / "sonicgrabber.desktop"
    exec_cmd = f'"{sys.executable}"' if FROZEN else f'"{sys.executable}" "{Path(__file__).resolve()}"'
    f.write_text("[Desktop Entry]\nType=Application\nName=SonicGrabber Pro\n"
                 "Comment=Download music with tags and cover art\n"
                 f'Exec={exec_cmd}\nIcon={icon}\nTerminal=false\n'
                 "Categories=AudioVideo;Audio;Network;\nStartupWMClass=sonicgrabber\n", "utf-8")
    f.chmod(0o755)
    return f


# ───────────────────────────── tagging ─────────────────────────────
def make_cover(data: bytes, square: bool) -> Optional[bytes]:
    if not data:
        return None
    if not PIL_OK:
        return data if data[:3] == b"\xff\xd8\xff" else None
    img = Image.open(io.BytesIO(data)).convert("RGB")
    if min(img.size) < 120:
        return None
    if square:
        s = min(img.size)
        l, t = (img.width - s) // 2, (img.height - s) // 2
        img = img.crop((l, t, l + s, t + s))
    if max(img.size) > 1000:
        img.thumbnail((1000, 1000))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92, progressive=False)  # baseline JPEG: widest player support
    return buf.getvalue()


def cover_candidates(info: dict) -> List[str]:
    urls = [info.get("thumbnail")] + [t.get("url") for t in reversed(info.get("thumbnails") or [])][:5]
    vid = info.get("id")
    if vid and "youtu" in str(info.get("webpage_url", "")):
        urls += [f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg", f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"]
    seen, out = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    out.sort(key=lambda u: 0 if re.search(r"\.jpe?g(\?|$)", u) else 1)
    return out


def plain_lyrics(txt: str) -> str:
    lines = []
    for ln in txt.splitlines():
        if re.match(r"^\[[a-zA-Z]{2,}:.*\]$", ln.strip()):
            continue
        lines.append(re.sub(r"\[\d+:\d+(?:[.:]\d+)?\]|<\d+:\d+(?:[.:]\d+)?>", "", ln).strip())
    return "\n".join(lines).strip()


def tag_file(path: Path, meta: dict, cover: Optional[bytes], lyrics: Optional[str], write_tags: bool):
    if not MUTAGEN_OK or not (write_tags or cover or lyrics):
        return
    ext = path.suffix.lower()
    title, artist, album, year = meta["title"], meta["artist"], meta["album"], meta["year"]
    if ext == ".mp3":
        try:
            t = ID3(path)
        except ID3NoHeaderError:
            t = ID3()
        if write_tags:
            t.setall("TIT2", [TIT2(encoding=1, text=title)])
            if artist:
                t.setall("TPE1", [TPE1(encoding=1, text=artist)])
            if album:
                t.setall("TALB", [TALB(encoding=1, text=album)])
            if year:
                t.setall("TDRC", [TDRC(encoding=1, text=year)])
            if meta.get("albumartist"):
                t.setall("TPE2", [TPE2(encoding=1, text=meta["albumartist"])])
            if meta.get("track"):
                t.setall("TRCK", [TRCK(encoding=1, text=meta["track"])])
        if cover:
            t.delall("APIC")
            t.add(APIC(encoding=1, mime="image/jpeg", type=3, desc="Cover", data=cover))
        if lyrics:
            t.setall("USLT", [USLT(encoding=1, lang="eng", desc="", text=lyrics)])
        t.save(path, v2_version=3)  # ID3v2.3: required by Windows Media Player, MusicBee, many car stereos
    elif ext == ".flac":
        f = FLAC(path)
        if write_tags:
            f["title"] = title
            if artist:
                f["artist"] = artist
            if album:
                f["album"] = album
            if year:
                f["date"] = year
            if meta.get("albumartist"):
                f["albumartist"] = meta["albumartist"]
            if meta.get("track"):
                f["tracknumber"] = meta["track"]
        if lyrics:
            f["lyrics"] = lyrics
        if cover:
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, "image/jpeg", "Cover", cover
            f.clear_pictures()
            f.add_picture(pic)
        f.save()
    elif ext == ".m4a":
        f = MP4(path)
        if write_tags:
            f["\xa9nam"] = [title]
            if artist:
                f["\xa9ART"] = [artist]
            if album:
                f["\xa9alb"] = [album]
            if year:
                f["\xa9day"] = [year]
            if meta.get("albumartist"):
                f["aART"] = [meta["albumartist"]]
            if meta.get("track"):
                f["trkn"] = [(int(meta["track"]), 0)]
        if lyrics:
            f["\xa9lyr"] = [lyrics]
        if cover:
            f["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
        f.save()
    elif ext == ".opus":
        f = OggOpus(path)
        if write_tags:
            f["title"] = [title]
            if artist:
                f["artist"] = [artist]
            if album:
                f["album"] = [album]
            if year:
                f["date"] = [year]
            if meta.get("albumartist"):
                f["albumartist"] = [meta["albumartist"]]
            if meta.get("track"):
                f["tracknumber"] = [meta["track"]]
        if lyrics:
            f["lyrics"] = [lyrics]
        if cover:
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, "image/jpeg", "Cover", cover
            f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        f.save()


# ───────────────────────────── history DB ─────────────────────────────
class History:
    def __init__(self, path: Path):
        self.path, self.lock = path, threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._run("""CREATE TABLE IF NOT EXISTS history(
            id INTEGER PRIMARY KEY AUTOINCREMENT, vid TEXT, title TEXT, artist TEXT,
            path TEXT, fmt TEXT, size INTEGER, ts REAL)""")
        self._run("CREATE INDEX IF NOT EXISTS idx_vid ON history(vid)")

    def _run(self, sql, args=(), fetch=False):
        with self.lock:
            c = sqlite3.connect(self.path, timeout=10)
            try:
                cur = c.execute(sql, args)
                rows = cur.fetchall() if fetch else None
                c.commit()
                return rows
            finally:
                c.close()

    def add(self, vid, title, artist, path, fmt, size):
        self._run("INSERT INTO history(vid,title,artist,path,fmt,size,ts) VALUES(?,?,?,?,?,?,?)",
                  (vid, title, artist, str(path), fmt, size, time.time()))

    def vids(self) -> set:
        return {r[0] for r in self._run("SELECT DISTINCT vid FROM history", fetch=True)}

    def path_for(self, vid) -> Optional[str]:
        r = self._run("SELECT path FROM history WHERE vid=? ORDER BY ts DESC LIMIT 1", (vid,), fetch=True)
        return r[0][0] if r else None

    def search(self, q: str = "", limit: int = 100, order: str = "Newest"):
        like = f"%{q}%"
        return self._run("SELECT id,title,artist,path,fmt,size,ts FROM history "
                         f"WHERE title LIKE ? OR artist LIKE ? ORDER BY {LIB_ORDERS.get(order, 'ts DESC')} LIMIT ?",
                         (like, like, limit), fetch=True)

    def delete(self, rid: int):
        self._run("DELETE FROM history WHERE id=?", (rid,))

    def clear(self):
        self._run("DELETE FROM history")


# ───────────────────────────── data classes ─────────────────────────────
@dataclass
class Track:
    vid: str
    title: str
    artist: str
    duration: str
    url: str
    thumb: str = ""
    views: str = ""
    selected: bool = False
    known: bool = False


@dataclass
class Job:
    id: int
    track: Track
    state: str = "queued"
    progress: float = 0.0
    speed: float = 0.0
    eta: Optional[int] = None
    msg: str = ""
    path: Optional[Path] = None
    cancel: threading.Event = field(default_factory=threading.Event)
    row: Optional["JobRow"] = None


class YLog:
    """yt-dlp logger that keeps the few messages that explain failures (JS challenge, SABR, cookies…)."""
    KEYS = ("jsc", "[pot]", "SABR", "challenge", "JS runtimes", "account cookies", "skipped", "player API",
            "signature", "No supported JavaScript")

    def __init__(self):
        self.lines: List[str] = []

    def _add(self, m: str):
        m = ANSI.sub("", str(m)).strip().replace("\n", " ")[:230]
        if m and m not in self.lines:
            self.lines.append(m)
            del self.lines[:-14]

    def debug(self, m):
        if any(k in m for k in self.KEYS):
            self._add(m)

    def info(self, m): pass

    def warning(self, m):
        log.debug("yt-dlp: %s", m)
        self._add("WARNING " + str(m))

    def error(self, m):
        log.debug("yt-dlp: %s", m)
        self._add("ERROR " + str(m))


def F(size=13, bold=False, mono=False):
    return ctk.CTkFont(family="DejaVu Sans Mono" if mono else None, size=size,
                       weight="bold" if bold else "normal")


# ───────────────────────────── queue row widget ─────────────────────────────
class JobRow(ctk.CTkFrame):
    STYLE = {"queued": ("…", DIM), "downloading": ("↓", ACCENT), "processing": ("◌", AMBER),
             "tagging": ("◌", AMBER), "done": ("✓", GREEN), "skipped": ("✓", DIM),
             "failed": ("!", RED), "cancelled": ("–", DIM)}

    def __init__(self, master, job: Job, app: "App"):
        super().__init__(master, fg_color=CARD, corner_radius=12, border_width=1, border_color=BORDER)
        self.job, self.app = job, app
        self.grid_columnconfigure(1, weight=1)
        self.icon = ctk.CTkLabel(self, text="…", width=44, font=F(20, True))
        self.icon.grid(row=0, column=0, rowspan=2, padx=(10, 0), pady=10)
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=0, column=1, sticky="ew", padx=(4, 10), pady=(10, 4))
        self.title = ctk.CTkLabel(body, text=shorten(f"{job.track.artist} — {job.track.title}"
                                                     if job.track.artist else job.track.title, 90),
                                  anchor="w", font=F(13, True))
        self.title.pack(fill="x")
        self.sub = ctk.CTkLabel(body, text="", anchor="w", font=F(11), text_color=DIM)
        self.sub.pack(fill="x", pady=(2, 0))
        self.bar = ctk.CTkProgressBar(self, height=6, fg_color=HOVER, progress_color=ACCENT)
        self.bar.grid(row=1, column=1, sticky="ew", padx=(4, 10), pady=(0, 12))
        self.bar.set(0)
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=0, column=2, rowspan=2, padx=(0, 10))
        self.main_btn = ctk.CTkButton(btns, text="", width=104, height=30, font=F(12),
                                      fg_color=HOVER, hover_color=BORDER, text_color=TEXT,
                                      command=self._main)
        self.main_btn.pack(side="left", padx=(0, 6))
        self.rm_btn = ctk.CTkButton(btns, text="✕", width=30, height=30, font=F(12),
                                    fg_color="transparent", hover_color=HOVER, text_color=DIM,
                                    command=lambda: app.remove_job(job))
        self.rm_btn.pack(side="left")
        self.up_btn = ctk.CTkButton(btns, text="↑", width=30, height=30, font=F(13, True), fg_color="transparent",
                                    hover_color=HOVER, text_color=DIM, command=lambda: app.prioritize(job))
        self.refresh()

    def _main(self):
        s = self.job.state
        if s in ("queued",) + ACTIVE:
            self.app.cancel_job(self.job)
        elif s in ("failed", "cancelled"):
            self.app.retry_job(self.job)
        elif s in ("done", "skipped") and self.job.path:
            open_path(self.job.path.parent)

    def refresh(self):
        j, s = self.job, self.job.state
        glyph, color = self.STYLE[s]
        if s == "queued":
            sub = "Waiting in queue"
        elif s == "downloading":
            bits = [f"{j.progress * 100:.0f}%"] if j.progress else [j.msg or "Starting…"]
            if j.speed:
                bits.append(f"{fmt_size(j.speed)}/s")
            if j.eta is not None:
                bits.append(f"{fmt_eta(j.eta)} left")
            sub = "  ·  ".join(bits)
        elif s == "processing":
            sub = "Converting audio…"
        elif s == "tagging":
            sub = "Writing tags, cover art & lyrics…"
        elif s == "done":
            size = j.path.stat().st_size if j.path and j.path.exists() else 0
            sub = f"Saved  ·  {fmt_size(size)}  ·  {j.path.name if j.path else ''}"
        elif s == "skipped":
            sub = "Already in your library — skipped"
        elif s == "failed":
            sub = j.msg or "Failed"
        else:
            sub = "Cancelled"
        barval = 1.0 if s in ("processing", "tagging", "done", "skipped") else j.progress if s == "downloading" else 0.0
        active = s in ("queued",) + ACTIVE
        btn = "✕ Cancel" if active else "↻ Retry" if s in ("failed", "cancelled") \
            else "Open folder" if s in ("done", "skipped") else ""
        key = (s, sub, round(barval, 2), btn)
        if key == getattr(self, "_key", None):  # nothing visible changed → no redraw
            return
        prev = getattr(self, "_key", None)
        self._key = key
        if not prev or prev[0] != s:
            self.icon.configure(text=glyph, text_color=color)
            self.bar.configure(progress_color=color)
            self.sub.configure(text_color=RED if s == "failed" else DIM)
            self.main_btn.configure(text=btn)
            self.rm_btn.configure(state="disabled" if s in ACTIVE else "normal")
            if s == "queued" and not self.up_btn.winfo_manager():
                self.up_btn.pack(side="left", padx=(0, 4), before=self.main_btn)
            elif s != "queued" and self.up_btn.winfo_manager():
                self.up_btn.pack_forget()
        if not prev or prev[1] != sub:
            self.sub.configure(text=shorten(sub, 120))
        if not prev or prev[2] != key[2] or prev[0] != s:
            self.bar.set(barval)


# ───────────────────────────── main application ─────────────────────────────
class App(ctk.CTk):
    def __init__(self):
        self.cfg = load_cfg()
        ctk.set_appearance_mode(self.cfg["appearance"].lower())
        ctk.set_default_color_theme("blue")
        sc = SCALES[self.cfg["ui_scale"]]
        ctk.set_widget_scaling(sc)
        ctk.set_window_scaling(sc)
        super().__init__()

        self._uiq: "queue.Queue" = queue.Queue()
        self._closing = False
        self.db = History(DB_FILE)
        self.ff_dir = find_ffmpeg()
        refresh_js()
        COOKIES.load(self.cfg.get("browser", "None"), lambda c: self.ui(self._cookies_done, c, False))
        self.results: List[Track] = []
        self.search_gen = 0
        self.jobs: List[Job] = []
        self.job_seq = 0
        self.paused = False
        self._batch = False
        self.toasts: List[ctk.CTkFrame] = []
        self._thumb_cache: Dict[str, ctk.CTkImage] = {}
        self._thumb_pool = ThreadPoolExecutor(max_workers=3)
        self._card_btns: Dict[str, ctk.CTkButton] = {}
        self._card_vars: Dict[str, ctk.BooleanVar] = {}
        self.preset_menus: List[ctk.CTkOptionMenu] = []
        self.ytdlp_ver = getattr(yt_dlp.version, "__version__", "?")
        self.player = Player(lambda t: self.ui(self._np_update, t))
        self._clip_last, self._auto_dl = "", None
        self._queue_sig, self._restored = None, False
        self._dirty, self._flush_pending, self._rows_stale, self._nav_q = {}, False, False, ""
        self._js_warned, self._js_fail_t = False, 0.0
        self._plan_start, self._diag_budget = 0, 10

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._prune_cache()
        self._setup_logging()

        self.title(f"{APP} {VER}")
        try:
            ic = DATA_DIR / "icon.png"
            if PIL_OK and not ic.exists():
                make_icon(ic)
            if ic.exists():
                self._icon = tk.PhotoImage(file=str(ic))
                self.iconphoto(True, self._icon)
        except Exception:
            pass
        self.geometry(self.cfg["geometry"])
        self.minsize(980, 640)
        self.configure(fg_color=BG)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self.main = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self.main.grid(row=0, column=1, sticky="nsew")
        self.main.grid_columnconfigure(0, weight=1)
        self.main.grid_rowconfigure(0, weight=1)
        self.pages: Dict[str, ctk.CTkFrame] = {}
        self._build_search()
        self._build_queue()
        self._build_library()
        self._build_settings()
        self._build_log()
        self._build_statusbar()
        self._bind_keys()
        self._nav("search")
        self._show_empty()
        self._update_ffmpeg_banner()
        self._update_js_ui()
        if JS_INFO:
            log.info("JavaScript runtime: %s %s", JS_INFO[0], ".".join(map(str, JS_INFO[2])))
        else:
            log.warning("No JavaScript runtime found — YouTube downloads will fail with HTTP 403. "
                        "Use “Fix YouTube downloads” (sidebar) or install Deno.")
        self.protocol("WM_DELETE_WINDOW", self._quit)
        self.after(40, self._poll)
        try:
            self._clip_last = self.clipboard_get().strip()
        except Exception:
            pass
        self.after(300, self._restore_queue)
        self.bind("<FocusIn>", lambda e: self.after(250, self._watch_clipboard) if e.widget is self else None)
        self._present()
        log.info("SonicGrabber %s · yt-dlp %s · FFmpeg %s · Pillow %s · mutagen %s · lyrics %s",
                 VER, self.ytdlp_ver, "ok" if self.ff_dir else "MISSING",
                 "ok" if PIL_OK else "missing", "ok" if MUTAGEN_OK else "missing",
                 "ok" if LYRICS_OK else "missing")

    # ───────── infrastructure ─────────
    def ui(self, fn, *a):
        """Run fn(*a) on the Tk thread (safe from any thread)."""
        self._uiq.put((fn, a))

    def _poll(self):
        if self._closing:
            return
        try:
            while True:
                fn, a = self._uiq.get_nowait()
                try:
                    fn(*a)
                except Exception:
                    log.debug("ui callback failed", exc_info=True)
        except queue.Empty:
            pass
        self.after(40, self._poll)

    def _setup_logging(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not log.handlers:
            fh = RotatingFileHandler(LOG_FILE, maxBytes=500_000, backupCount=2, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            log.addHandler(fh)
        log.addHandler(UIHandler(lambda m, lv: self.ui(self._append_log, m, lv)))

    @staticmethod
    def _prune_cache():
        try:
            files = sorted(CACHE_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
            for p in files[400:]:
                p.unlink(missing_ok=True)
        except Exception:
            pass

    def _set(self, key, value):
        self.cfg[key] = value
        save_cfg(self.cfg)

    def _bind_keys(self):
        for i, k in enumerate(("search", "queue", "library", "settings", "log"), 1):
            self.bind(f"<Control-Key-{i}>", lambda e, k=k: self._nav(k))
        self.bind("<Control-f>", lambda e: (self._nav("search"), self.search_entry.focus_set()))
        self.bind("<Control-a>", self._kb_select_all)

    def _kb_select_all(self, e):
        if self.current == "search" and self.results and self.focus_get() is not self.search_entry._entry:
            self.sel_all_var.set(True)
            self._select_all()

    # ───────── toasts ─────────
    def toast(self, msg: str, kind: str = "info", action=None, ms: int = 4200):
        color = {"info": ACCENT, "ok": GREEN, "warn": AMBER, "err": RED}[kind]
        f = ctk.CTkFrame(self, fg_color=CARD, corner_radius=12, border_width=2, border_color=color)
        ctk.CTkLabel(f, text=msg, font=F(12), wraplength=340, justify="left").pack(
            side="left", padx=(14, 10), pady=11)
        if action:
            ctk.CTkButton(f, text=action[0], width=84, height=28, font=F(12, True), fg_color=color,
                          hover_color=color, text_color="white",
                          command=lambda: (action[1](), self._close_toast(f))
                          ).pack(side="left", padx=(0, 12))
        self.toasts.append(f)
        while len(self.toasts) > 4:
            self._close_toast(self.toasts[0])
        self._layout_toasts()
        self.after(ms, lambda: self._close_toast(f))

    def _close_toast(self, f):
        if f in self.toasts:
            self.toasts.remove(f)
            f.destroy()
            self._layout_toasts()

    def _layout_toasts(self):
        y = -44
        for f in reversed(self.toasts):
            f.place(relx=1.0, rely=1.0, x=-20, y=y, anchor="se")
            f.lift()
            f.update_idletasks()
            y -= f.winfo_reqheight() + 8

    # ───────── sidebar / navigation ─────────
    def _build_sidebar(self):
        sb = ctk.CTkFrame(self, width=220, fg_color=SIDEBAR, corner_radius=0)
        sb.grid(row=0, column=0, rowspan=2, sticky="nsew")
        sb.grid_propagate(False)
        head = ctk.CTkFrame(sb, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(26, 22))
        ctk.CTkLabel(head, text="♪", width=40, height=40, corner_radius=12, fg_color=ACCENT,
                     text_color="white", font=F(22, True)).pack(side="left")
        t = ctk.CTkFrame(head, fg_color="transparent")
        t.pack(side="left", padx=12)
        ctk.CTkLabel(t, text="SonicGrabber", font=F(16, True), anchor="w").pack(anchor="w")
        ctk.CTkLabel(t, text=f"Pro  ·  v{VER}", font=F(10), text_color=DIM, anchor="w").pack(anchor="w")

        self.nav_btns: Dict[str, ctk.CTkButton] = {}
        self._nav_labels = {"search": "Search", "queue": "Queue", "library": "Library",
                            "settings": "Settings", "log": "Activity log"}
        icons = {"search": "⌕", "queue": "⇣", "library": "♫", "settings": "⚙", "log": "☰"}
        for i, k in enumerate(self._nav_labels, 1):
            b = ctk.CTkButton(sb, text=f"   {icons[k]}    {self._nav_labels[k]}", anchor="w", height=42,
                              font=F(13), corner_radius=10, fg_color="transparent", hover_color=HOVER,
                              text_color=TEXT, command=lambda k=k: self._nav(k))
            b.pack(fill="x", padx=12, pady=2)
            self.nav_btns[k] = b
        ctk.CTkFrame(sb, fg_color="transparent").pack(fill="both", expand=True)

        card = ctk.CTkFrame(sb, fg_color=CARD2, corner_radius=10)
        card.pack(fill="x", padx=12, pady=(0, 16))
        self.ff_dot = ctk.CTkLabel(card, text="●", font=F(11))
        self.ff_dot.pack(side="left", padx=(12, 6), pady=10)
        self.ff_lbl = ctk.CTkLabel(card, text="", font=F(11), text_color=DIM)
        self.ff_lbl.pack(side="left", pady=10)
        self.js_btn = ctk.CTkButton(sb, text="⚠  Fix YouTube downloads", height=34, font=F(12, True), fg_color=AMBER,
                                    hover_color="#d97706", text_color="black", command=self._install_js_runtime)
        self._js_before = card

    def _nav(self, key: str):
        self.current = key
        for k, b in self.nav_btns.items():
            b.configure(fg_color=ACCENT if k == key else "transparent",
                        text_color="white" if k == key else TEXT)
        for k, p in self.pages.items():
            p.grid(row=0, column=0, sticky="nsew") if k == key else p.grid_remove()
        if key == "queue" and self._rows_stale:
            self._rows_stale = False
            for j in self.jobs:
                j.row.refresh()
        if key == "library":
            self._refresh_library()
        elif key == "search":
            self.search_entry.focus_set()

    def _title(self, parent, text, sub=""):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        ctk.CTkLabel(f, text=text, font=F(26, True), anchor="w").pack(anchor="w")
        if sub:
            ctk.CTkLabel(f, text=sub, font=F(12), text_color=DIM, anchor="w").pack(anchor="w", pady=(2, 0))
        return f

    # ───────── search page ─────────
    def _build_search(self):
        pg = ctk.CTkFrame(self.main, fg_color="transparent")
        pg.grid_columnconfigure(0, weight=1)
        pg.grid_rowconfigure(4, weight=1)
        self.pages["search"] = pg

        self._title(pg, "Search", "Find a song, or paste a track / playlist link").grid(
            row=0, column=0, sticky="w", padx=36, pady=(26, 0))

        self.banner = ctk.CTkFrame(pg, fg_color=CARD, corner_radius=12, border_width=2, border_color=AMBER)
        self.banner_lbl = ctk.CTkLabel(self.banner, text="", font=F(12), anchor="w", justify="left")
        self.banner_lbl.pack(side="left", padx=14, pady=10)
        ctk.CTkButton(self.banner, text="Re-check", width=80, height=28, font=F(12), fg_color=AMBER,
                      hover_color="#d97706", text_color="black",
                      command=self._recheck_ffmpeg).pack(side="right", padx=(0, 12))
        ctk.CTkButton(self.banner, text="Copy command", width=110, height=28, font=F(12),
                      fg_color=HOVER, hover_color=BORDER, text_color=TEXT,
                      command=self._copy_ff_cmd).pack(side="right", padx=6)

        bar = ctk.CTkFrame(pg, fg_color=CARD, corner_radius=14, height=54, border_width=1, border_color=BORDER)
        bar.grid(row=2, column=0, sticky="ew", padx=36, pady=(16, 0))
        bar.grid_columnconfigure(0, weight=1)
        bar.grid_propagate(False)
        self.search_entry = ctk.CTkEntry(
            bar, placeholder_text="Search songs or artists — or paste a YouTube / SoundCloud link",
            font=F(14), fg_color="transparent", border_width=0, height=50)
        self.search_entry.grid(row=0, column=0, sticky="ew", padx=(16, 4))
        self.search_entry.bind("<Return>", lambda e: self._search())
        self.search_entry.bind("<Escape>", lambda e: (self.search_entry.delete(0, "end"), "break")[1])
        ctk.CTkButton(bar, text="Paste", width=64, height=34, font=F(12), fg_color=HOVER,
                      hover_color=BORDER, text_color=TEXT, command=self._paste_go).grid(row=0, column=1, padx=3)
        self.search_btn = ctk.CTkButton(bar, text="Search", width=100, height=36, font=F(13, True),
                                        fg_color=ACCENT, hover_color=ACCENT_H, text_color="white",
                                        command=self._search)
        self.search_btn.grid(row=0, column=2, padx=(3, 8), pady=7)

        tb = ctk.CTkFrame(pg, fg_color="transparent")
        tb.grid(row=3, column=0, sticky="ew", padx=36, pady=(12, 0))
        self.src_seg = ctk.CTkSegmentedButton(
            tb, values=SOURCES, command=lambda v: self._set("source", v), font=F(12),
            selected_color=ACCENT, selected_hover_color=ACCENT_H, unselected_color=HOVER,
            unselected_hover_color=BORDER, fg_color=HOVER)
        self.src_seg.set(self.cfg["source"])
        self.src_seg.pack(side="left")
        self.songs_var = ctk.BooleanVar(value=self.cfg["songs_only"])
        ctk.CTkSwitch(tb, text="Songs only", variable=self.songs_var, font=F(12), progress_color=ACCENT,
                      command=lambda: self._set("songs_only", self.songs_var.get())
                      ).pack(side="left", padx=(18, 0))
        ctk.CTkLabel(tb, text="Results", font=F(12), text_color=DIM).pack(side="left", padx=(18, 6))
        ctk.CTkOptionMenu(tb, values=["10", "25", "50"], width=70, height=30, font=F(12), fg_color=HOVER,
                          button_color=HOVER, button_hover_color=BORDER, text_color=TEXT,
                          command=lambda v: self._set("result_count", v)
                          ).pack(side="left")
        self._count_menu = tb.winfo_children()[-1]
        self._count_menu.set(self.cfg["result_count"])
        ctk.CTkLabel(tb, text="Format", font=F(12), text_color=DIM).pack(side="left", padx=(18, 6))
        self._preset_menu(tb).pack(side="left")
        self.save_btn = ctk.CTkButton(tb, text="", height=30, font=F(11), fg_color="transparent",
                                      hover_color=HOVER, text_color=DIM,
                                      command=lambda: open_path(self._out_dir()))
        self.save_btn.pack(side="right")
        self._update_save_label()

        self.res = ctk.CTkFrame(pg, fg_color="transparent")
        self.res.grid(row=4, column=0, sticky="nsew", padx=36, pady=(12, 22))
        self.res.grid_columnconfigure(0, weight=1)
        self.res.grid_rowconfigure(1, weight=1)

        self.res_hdr = ctk.CTkFrame(self.res, fg_color="transparent")
        self.res_count = ctk.CTkLabel(self.res_hdr, text="", font=F(13, True))
        self.res_count.pack(side="left")
        self.sel_all_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(self.res_hdr, text="Select all", variable=self.sel_all_var, font=F(12),
                        fg_color=ACCENT, hover_color=ACCENT_H, command=self._select_all
                        ).pack(side="left", padx=18)
        self.dl_all_btn = ctk.CTkButton(self.res_hdr, text="Download all", height=34, font=F(12, True),
                                        fg_color=GREEN, hover_color=GREEN_H, text_color="white",
                                        command=lambda: self._download(self.results))
        self.dl_all_btn.pack(side="right")
        self.dl_sel_btn = ctk.CTkButton(self.res_hdr, text="Download selected", height=34, font=F(12, True),
                                        fg_color=ACCENT, hover_color=ACCENT_H, text_color="white",
                                        state="disabled",
                                        command=lambda: self._download([t for t in self.results if t.selected]))
        self.dl_sel_btn.pack(side="right", padx=8)

        self.scroll = ctk.CTkScrollableFrame(self.res, fg_color="transparent",
                                             scrollbar_button_color=HOVER, scrollbar_button_hover_color=BORDER)
        self.scroll.grid(row=1, column=0, sticky="nsew")
        self.scroll.grid_columnconfigure(0, weight=1)

    def _preset_menu(self, parent):
        m = ctk.CTkOptionMenu(parent, values=list(PRESETS), width=160, height=30, font=F(12), fg_color=HOVER,
                              button_color=HOVER, button_hover_color=BORDER, text_color=TEXT,
                              command=self._set_preset)
        m.set(self.cfg["preset"])
        self.preset_menus.append(m)
        return m

    def _set_preset(self, v):
        for m in self.preset_menus:
            m.set(v)
        self._set("preset", v)
        self.toast(f"Format set to {v}", "info", ms=1800)

    def _out_dir(self) -> Path:
        p = Path(self.cfg["output_dir"]).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _update_save_label(self):
        self.save_btn.configure(text=f"Saving to  {shorten_left(self.cfg['output_dir'], 34)}  ↗")

    def _clear_scroll(self):
        for w in list(self.scroll.winfo_children()):
            w.destroy()
        self._card_btns.clear()
        self._card_vars.clear()

    def _show_empty(self):
        self._clear_scroll()
        self.res_hdr.grid_remove()
        box = ctk.CTkFrame(self.scroll, fg_color="transparent")
        box.pack(expand=True, pady=(60, 0))
        ctk.CTkLabel(box, text="♪", font=F(54, True), text_color=ACCENT).pack()
        ctk.CTkLabel(box, text="Find your next favourite track", font=F(20, True)).pack(pady=(6, 2))
        ctk.CTkLabel(box, text="Type a song or artist, or paste a link to a track or whole playlist.",
                     font=F(13), text_color=DIM).pack()
        recent = self.cfg.get("recent") or []
        if recent:
            ctk.CTkLabel(box, text="Recent searches", font=F(11), text_color=DIM).pack(pady=(26, 8))
            row = ctk.CTkFrame(box, fg_color="transparent")
            row.pack()
            for q in recent[:6]:
                ctk.CTkButton(row, text=shorten(q, 22), height=30, font=F(12), corner_radius=15,
                              fg_color=HOVER, hover_color=BORDER, text_color=TEXT,
                              command=lambda q=q: self._run_query(q)).pack(side="left", padx=4)

    def _show_loading(self, text):
        self._clear_scroll()
        self.res_hdr.grid_remove()
        box = ctk.CTkFrame(self.scroll, fg_color="transparent")
        box.pack(expand=True, pady=(90, 0))
        ctk.CTkLabel(box, text=text, font=F(14), text_color=DIM).pack(pady=(0, 12))
        pb = ctk.CTkProgressBar(box, width=240, mode="indeterminate", progress_color=ACCENT)
        pb.pack()
        pb.start()

    def _show_message(self, title, sub="", color=DIM):
        self._clear_scroll()
        self.res_hdr.grid_remove()
        box = ctk.CTkFrame(self.scroll, fg_color="transparent")
        box.pack(expand=True, pady=(90, 0))
        ctk.CTkLabel(box, text=title, font=F(16, True), text_color=color).pack()
        if sub:
            ctk.CTkLabel(box, text=sub, font=F(12), text_color=DIM, wraplength=520).pack(pady=(6, 0))

    def _update_ffmpeg_banner(self):
        ok = bool(self.ff_dir)
        self.ff_dot.configure(text_color=GREEN if ok else RED)
        self.ff_lbl.configure(text=f"FFmpeg ready · yt-dlp {self.ytdlp_ver}" if ok else "FFmpeg missing")
        if ok:
            self.banner.grid_remove()
        else:
            self.banner_lbl.configure(text=f"FFmpeg isn't installed — needed to convert audio.\n"
                                           f"Install it:  {ffmpeg_install_cmd()}")
            self.banner.grid(row=1, column=0, sticky="ew", padx=36, pady=(14, 0))

    def _recheck_ffmpeg(self):
        self.ff_dir = find_ffmpeg()
        self._update_ffmpeg_banner()
        self.toast("FFmpeg found — you're all set" if self.ff_dir else "FFmpeg still not found",
                   "ok" if self.ff_dir else "warn")

    def _copy_ff_cmd(self):
        self.clipboard_clear()
        self.clipboard_append(ffmpeg_install_cmd().split("#")[0].strip())
        self.toast("Install command copied", "ok", ms=1800)

    # ───────── search logic ─────────
    def _paste_go(self):
        try:
            txt = self.clipboard_get().strip()
        except Exception:
            self.toast("Clipboard is empty", "warn", ms=1800)
            return
        self.search_entry.delete(0, "end")
        self.search_entry.insert(0, txt)
        self._search()

    def _run_query(self, q):
        self.search_entry.delete(0, "end")
        self.search_entry.insert(0, q)
        self._search()

    def _search(self):
        q = self.search_entry.get().strip()
        if not q:
            self.toast("Type something to search for", "warn", ms=1800)
            return
        self.search_gen += 1
        gen = self.search_gen
        self.results = []
        self.search_btn.configure(state="disabled", text="Searching…")
        if is_url(q):
            self._show_loading("Reading link…")
            threading.Thread(target=self._resolve_worker, args=(q, gen), daemon=True).start()
        else:
            rec = [q] + [r for r in self.cfg.get("recent", []) if r != q]
            self._set("recent", rec[:8])
            self._show_loading(f"Searching {self.cfg['source']} for “{shorten(q, 40)}”…")
            threading.Thread(target=self._search_worker, args=(q, gen, self.cfg["source"],
                             int(self.cfg["result_count"]), self.songs_var.get()), daemon=True).start()

    def _tracks_from(self, entries, songs_only=False, query="") -> List[Track]:
        if query:  # best-match-first: studio / Topic uploads before live, covers and remixes
            entries = [e for _, e in sorted(enumerate(entries),
                                            key=lambda p: -(rank_entry(p[1] or {}, query) - p[0] * 0.15))]
        known, seen, out = self.db.vids(), set(), []
        for e in entries:
            if not e:
                continue
            url = entry_url(e)
            if not url:
                continue
            title_raw = (e.get("title") or "").lower()
            dur = e.get("duration")
            if songs_only and (any(k in title_raw for k in HARD_SKIP) or
                               (dur and (dur < 45 or dur > 900))):
                continue
            vid = str(e.get("id") or hashlib.md5(url.encode()).hexdigest()[:12])
            if vid in seen:
                continue
            seen.add(vid)
            m = derive_meta(e)
            title = m["title"]
            if title in ("Unknown", ""):
                title = url.rstrip("/").split("/")[-1].replace("-", " ").title() or "Unknown"
            if len(vid) == 11 and "youtu" in url:
                thumb = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
            else:
                th = e.get("thumbnail") or next((t.get("url") for t in reversed(e.get("thumbnails") or [])
                                                 if t.get("url")), "")
                thumb = th or ""
            views = e.get("view_count")
            out.append(Track(vid=vid, title=title, artist=m["artist"], duration=fmt_dur(dur), url=url,
                             thumb=thumb, views=f"{views:,} views" if views else "", known=vid in known))
        return out

    def _search_worker(self, q, gen, source, count, songs_only):
        try:
            prefix = "scsearch" if source == "SoundCloud" else "ytsearch"
            n = count + (count // 2 if songs_only else 0)
            opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True,
                    "socket_timeout": 15, "logger": YLog()}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"{prefix}{n}:{q}", download=False)
            tracks = self._tracks_from((info or {}).get("entries") or [], songs_only,
                                       q if source == "YouTube" else "")[:count]
            log.info("Search “%s” on %s → %d results", q, source, len(tracks))
            self.ui(self._show_results, tracks, gen, f"{len(tracks)} results")
        except Exception as ex:
            log.error("Search failed: %s", clean_err(ex))
            self.ui(self._show_error, gen, clean_err(ex))

    def _resolve_worker(self, url, gen):
        try:
            opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
                    "skip_download": True, "socket_timeout": 15, "logger": YLog()}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if not info:
                raise RuntimeError("Nothing found at that link")
            if info.get("entries") is not None:
                tracks = self._tracks_from(list(info["entries"]))
                head = f"Playlist · {shorten(info.get('title') or 'Untitled', 50)} · {len(tracks)} tracks"
            else:
                tracks = self._tracks_from([info])
                head = "1 track"
            log.info("Resolved link → %s", head)
            self.ui(self._show_results, tracks, gen, head)
        except Exception as ex:
            log.error("Link failed: %s", clean_err(ex))
            self.ui(self._show_error, gen, clean_err(ex))

    def _show_error(self, gen, msg):
        if gen != self.search_gen:
            return
        self.search_btn.configure(state="normal", text="Search")
        self._show_message("Something went wrong", msg, RED)

    def _show_results(self, tracks, gen, heading):
        if gen != self.search_gen:
            return
        self.search_btn.configure(state="normal", text="Search")
        self.results = tracks
        self.sel_all_var.set(False)
        if not tracks:
            self._show_message("No results", "Try different keywords, or turn off “Songs only”.")
            return
        self._clear_scroll()
        self.res_count.configure(text=heading)
        self.res_hdr.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.dl_all_btn.configure(text=f"Download all ({len(tracks)})")
        self._update_sel()
        self._build_cards(tracks, gen, 0)
        if self._auto_dl:  # command-line mode: main.py -d "query or link"
            pick = tracks if self._auto_dl == "all" else tracks[:1]
            self._auto_dl = None
            self._download(pick)

    PAGE = 12

    def _build_cards(self, tracks, gen, start, stop=None):
        if gen != self.search_gen:
            return
        stop = min(len(tracks), stop if stop is not None else start + self.PAGE)
        end = min(start + 4, stop)
        for t in tracks[start:end]:
            self._result_card(t, gen)
        if end < stop:
            self.after(6, lambda: self._build_cards(tracks, gen, end, stop))
        elif stop < len(tracks):
            left = len(tracks) - stop
            b = ctk.CTkButton(self.scroll, text=f"Show {min(self.PAGE, left)} more   ({left} remaining)", height=36,
                              font=F(12), fg_color=HOVER, hover_color=BORDER, text_color=TEXT,
                              command=lambda: (b.destroy(), self._build_cards(tracks, gen, stop)))
            b.pack(pady=10)

    def _result_card(self, t: Track, gen):
        card = ctk.CTkFrame(self.scroll, fg_color=CARD, corner_radius=10, border_width=1,
                            border_color=ACCENT if t.selected else BORDER)
        card.pack(fill="x", pady=3)
        card.grid_columnconfigure(2, weight=1)
        mark = ctk.CTkLabel(card, text="☑" if t.selected else "☐", width=34, font=F(18),
                            text_color=ACCENT if t.selected else DIM)
        mark.grid(row=0, column=0, rowspan=2, padx=(10, 2))
        thumb = ctk.CTkLabel(card, text="♪", width=120, height=68, corner_radius=8, fg_color=HOVER,
                             text_color=DIM, font=F(22))
        thumb.grid(row=0, column=1, rowspan=2, padx=(4, 12), pady=10)
        title = ctk.CTkLabel(card, text=shorten(t.title, 70), anchor="w", font=F(14, True))
        title.grid(row=0, column=2, sticky="sw", pady=(10, 0))
        meta = "  ·  ".join(x for x in (t.artist, t.duration, t.views) if x)
        sub = ctk.CTkLabel(card, text=meta + ("   ✓ In your library" if t.known else ""), anchor="w",
                           font=F(12), text_color=GREEN if t.known else DIM)
        sub.grid(row=1, column=2, sticky="nw", pady=(2, 10))
        ctk.CTkButton(card, text="▶", width=34, height=34, font=F(12), fg_color="transparent",
                      hover_color=HOVER, text_color=DIM,
                      command=lambda tr=t: self._preview(tr)).grid(row=0, column=3, rowspan=2, padx=2)
        db = ctk.CTkButton(card, text="↓  Download", width=112, height=34, font=F(12, True),
                           fg_color=ACCENT, hover_color=ACCENT_H, text_color="white",
                           command=lambda tr=t: self._download([tr]))
        db.grid(row=0, column=4, rowspan=2, padx=(2, 12))
        self._card_btns[t.vid] = db

        def setsel(v: bool):
            t.selected = v
            mark.configure(text="☑" if v else "☐", text_color=ACCENT if v else DIM)
            card.configure(border_color=ACCENT if v else BORDER)

        self._card_vars[t.vid] = setsel

        def toggle(_e=None):
            setsel(not t.selected)
            self._update_sel()

        for w in (card, mark, thumb, title, sub):
            w.bind("<Button-1>", toggle, add="+")
        for w in (title, thumb):  # double-click a title/thumbnail → open in browser
            w.bind("<Double-Button-1>", lambda e, u=t.url: open_path(u), add="+")
        if t.thumb and PIL_OK:
            if t.thumb in self._thumb_cache:
                thumb.configure(image=self._thumb_cache[t.thumb], text="")
            else:
                self._thumb_pool.submit(self._load_thumb, t.thumb, thumb, gen)

    def _load_thumb(self, url, label, gen):
        try:
            cp = CACHE_DIR / f"{hashlib.md5(url.encode()).hexdigest()}.jpg"
            data = cp.read_bytes() if cp.exists() else http_get(url, 8)
            if not cp.exists():
                cp.write_bytes(data)
            img = ImageOps.fit(Image.open(io.BytesIO(data)).convert("RGB"), (240, 136))
        except Exception:
            return
        self.ui(self._apply_thumb, url, img, label, gen)

    def _apply_thumb(self, url, img, label, gen):
        if gen != self.search_gen:
            return
        ci = self._thumb_cache.get(url)
        if ci is None:
            if len(self._thumb_cache) > 300:
                self._thumb_cache.clear()
            ci = self._thumb_cache[url] = ctk.CTkImage(light_image=img, dark_image=img, size=(120, 68))
        try:
            if label.winfo_exists():
                label.configure(image=ci, text="")
        except Exception:
            pass

    def _select_all(self):
        v = self.sel_all_var.get()
        for t in self.results:
            t.selected = v
            setter = self._card_vars.get(t.vid)
            if setter:
                setter(v)
        self._update_sel()

    def _update_sel(self):
        n = sum(1 for t in self.results if t.selected)
        self.dl_sel_btn.configure(state="normal" if n else "disabled",
                                  text=f"Download selected ({n})" if n else "Download selected")
        if n != len(self.results):
            self.sel_all_var.set(False)

    # ───────── queue page ─────────
    def _build_queue(self):
        pg = ctk.CTkFrame(self.main, fg_color="transparent")
        pg.grid_columnconfigure(0, weight=1)
        pg.grid_rowconfigure(2, weight=1)
        self.pages["queue"] = pg
        self._title(pg, "Queue", "Downloads run in the background — keep searching while they finish").grid(
            row=0, column=0, sticky="w", padx=36, pady=(26, 0))
        bar = ctk.CTkFrame(pg, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", padx=36, pady=(14, 8))
        self.q_summary = ctk.CTkLabel(bar, text="", font=F(13, True))
        self.q_summary.pack(side="left")
        for txt, cmd, kw in (("Cancel all", self.cancel_all, dict(fg_color=HOVER, text_color=RED)),
                             ("Clear finished", self.clear_finished, dict(fg_color=HOVER, text_color=TEXT)),
                             ("Retry failed", self.retry_failed, dict(fg_color=HOVER, text_color=TEXT))):
            ctk.CTkButton(bar, text=txt, height=32, font=F(12), hover_color=BORDER, command=cmd, **kw
                          ).pack(side="right", padx=(6, 0))
        self.pause_btn = ctk.CTkButton(bar, text="Pause queue", height=32, font=F(12, True), fg_color=AMBER,
                                       hover_color="#d97706", text_color="black", command=self.toggle_pause)
        self.pause_btn.pack(side="right", padx=(6, 0))
        self.q_scroll = ctk.CTkScrollableFrame(pg, fg_color="transparent", scrollbar_button_color=HOVER,
                                               scrollbar_button_hover_color=BORDER)
        self.q_scroll.grid(row=2, column=0, sticky="nsew", padx=36, pady=(0, 22))
        self.q_scroll.grid_columnconfigure(0, weight=1)
        self.q_empty = ctk.CTkFrame(self.q_scroll, fg_color="transparent")
        ctk.CTkLabel(self.q_empty, text="⇣", font=F(48, True), text_color=ACCENT).pack(pady=(70, 0))
        ctk.CTkLabel(self.q_empty, text="Nothing in the queue", font=F(18, True)).pack(pady=(4, 2))
        ctk.CTkLabel(self.q_empty, text="Songs you download will show up here with live progress.",
                     font=F(12), text_color=DIM).pack()
        ctk.CTkButton(self.q_empty, text="Go to Search", height=34, font=F(12, True), fg_color=ACCENT,
                      hover_color=ACCENT_H, text_color="white",
                      command=lambda: self._nav("search")).pack(pady=16)
        self.q_empty.pack(fill="x")

    def _download(self, tracks: List[Track]):
        if not tracks:
            return
        if not self.ff_dir:
            self._nav("search")
            self.toast("FFmpeg is required to convert audio — see the banner above", "warn")
            return
        if not JS_OPTS and not self._js_warned and any("youtu" in t.url for t in tracks):
            self._js_warned = True
            self.toast("No JavaScript runtime found — YouTube downloads will probably fail (HTTP 403).", "warn",
                       action=("Fix it", self._install_js_runtime), ms=10000)
        queued = {j.track.vid for j in self.jobs if j.state == "queued" or j.state in ACTIVE}
        fresh = [t for t in tracks if t.vid not in queued]
        for t in fresh:
            self.job_seq += 1
            job = Job(self.job_seq, t)
            job.row = JobRow(self.q_scroll, job, self)
            job.row.pack(fill="x", pady=4)
            self.jobs.append(job)
            btn = self._card_btns.get(t.vid)
            if btn:
                btn.configure(text="✓  Queued", fg_color=GREEN, hover_color=GREEN_H, state="disabled")
        if not fresh:
            self.toast("Already in the queue", "warn", ms=2200)
            return
        skipped = len(tracks) - len(fresh)
        msg = f"Added {len(fresh)} track{'s' if len(fresh) != 1 else ''} to the queue"
        if skipped:
            msg += f" ({skipped} already queued)"
        self.toast(msg, "ok", action=("View queue", lambda: self._nav("queue")))
        self._batch = True
        self._pump()
        self._refresh_queue_ui()

    def _pump(self):
        if self.paused:
            return
        slots = int(self.cfg["concurrency"]) - sum(1 for j in self.jobs if j.state in ACTIVE)
        for j in self.jobs:
            if slots <= 0:
                break
            if j.state == "queued":
                j.state, j.progress, j.speed, j.eta = "downloading", 0.0, 0.0, None
                j.row.refresh()
                threading.Thread(target=self._run_job, args=(j,), daemon=True).start()
                slots -= 1

    def _job_changed(self, job: Job):
        """Coalesce progress updates: redraw at most ~6x per second, and only rows that are on screen."""
        self._dirty[job.id] = job
        if not self._flush_pending:
            self._flush_pending = True
            self.after(160, self._flush)

    def _flush(self):
        self._flush_pending = False
        dirty, self._dirty = self._dirty, {}
        if self.current == "queue":
            for j in dirty.values():
                if j.row and j.row.winfo_exists():
                    j.row.refresh()
        else:
            self._rows_stale = True
        self._refresh_queue_ui()

    def _on_job_end(self, job: Job):
        self._job_changed(job)
        if job.state == "failed":
            if not JS_OPTS and "403" in job.msg:
                if time.time() - self._js_fail_t > 20:
                    self._js_fail_t = time.time()
                    self.toast("YouTube is blocking downloads: it needs a JavaScript runtime. "
                               "SonicGrabber can set one up for you.", "err",
                               action=("Fix it", self._install_js_runtime), ms=14000)
            else:
                self.toast(f"Failed: {shorten(job.track.title, 34)} — {shorten(job.msg, 70)}", "err",
                           action=("View queue", lambda: self._nav("queue")), ms=6000)
        self._pump()
        self._refresh_queue_ui()
        busy = any(j.state == "queued" or j.state in ACTIVE for j in self.jobs)
        if self._batch and not busy and not self.paused:
            self._batch = False
            ok = sum(1 for j in self.jobs if j.state == "done")
            bad = sum(1 for j in self.jobs if j.state == "failed")
            self.toast(f"All done — {ok} saved" + (f", {bad} failed" if bad else ""), "ok" if not bad else "warn",
                       action=("Open folder", lambda: open_path(self._out_dir())), ms=7000)
            if self.cfg["notify"] and ok:
                desktop_notify("Downloads complete", f"{ok} track{'s' if ok != 1 else ''} saved to "
                                                     f"{self.cfg['output_dir']}")

    def _refresh_queue_ui(self):
        self._save_queue()
        n = {s: sum(1 for j in self.jobs if j.state == s) for s in
             ("queued", "downloading", "processing", "tagging", "done", "failed", "skipped", "cancelled")}
        active = n["downloading"] + n["processing"] + n["tagging"]
        waiting = n["queued"]
        bits = []
        if active:
            bits.append(f"{active} active")
        if waiting:
            bits.append(f"{waiting} waiting")
        if n["done"]:
            bits.append(f"{n['done']} done")
        if n["failed"]:
            bits.append(f"{n['failed']} failed")
        self.q_summary.configure(text="  ·  ".join(bits) if bits else "Empty")
        if self.jobs:
            self.q_empty.pack_forget()
        elif not self.q_empty.winfo_ismapped():
            self.q_empty.pack(fill="x")
        pending = active + waiting
        nq = f"   ⇣    Queue" + (f"   ({pending})" if pending else "")
        if nq != self._nav_q:
            self._nav_q = nq
            self.nav_btns["queue"].configure(text=nq)
        # status bar
        speed = sum(j.speed for j in self.jobs if j.state == "downloading")
        if active or waiting:
            if self.paused and not active:
                txt, col = f"Queue paused · {waiting} waiting", AMBER
            else:
                txt, col = f"Downloading {active} · {waiting} waiting", ACCENT
            tot = [j for j in self.jobs if j.state not in ("cancelled",)]
            prog = sum(1 if j.state in ("done", "skipped", "failed", "processing", "tagging") else j.progress
                       for j in tot) / max(1, len(tot))
            self.sb_bar.set(prog)
            if not self.sb_bar.winfo_manager():
                self.sb_bar.pack(side="right", padx=(0, 14))
            self.sb_speed.configure(text=f"{fmt_size(speed)}/s" if speed else "")
        else:
            txt, col = "Ready", DIM
            if self.sb_bar.winfo_manager():
                self.sb_bar.pack_forget()
            self.sb_speed.configure(text="")
        self.sb_txt.configure(text=txt)
        self.sb_dot.configure(text_color=col)

    def toggle_pause(self):
        self.paused = not self.paused
        self.pause_btn.configure(text="Resume queue" if self.paused else "Pause queue",
                                 fg_color=GREEN if self.paused else AMBER,
                                 hover_color=GREEN_H if self.paused else "#d97706",
                                 text_color="white" if self.paused else "black")
        self.toast("Queue paused — current downloads will finish" if self.paused else "Queue resumed",
                   "warn" if self.paused else "ok", ms=2400)
        self._pump()
        self._refresh_queue_ui()

    def cancel_job(self, job: Job):
        if job.state == "queued":
            job.state = "cancelled"
            job.row.refresh()
        else:
            job.cancel.set()
        self._refresh_queue_ui()

    def cancel_all(self):
        n = 0
        for j in self.jobs:
            if j.state == "queued" or j.state in ACTIVE:
                self.cancel_job(j)
                n += 1
        if n:
            self.toast(f"Cancelled {n} download{'s' if n != 1 else ''}", "info", ms=2200)

    def retry_job(self, job: Job):
        job.cancel = threading.Event()
        job.state, job.progress, job.msg = "queued", 0.0, ""
        job.row.refresh()
        self._batch = True
        self._pump()
        self._refresh_queue_ui()

    def retry_failed(self):
        bad = [j for j in self.jobs if j.state in ("failed", "cancelled")]
        for j in bad:
            self.retry_job(j)
        self.toast(f"Retrying {len(bad)} item{'s' if len(bad) != 1 else ''}" if bad else "Nothing to retry",
                   "info", ms=2000)

    def remove_job(self, job: Job):
        if job.state in ACTIVE:
            return
        if job.row:
            job.row.destroy()
        if job in self.jobs:
            self.jobs.remove(job)
        self._refresh_queue_ui()

    def clear_finished(self):
        for j in [j for j in self.jobs if j.state in FINISHED]:
            self.remove_job(j)

    # ───────── download worker ─────────
    def _run_job(self, job: Job):
        cfg = dict(self.cfg)
        try:
            attempts = int(cfg.get("retries", "3"))
            for n in range(1, attempts + 1):
                try:
                    job.path, skipped = self._download_one(job, cfg, n)
                    job.state = "skipped" if skipped else "done"
                    break
                except Cancelled:
                    raise
                except Exception as e:
                    self._dump_diag(job)
                    if job.cancel.is_set() or n == attempts or not transient(e) \
                            or ("403" in str(e) and not JS_OPTS):  # 403 without a JS runtime never clears by retrying
                        raise
                    log.warning("Retry %d/%d for %s — %s (trying another approach)", n, attempts,
                                job.track.title, clean_err(e))
                    job.state, job.progress, job.eta = "downloading", 0.0, None
                    job.msg = f"Retrying ({n + 1}/{attempts})…"
                    self.ui(self._job_changed, job)
                    if job.cancel.wait(3 * n):
                        raise Cancelled
        except Cancelled:
            job.state = "cancelled"
        except Exception as e:
            if job.cancel.is_set():
                job.state = "cancelled"
            else:
                job.state, job.msg = "failed", clean_err(e)
                log.error("✗ %s — %s", job.track.title, job.msg)
        finally:
            self._cleanup_partials(job)
            self.ui(self._on_job_end, job)

    @staticmethod
    def _cleanup_partials(job: Job):
        if job.state in ("failed", "cancelled") and getattr(job, "_stem", None):
            stem = Path(job._stem)  # type: ignore[attr-defined]
            for f in stem.parent.glob(glob.escape(stem.name) + "*"):
                if f.suffix in (".part", ".ytdl", ".temp"):
                    f.unlink(missing_ok=True)

    def _download_one(self, job: Job, cfg: dict, attempt: int = 1):
        t, fi = job.track, PRESETS[cfg["preset"]]
        out_dir = Path(cfg["output_dir"]).expanduser()
        last = [0.0]
        plan_idx, clients, use_cookies = self._plan(attempt)
        ylog = job.ylog = YLog()
        if COOKIES.state == "loading":
            job.msg = "Reading browser cookies…"
            self.ui(self._job_changed, job)
            for _ in range(90):
                if COOKIES.event.wait(0.5) or job.cancel.is_set():
                    break
        if job.cancel.is_set():
            raise Cancelled
        job.msg = "Fetching video info…"
        self.ui(self._job_changed, job)

        with new_ydl({"quiet": True, "no_warnings": True, "noplaylist": True, "format": "bestaudio/best", "verbose": True,
                               "socket_timeout": 30, "logger": ylog, **ydl_common(cfg), **yt_args(clients)}, use_cookies) as ydl:
            info = run_timeout(lambda: ydl.extract_info(t.url, download=False), 120, "fetching video info")
        if info.get("entries"):
            info = next(e for e in info["entries"] if e)
        if job.cancel.is_set():
            raise Cancelled
        meta = derive_meta(info)
        vid = str(info.get("id") or t.vid)
        mb = None
        if cfg.get("musicbrainz") and cfg["embed_tags"]:
            job.msg = "Looking up album info…"
            self.ui(self._job_changed, job)
            try:
                mb = mb_lookup(meta["artist"], meta["title"])
            except Exception as e:
                log.debug("MusicBrainz lookup failed: %s", e)
            if mb:
                meta.update({k: mb[k] for k in ("album", "albumartist", "track") if mb.get(k)})
                meta["year"] = mb.get("year") or meta["year"]
                log.info("  MusicBrainz: %s (%s)", meta["album"], meta["year"] or "?")

        if cfg["organize"] == "Artist folders":
            folder = out_dir / safe_name(meta["artist"] or "Unknown Artist")
        elif cfg["organize"] == "Artist / Album":
            folder = out_dir / safe_name(meta["artist"] or "Unknown Artist") / safe_name(meta["album"] or "Singles")
        else:
            folder = out_dir
        base = f"{meta['artist']} - {meta['title']}" if cfg["name_style"] == "Artist - Title" and meta["artist"] \
            else meta["title"]
        stem = folder / safe_name(base)
        job._stem = str(stem)  # type: ignore[attr-defined]
        final = Path(f"{stem}.{fi['ext']}")

        if cfg["skip_existing"]:
            if final.exists():
                return final, True
            old = self.db.path_for(vid)
            if old and Path(old).exists():
                return Path(old), True
        folder.mkdir(parents=True, exist_ok=True)

        def hook(d):
            if job.cancel.is_set():
                raise Cancelled
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                job.progress = min(done / total, 1.0) if total else 0.0
                job.speed, job.eta = d.get("speed") or 0.0, d.get("eta")
                now = time.monotonic()
                if now - last[0] > 0.2:
                    last[0] = now
                    self.ui(self._job_changed, job)
            elif d.get("status") == "finished":
                job.progress, job.state = 1.0, "processing"
                self.ui(self._job_changed, job)

        opts = {
            "format": "bestaudio/best", "noplaylist": True, "quiet": True, "no_warnings": True,
            "noprogress": True, "socket_timeout": 30, "retries": 5, "fragment_retries": 5,
            "concurrent_fragment_downloads": 4, "progress_hooks": [hook], "logger": ylog,
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": fi["codec"],
                                "preferredquality": fi["q"]}],
            "outtmpl": str(stem).replace("%", "%%") + ".%(ext)s",
            "ffmpeg_location": self.ff_dir, **ydl_common(cfg), **yt_args(clients),
        }
        if cfg["normalize"]:
            opts["postprocessor_args"] = {"extractaudio": ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]}
        if cfg.get("sponsorblock"):  # cut intros/skits/non-music parts of music videos
            opts["postprocessors"] = [
                {"key": "SponsorBlock", "categories": ["music_offtopic"], "when": "after_filter"},
                {"key": "ModifyChapters", "remove_sponsor_segments": ["music_offtopic"]},
            ] + opts["postprocessors"]

        log.info("↓ %s", f"{meta['artist']} — {meta['title']}" if meta["artist"] else meta["title"])
        with new_ydl(opts, use_cookies) as ydl:
            ydl.process_ie_result(info, download=True)

        if not final.exists():
            alts = [p for p in folder.glob(glob.escape(stem.name) + ".*")
                    if p.suffix not in (".part", ".ytdl", ".temp")]
            if not alts:
                raise RuntimeError("Output file not found after conversion")
            final = alts[0]

        job.state = "tagging"
        self.ui(self._job_changed, job)
        cover = lyrics = raw_lyrics = None
        if cfg["embed_cover"]:
            for u in mb_cover(mb) + cover_candidates(info):
                try:
                    cover = make_cover(http_get(u), cfg["square_cover"])
                except Exception:
                    cover = None
                if cover:
                    break
            if not cover:
                log.warning("No cover art found for %s", meta["title"])
        if cfg["embed_lyrics"] and LYRICS_OK:
            try:
                raw_lyrics = syncedlyrics.search(f"{meta['artist']} {meta['title']}".strip())
                lyrics = plain_lyrics(raw_lyrics) if raw_lyrics else None
            except Exception:
                lyrics = None
        try:
            tag_file(final, meta, cover, lyrics, cfg["embed_tags"])
            if raw_lyrics and cfg["save_lrc"]:
                final.with_suffix(".lrc").write_text(raw_lyrics, "utf-8")
        except Exception as e:
            log.warning("Tagging failed for %s: %s", final.name, e)

        size = final.stat().st_size
        self.db.add(vid, meta["title"], meta["artist"], final, cfg["preset"], size)
        log.info("✓ Saved %s (%s%s%s)", final.name, fmt_size(size), ", cover" if cover else "",
                 ", lyrics" if lyrics else "")
        self._plan_start = plan_idx  # remember what worked so the next songs start there
        return final, False

    # ───────── library page ─────────
    def _build_library(self):
        pg = ctk.CTkFrame(self.main, fg_color="transparent")
        pg.grid_columnconfigure(0, weight=1)
        pg.grid_rowconfigure(2, weight=1)
        self.pages["library"] = pg
        self._title(pg, "Library", "Everything you've downloaded with SonicGrabber").grid(
            row=0, column=0, sticky="w", padx=36, pady=(26, 0))
        bar = ctk.CTkFrame(pg, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", padx=36, pady=(14, 8))
        self.lib_entry = ctk.CTkEntry(bar, placeholder_text="Filter by title or artist…", height=36,
                                      font=F(13), width=320)
        self.lib_entry.pack(side="left")
        self._lib_after = None
        self.lib_entry.bind("<KeyRelease>", self._lib_debounce)
        self.lib_sort = ctk.CTkOptionMenu(bar, values=list(LIB_ORDERS), width=120, height=34, font=F(12),
                                          fg_color=HOVER, button_color=HOVER, button_hover_color=BORDER,
                                          text_color=TEXT, command=lambda v: self._refresh_library())
        self.lib_sort.set("Newest")
        self.lib_sort.pack(side="left", padx=8)
        ctk.CTkButton(bar, text="Clear history", height=34, font=F(12), fg_color=HOVER, hover_color=BORDER,
                      text_color=RED, command=self._clear_history).pack(side="right", padx=(6, 0))
        ctk.CTkButton(bar, text="Open music folder", height=34, font=F(12), fg_color=HOVER,
                      hover_color=BORDER, text_color=TEXT,
                      command=lambda: open_path(self._out_dir())).pack(side="right")
        ctk.CTkButton(bar, text="Export M3U", height=34, font=F(12), fg_color=HOVER, hover_color=BORDER,
                      text_color=TEXT, command=self._export_m3u).pack(side="right", padx=6)
        self.lib_scroll = ctk.CTkScrollableFrame(pg, fg_color="transparent", scrollbar_button_color=HOVER,
                                                 scrollbar_button_hover_color=BORDER)
        self.lib_scroll.grid(row=2, column=0, sticky="nsew", padx=36, pady=(0, 22))
        self.lib_scroll.grid_columnconfigure(0, weight=1)

    def _lib_debounce(self, _e=None):
        if self._lib_after:
            self.after_cancel(self._lib_after)
        self._lib_after = self.after(250, self._refresh_library)

    def _refresh_library(self):
        self._lib_gen = getattr(self, "_lib_gen", 0) + 1
        gen = self._lib_gen
        for w in list(self.lib_scroll.winfo_children()):
            w.destroy()
        q = self.lib_entry.get().strip()
        rows = self.db.search(q, 51, self.lib_sort.get())
        if not rows:
            ctk.CTkLabel(self.lib_scroll, text="No downloads yet" if not q else "No matches", font=F(15),
                         text_color=DIM).pack(pady=80)
            return
        self._build_lib(rows[:50], gen, 0, len(rows) > 50)

    def _build_lib(self, rows, gen, start, more):
        if gen != self._lib_gen:
            return
        for rid, title, artist, path, fmt, size, ts in rows[start:start + 3]:
            self._lib_row(rid, title, artist, path, fmt, size, ts)
        if start + 3 < len(rows):
            self.after(6, lambda: self._build_lib(rows, gen, start + 3, more))
        elif more:
            ctk.CTkLabel(self.lib_scroll, text="Showing the latest 50 — use the filter to find older tracks",
                         font=F(11), text_color=DIM).pack(pady=12)

    def _lib_row(self, rid, title, artist, path, fmt, size, ts):
        exists = Path(path).exists()
        card = ctk.CTkFrame(self.lib_scroll, fg_color=CARD, corner_radius=10, border_width=1, border_color=BORDER)
        card.pack(fill="x", pady=3)
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(card, text=shorten(f"{artist} — {title}" if artist else title, 80), anchor="w",
                     font=F(13, True)).grid(row=0, column=0, sticky="sw", padx=16, pady=(10, 0))
        meta = f"{fmt}  ·  {fmt_size(size or 0)}  ·  {datetime.fromtimestamp(ts):%d %b %Y, %H:%M}"
        ctk.CTkLabel(card, text=meta if exists else "File missing  ·  " + meta, anchor="w", font=F(11),
                     text_color=DIM if exists else RED).grid(row=1, column=0, sticky="nw", padx=16, pady=(2, 10))
        st = "normal" if exists else "disabled"
        ctk.CTkButton(card, text="▶ Play", width=70, height=30, font=F(12), fg_color=ACCENT, hover_color=ACCENT_H,
                      text_color="white", state=st,
                      command=lambda p=path, tt=title: self._play_file(p, tt)).grid(row=0, column=1, rowspan=2, padx=2)
        ctk.CTkButton(card, text="Folder", width=64, height=30, font=F(12), fg_color=HOVER, hover_color=BORDER,
                      text_color=TEXT, state=st,
                      command=lambda p=path: open_path(Path(p).parent)).grid(row=0, column=2, rowspan=2, padx=2)
        ctk.CTkButton(card, text="✕", width=30, height=30, font=F(12), fg_color="transparent", hover_color=HOVER,
                      text_color=DIM, command=lambda r=rid: (self.db.delete(r), self._refresh_library())
                      ).grid(row=0, column=3, rowspan=2, padx=(2, 12))

    def _clear_history(self):
        if messagebox.askyesno("Clear history", "Remove all entries from the library list?\n"
                                                "(Your audio files are not deleted.)"):
            self.db.clear()
            self._refresh_library()
            self.toast("History cleared", "info", ms=2000)

    # ───────── settings page ─────────
    def _build_settings(self):
        pg = ctk.CTkFrame(self.main, fg_color="transparent")
        pg.grid_columnconfigure(0, weight=1)
        pg.grid_rowconfigure(1, weight=1)
        self.pages["settings"] = pg
        self._title(pg, "Settings", "Changes apply instantly and are saved automatically").grid(
            row=0, column=0, sticky="w", padx=36, pady=(26, 12))
        sc = ctk.CTkScrollableFrame(pg, fg_color="transparent", scrollbar_button_color=HOVER,
                                    scrollbar_button_hover_color=BORDER)
        sc.grid(row=1, column=0, sticky="nsew", padx=(36, 26), pady=(0, 22))
        sc.grid_columnconfigure(0, weight=1)

        def section(title):
            ctk.CTkLabel(sc, text=title.upper(), font=F(11, True), text_color=DIM, anchor="w").pack(
                fill="x", padx=6, pady=(14, 6))
            c = ctk.CTkFrame(sc, fg_color=CARD, corner_radius=14, border_width=1, border_color=BORDER)
            c.pack(fill="x")
            return c

        def row(parent, title, desc=""):
            r = ctk.CTkFrame(parent, fg_color="transparent")
            r.pack(fill="x", padx=20, pady=9)
            left = ctk.CTkFrame(r, fg_color="transparent")
            left.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(left, text=title, font=F(13, True), anchor="w").pack(anchor="w")
            if desc:
                ctk.CTkLabel(left, text=desc, font=F(11), text_color=DIM, anchor="w", justify="left"
                             ).pack(anchor="w")
            return r

        def switch(parent, title, desc, key):
            r = row(parent, title, desc)
            var = ctk.BooleanVar(value=self.cfg[key])
            ctk.CTkSwitch(r, text="", variable=var, width=46, progress_color=ACCENT,
                          command=lambda: self._set(key, var.get())).pack(side="right")

        def menu(parent, title, desc, key, values, cb=None, width=170):
            r = row(parent, title, desc)
            m = ctk.CTkOptionMenu(r, values=values, width=width, height=32, font=F(12), fg_color=HOVER,
                                  button_color=HOVER, button_hover_color=BORDER, text_color=TEXT,
                                  command=cb or (lambda v: self._set(key, v)))
            m.set(self.cfg[key])
            m.pack(side="right")

        c = section("Output")
        r = row(c, "Download folder", "Where finished files are saved")
        ctk.CTkButton(r, text="Open", width=60, height=32, font=F(12), fg_color=HOVER, hover_color=BORDER,
                      text_color=TEXT, command=lambda: open_path(self._out_dir())).pack(side="right")
        ctk.CTkButton(r, text="Change…", width=84, height=32, font=F(12), fg_color=ACCENT,
                      hover_color=ACCENT_H, text_color="white", command=self._choose_dir
                      ).pack(side="right", padx=6)
        self.dir_lbl = ctk.CTkLabel(r, text=shorten_left(self.cfg["output_dir"], 36), font=F(11),
                                    text_color=DIM)
        self.dir_lbl.pack(side="right", padx=8)
        menu(c, "Folder layout", "One folder, per artist, or Artist / Album (album comes from MusicBrainz)", "organize",
             ["Flat", "Artist folders", "Artist / Album"])
        menu(c, "File names", "How saved files are named", "name_style", ["Artist - Title", "Title only"])

        c = section("Audio")
        r = row(c, "Format & quality", "MP3 for compatibility, FLAC/Opus for best quality")
        self._preset_menu(r).pack(side="right")
        switch(c, "Normalize volume", "Even out loudness across songs (EBU R128) — slower conversion",
               "normalize")
        switch(c, "Trim non-music parts", "Cut intros/skits from music videos via SponsorBlock", "sponsorblock")

        c = section("Tags & artwork")
        switch(c, "Write tags", "Title, artist and year written into each file (ID3v2.3 for MP3)", "embed_tags")
        switch(c, "Embed cover art", "Adds album art that shows up in every player", "embed_cover")
        switch(c, "Square-crop cover", "Centre-crops 16:9 thumbnails to a square cover", "square_cover")
        switch(c, "Embed lyrics", "Looks up lyrics online and stores them in the file", "embed_lyrics")
        switch(c, "Save .lrc file", "Also keep time-synced lyrics next to the song", "save_lrc")
        switch(c, "Fetch album info", "MusicBrainz lookup: album, track number, official cover art", "musicbrainz")

        c = section("Behaviour")
        switch(c, "Skip duplicates", "Don't download a song that's already in your library", "skip_existing")
        menu(c, "Simultaneous downloads", "More is faster, but can trigger rate limits", "concurrency",
             ["1", "2", "3", "4", "5"], cb=lambda v: (self._set("concurrency", v), self._pump()), width=90)
        switch(c, "Desktop notification", "Notify when a batch finishes (needs notify-send)", "notify")
        switch(c, "Watch clipboard", "Offer to search when you copy a YouTube / SoundCloud link", "watch_clipboard")

        c = section("Network & access")
        menu(c, "Browser cookies", "Firefox works out of the box; Chrome-family browsers need the system keyring", "browser",
             BROWSERS, cb=self._set_browser, width=130)
        menu(c, "Speed limit", "Cap download speed per file", "ratelimit", list(RATES), width=130)
        menu(c, "Auto-retry", "Attempts per song when the network hiccups", "retries", ["1", "2", "3", "4", "5"],
             width=90)
        r = row(c, "JavaScript runtime", "Required by YouTube. Without it downloads fail with HTTP 403")
        ctk.CTkButton(r, text="Install Deno", width=110, height=32, font=F(12, True), fg_color=ACCENT,
                      hover_color=ACCENT_H, text_color="white", command=self._install_js_runtime
                      ).pack(side="right")
        ctk.CTkButton(r, text="Re-check", width=80, height=32, font=F(12), fg_color=HOVER, hover_color=BORDER,
                      text_color=TEXT, command=self._recheck_js).pack(side="right", padx=6)
        self.js_status = ctk.CTkLabel(r, text="", font=F(12, True))
        self.js_status.pack(side="right", padx=8)

        c = section("Appearance")
        menu(c, "Theme", "Dark, light, or follow your system", "appearance", ["Dark", "Light", "System"],
             cb=self._set_theme, width=120)
        menu(c, "Interface scale", "Raise this on HiDPI screens", "ui_scale", list(SCALES),
             cb=self._set_scale, width=100)

        c = section("Maintenance")
        r = row(c, "yt-dlp", f"Installed version: {self.ytdlp_ver} — update if downloads start failing")
        self.upd_btn = ctk.CTkButton(r, text="Update yt-dlp", width=120, height=32, font=F(12, True),
                                     fg_color=ACCENT, hover_color=ACCENT_H, text_color="white",
                                     command=self._update_ytdlp)
        self.upd_btn.pack(side="right")
        r = row(c, "Desktop launcher", "Add SonicGrabber to your application menu")
        ctk.CTkButton(r, text="Install", width=90, height=32, font=F(12, True), fg_color=ACCENT,
                      hover_color=ACCENT_H, text_color="white", command=self._install_launcher).pack(side="right")
        r = row(c, "Thumbnail cache", "Cached search-result images")
        ctk.CTkButton(r, text="Clear cache", width=110, height=32, font=F(12), fg_color=HOVER,
                      hover_color=BORDER, text_color=TEXT, command=self._clear_cache).pack(side="right")
        r = row(c, "Reset settings", "Restore defaults and restart the app")
        ctk.CTkButton(r, text="Reset…", width=90, height=32, font=F(12), fg_color=HOVER, hover_color=BORDER,
                      text_color=RED, command=self._reset).pack(side="right")

        sysf = ctk.CTkFrame(sc, fg_color="transparent")
        sysf.pack(fill="x", pady=(16, 6))
        ctk.CTkLabel(sysf, text=f"Python {sys.version.split()[0]}  ·  Pillow {'✓' if PIL_OK else '✗'}  ·  "
                                f"mutagen {'✓' if MUTAGEN_OK else '✗'}  ·  lyrics {'✓' if LYRICS_OK else '✗'}  ·  "
                                f"config {shorten_left(str(CFG_FILE), 40)}",
                     font=F(10, mono=True), text_color=DIM).pack(anchor="w", padx=6)

    def _choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.cfg["output_dir"], title="Choose download folder")
        if d:
            self._set("output_dir", d)
            self.dir_lbl.configure(text=shorten_left(d, 36))
            self._update_save_label()
            self.toast("Download folder updated", "ok", ms=2000)

    def _set_theme(self, v):
        self._set("appearance", v)
        ctk.set_appearance_mode(v.lower())

    def _set_scale(self, v):
        self._set("ui_scale", v)
        ctk.set_widget_scaling(SCALES[v])
        ctk.set_window_scaling(SCALES[v])

    def _update_ytdlp(self):
        if FROZEN:
            self.toast("yt-dlp is bundled in this packaged build — get a newer SonicGrabber build "
                       "(or run build.sh) to update it", "info", ms=7000)
            return
        self.upd_btn.configure(state="disabled", text="Updating…")

        def work():
            try:
                r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
                                   capture_output=True, text=True, timeout=240)
                ok = r.returncode == 0
                log.info("pip: %s", (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr) else "")
                if not ok:
                    log.error("Update failed — if pip refuses (externally-managed), use a venv or pipx")
                self.ui(self.toast, "yt-dlp updated — restart the app to use it" if ok else
                        "Update failed — see the Activity log", "ok" if ok else "err", None, 6000)
            except Exception as e:
                self.ui(self.toast, f"Update failed: {clean_err(e)}", "err")
            self.ui(self.upd_btn.configure, state="normal", text="Update yt-dlp")

        threading.Thread(target=work, daemon=True).start()

    def _clear_cache(self):
        n = 0
        for p in CACHE_DIR.glob("*.jpg"):
            p.unlink(missing_ok=True)
            n += 1
        self._thumb_cache.clear()
        self.toast(f"Removed {n} cached images", "ok", ms=2200)

    def _reset(self):
        if messagebox.askyesno("Reset settings", "Restore all settings to defaults and restart?"):
            save_cfg(dict(DEFAULTS))
            self._closing = True
            if FROZEN:
                self.toast("Settings reset — please reopen SonicGrabber", "ok")
                self.after(1500, self.destroy)
            else:
                os.execv(sys.executable, [sys.executable] + sys.argv)

    # ───────── log page ─────────
    def _build_log(self):
        pg = ctk.CTkFrame(self.main, fg_color="transparent")
        pg.grid_columnconfigure(0, weight=1)
        pg.grid_rowconfigure(1, weight=1)
        self.pages["log"] = pg
        hdr = ctk.CTkFrame(pg, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=36, pady=(26, 12))
        self._title(hdr, "Activity log", f"Also saved to {shorten_left(str(LOG_FILE), 50)}").pack(side="left")
        ctk.CTkButton(hdr, text="Clear", width=70, height=32, font=F(12), fg_color=HOVER, hover_color=BORDER,
                      text_color=TEXT, command=self._clear_log).pack(side="right", padx=(6, 0))
        ctk.CTkButton(hdr, text="Copy", width=70, height=32, font=F(12), fg_color=HOVER, hover_color=BORDER,
                      text_color=TEXT, command=self._copy_log).pack(side="right")
        self.log_text = ctk.CTkTextbox(pg, font=F(12, mono=True), fg_color=CARD, border_width=1,
                                       border_color=BORDER, wrap="word", state="disabled",
                                       scrollbar_button_color=HOVER, scrollbar_button_hover_color=BORDER)
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=36, pady=(0, 22))
        self.log_text._textbox.tag_config("err", foreground=RED)
        self.log_text._textbox.tag_config("warn", foreground=AMBER)

    def _append_log(self, text, level):
        tag = "err" if level >= logging.ERROR else "warn" if level >= logging.WARNING else ""
        self.log_text.configure(state="normal")
        self.log_text._textbox.insert("end", text + "\n", tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _copy_log(self):
        self.clipboard_clear()
        self.clipboard_append(self.log_text.get("1.0", "end"))
        self.toast("Log copied to clipboard", "ok", ms=1800)

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ───────── JavaScript runtime (fixes YouTube HTTP 403) ─────────
    def _update_js_ui(self):
        if JS_INFO:
            self.js_btn.pack_forget()
            self.js_status.configure(text=f"{JS_INFO[0]} {'.'.join(map(str, JS_INFO[2]))} ✓", text_color=GREEN)
        else:
            self.js_btn.configure(state="normal", text="⚠  Fix YouTube downloads")
            if not self.js_btn.winfo_manager():
                self.js_btn.pack(fill="x", padx=12, pady=(0, 8), before=self._js_before)
            self.js_status.configure(text="not found", text_color=RED)

    def _set_browser(self, v):
        self._set("browser", v)
        self._plan_start = 0
        if v != "None":
            self.toast(f"Reading cookies from {v}…", "info", ms=2500)
        COOKIES.load(v, lambda c: self.ui(self._cookies_done, c, True))

    def _cookies_done(self, c, announce):
        if c.state == "ready":
            if announce:
                self.toast(f"Loaded {len(c.jar)} cookies from {c.browser}", "ok", ms=3000)
        elif c.state == "failed":
            self.toast(f"Couldn't read {c.browser} cookies: {c.error}. Downloads will continue without them.",
                       "warn", ms=12000)

    def _plans(self):
        """Approaches to try; without loaded cookies the with/without-cookies variants would be identical."""
        if COOKIES.get() is not None:
            return PLANS
        out = []
        for clients, _ in PLANS:
            if (clients, False) not in out:
                out.append((clients, False))
        return out

    def _plan(self, attempt: int):
        plans = self._plans()
        idx = (self._plan_start + attempt - 1) % len(plans)
        return (idx,) + plans[idx]

    def _dump_diag(self, job: Job):
        """Write yt-dlp's own explanation of a failure into the Activity log (limited, so it can't flood)."""
        y = getattr(job, "ylog", None)
        if y and y.lines and self._diag_budget > 0:
            self._diag_budget -= 1
            log.warning("Why it failed — what yt-dlp reported for “%s”:", job.track.title)
            for l in y.lines[-8:]:
                log.warning("   ▸ %s", l)

    def _recheck_js(self):
        refresh_js()
        self._update_js_ui()
        self.toast(f"JavaScript runtime: {JS_INFO[0]} {'.'.join(map(str, JS_INFO[2]))}" if JS_INFO
                   else "No usable JavaScript runtime found", "ok" if JS_INFO else "warn", ms=3000)

    def _install_js_runtime(self):
        arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(
            platform.machine().lower())
        if not arch or not sys.platform.startswith("linux"):
            self.toast("Automatic setup isn't available here — install Deno from deno.com", "warn", ms=7000)
            return
        if not messagebox.askyesno("Fix YouTube downloads",
                                   "YouTube needs a JavaScript runtime (Deno, about 40 MB).\n\n"
                                   "SonicGrabber will download it from the official Deno releases on GitHub into\n"
                                   f"{DATA_DIR / 'runtime'}\n\nContinue?"):
            return
        self.js_btn.configure(state="disabled", text="Installing Deno…")
        self.toast("Downloading Deno (about 40 MB)…", "info", ms=5000)
        threading.Thread(target=self._install_js_worker, args=(arch,), daemon=True).start()

    def _install_js_worker(self, arch: str):
        try:
            url = f"https://github.com/denoland/deno/releases/latest/download/deno-{arch}-unknown-linux-gnu.zip"
            dest = DATA_DIR / "runtime"
            dest.mkdir(parents=True, exist_ok=True)
            data = http_get(url, 240)
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                z.extract("deno", dest)
            (dest / "deno").chmod(0o755)
            v = js_version(str(dest / "deno"))
            if not v or v < JS_MIN["deno"]:
                raise RuntimeError("the downloaded runtime did not start")
            refresh_js()
            log.info("Installed Deno %s", ".".join(map(str, v)))
            self.ui(self._js_ready)
        except Exception as e:
            log.error("Deno setup failed: %s", clean_err(e))
            self.ui(self._js_failed, clean_err(e))

    def _js_ready(self):
        self._update_js_ui()
        self.toast("JavaScript runtime ready — retrying your failed downloads", "ok", ms=5000)
        self.retry_failed()

    def _js_failed(self, msg):
        self._update_js_ui()
        self.toast(f"Couldn't set up Deno ({msg}). Install it manually:  curl -fsSL https://deno.land/install.sh | sh",
                   "err", ms=15000)

    # ───────── extras: persistence, player, clipboard, library tools ─────────
    def _save_queue(self, force=False):
        if not self._restored:
            return
        pend = [j for j in self.jobs if j.state == "queued" or j.state in ACTIVE]
        sig = tuple(j.track.vid for j in pend)
        if sig == self._queue_sig and not force:
            return
        self._queue_sig = sig
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            data = [dict(vid=j.track.vid, title=j.track.title, artist=j.track.artist,
                         duration=j.track.duration, url=j.track.url, thumb=j.track.thumb, views=j.track.views)
                    for j in pend]
            tmp = QUEUE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), "utf-8")
            tmp.replace(QUEUE_FILE)
        except Exception as e:
            log.debug("Could not save queue: %s", e)

    def _restore_queue(self):
        try:
            items = json.loads(QUEUE_FILE.read_text("utf-8"))
        except Exception:
            items = []
        n = 0
        for d in items:
            try:
                t = Track(**d)
            except TypeError:
                continue
            self.job_seq += 1
            job = Job(self.job_seq, t)
            job.row = JobRow(self.q_scroll, job, self)
            job.row.pack(fill="x", pady=4)
            self.jobs.append(job)
            n += 1
        self._restored = True
        if n:
            self.paused = True
            self.pause_btn.configure(text="Resume queue", fg_color=GREEN, hover_color=GREEN_H, text_color="white")
            self.toast(f"Restored {n} unfinished download{'s' if n != 1 else ''} from last time", "info",
                       action=("Resume", self.toggle_pause), ms=9000)
        self._queue_sig = tuple(j.track.vid for j in self.jobs)
        self._refresh_queue_ui()

    def prioritize(self, job: Job):
        if job.state != "queued" or job not in self.jobs:
            return
        self.jobs.remove(job)
        self.jobs.insert(next((i for i, j in enumerate(self.jobs) if j.state == "queued"), len(self.jobs)), job)
        for j in self.jobs:
            j.row.pack_forget()
            j.row.pack(fill="x", pady=4)
        self.toast("Moved to the front of the queue", "info", ms=1600)
        self._pump()

    def _np_update(self, title):
        if title:
            self.np_lbl.configure(text="♪  " + shorten(title, 56))
            self.np_frame.pack(side="left", padx=24)
        else:
            self.np_frame.pack_forget()

    def _preview(self, t: Track):
        if not Player.available():
            self.toast("Install mpv for in-app playback:  sudo apt install mpv", "warn", ms=5000)
            return
        self.toast(f"Loading preview: {shorten(t.title, 36)}", "info", ms=1800)

        def work():
            try:
                with new_ydl({"format": "bestaudio/best", "quiet": True, "no_warnings": True,
                                       "noplaylist": True, "socket_timeout": 15, **ydl_common(self.cfg)}) as ydl:
                    info = ydl.extract_info(t.url, download=False)
                ua = (info.get("http_headers") or {}).get("User-Agent")
                self.player.play(info["url"], f"{t.artist} — {t.title}" if t.artist else t.title, ua)
            except Exception as e:
                self.ui(self.toast, f"Preview failed: {clean_err(e)}", "err")

        threading.Thread(target=work, daemon=True).start()

    def _play_file(self, path, title):
        if Player.available():
            self.player.play(path, title)
        else:
            open_path(path)

    def _watch_clipboard(self):
        if self._closing:
            return
        if self.cfg.get("watch_clipboard"):
            try:
                txt = self.clipboard_get().strip()
            except Exception:
                txt = ""
            if txt and txt != self._clip_last:
                self._clip_last = txt
                if re.match(r"https?://(www\.|m\.|music\.)?(youtube\.com|youtu\.be|soundcloud\.com)/\S+$", txt) \
                        and txt != self.search_entry.get().strip():
                    self.toast("Music link copied — look it up?", "info", ms=8000,
                               action=("Open", lambda u=txt: (self._nav("search"), self._run_query(u))))

    def start_cli(self, q: str, download: bool):
        self._auto_dl = ("all" if is_url(q) else "first") if download else None
        self._run_query(q)

    def _export_m3u(self):
        rows = [r for r in self.db.search(self.lib_entry.get().strip(), 5000, self.lib_sort.get())
                if Path(r[3]).exists()]
        if not rows:
            self.toast("Nothing to export yet", "warn", ms=2000)
            return
        f = filedialog.asksaveasfilename(defaultextension=".m3u8", initialfile="SonicGrabber.m3u8",
                                         filetypes=[("Playlist", "*.m3u8 *.m3u")])
        if f:
            lines = ["#EXTM3U"] + [f"#EXTINF:-1,{a + ' - ' if a else ''}{t}\n{p}" for _, t, a, p, *_ in rows]
            Path(f).write_text("\n".join(lines) + "\n", "utf-8")
            self.toast(f"Exported {len(rows)} tracks", "ok", ms=2500)

    def _install_launcher(self):
        try:
            f = install_launcher()
            self.toast(f"Added to your app menu ({f.name})", "ok", ms=3500)
        except Exception as e:
            self.toast(f"Could not install launcher: {clean_err(e)}", "err")

    def _present(self):
        """Centre the window on screen (clamped to its size) and bring it to the front."""
        try:
            self.update_idletasks()
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            m = re.match(r"(\d+)x(\d+)", str(self.cfg.get("geometry", "")))
            w, h = (int(m.group(1)), int(m.group(2))) if m else (1180, 780)
            w, h = min(w, sw - 60), min(h, sh - 100)
            self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
            self.deiconify()
            self.lift()
            self.focus_force()
            self.attributes("-topmost", True)
            self.after(400, lambda: self.attributes("-topmost", False))
        except Exception:
            log.debug("present failed", exc_info=True)

    def report_callback_exception(self, exc, val, tb):
        log.error("UI error: %s", "".join(traceback.format_exception(exc, val, tb))[-700:])
        traceback.print_exception(exc, val, tb)

    # ───────── status bar / quit ─────────
    def _build_statusbar(self):
        sb = ctk.CTkFrame(self, height=32, fg_color=SIDEBAR, corner_radius=0)
        sb.grid(row=1, column=1, sticky="ew")
        self.sb_dot = ctk.CTkLabel(sb, text="●", text_color=DIM, font=F(11))
        self.sb_dot.pack(side="left", padx=(16, 6), pady=5)
        self.sb_txt = ctk.CTkLabel(sb, text="Ready", font=F(11), text_color=DIM)
        self.sb_txt.pack(side="left", pady=5)
        self.np_frame = ctk.CTkFrame(sb, fg_color="transparent")
        self.np_lbl = ctk.CTkLabel(self.np_frame, text="", font=F(11), text_color=TEXT)
        self.np_lbl.pack(side="left")
        ctk.CTkButton(self.np_frame, text="■ Stop", width=56, height=22, font=F(10, True), fg_color=HOVER,
                      hover_color=BORDER, text_color=TEXT, command=lambda: self.player.stop()).pack(side="left", padx=8)
        self.sb_speed = ctk.CTkLabel(sb, text="", font=F(11), text_color=DIM, width=80, anchor="e")
        self.sb_speed.pack(side="right", padx=(0, 16))
        self.sb_bar = ctk.CTkProgressBar(sb, width=160, height=6, fg_color=HOVER, progress_color=ACCENT)
        self.sb_bar.set(0)

    def _quit(self):
        busy = [j for j in self.jobs if j.state in ACTIVE or j.state == "queued"]
        if busy and not messagebox.askyesno("Downloads in progress",
                                            f"{len(busy)} download(s) are unfinished. They will be restored the next time you open SonicGrabber.\nQuit now?"):
            return
        self._save_queue(True)
        self._closing = True
        self.player.stop()
        for j in self.jobs:
            j.cancel.set()
        self.cfg["geometry"] = self.geometry().split("+")[0]
        save_cfg(self.cfg)
        self._thumb_pool.shutdown(wait=False, cancel_futures=True)
        self.destroy()


def selftest() -> int:
    """`sonicgrabber --selftest` — verifies every component works (handy for packaged builds)."""
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            print(f"  ok    {name}: {fn()}")
        except Exception as e:
            ok = False
            print(f"  FAIL  {name}: {e!r}")

    def cover():
        buf = io.BytesIO()
        Image.new("RGB", (400, 300), (10, 120, 200)).save(buf, "PNG")
        return f"{len(make_cover(buf.getvalue(), True))} byte JPEG"

    def need_ffmpeg():
        d = find_ffmpeg()
        if not d:
            raise RuntimeError("ffmpeg/ffprobe not found in PATH")
        return d

    def window():
        a = App()
        a.update()
        a.destroy()
        return "window created"

    print(f"SonicGrabber {VER} self-test ({'packaged' if FROZEN else 'script'}, Python {sys.version.split()[0]})")
    check("yt-dlp", lambda: f"{yt_dlp.version.__version__}, {len(yt_dlp.extractor.gen_extractor_classes())} extractors")
    check("YouTube extractor", lambda: yt_dlp.YoutubeDL({"quiet": True}).get_info_extractor("Youtube").IE_NAME)
    check("Pillow", cover)
    check("mutagen", lambda: MUTAGEN_OK or (_ for _ in ()).throw(RuntimeError("import failed")))
    check("lyrics", lambda: LYRICS_OK or (_ for _ in ()).throw(RuntimeError("import failed")))
    check("HTTPS certs", lambda: os.environ.get("SSL_CERT_FILE") or ssl.get_default_verify_paths().cafile)
    check("FFmpeg", need_ffmpeg)
    from yt_dlp.dependencies import secretstorage as _ss
    print("  ok    keyring support (Chrome-family cookies): yes" if _ss else
          "  warn  keyring support missing — Chrome/Chromium/Brave cookies can't be decrypted (Firefox is fine)")
    refresh_js()
    print(f"  ok    JS runtime: {JS_INFO[0]} {'.'.join(map(str, JS_INFO[2]))}" if JS_INFO else
          "  warn  JS runtime: none — YouTube downloads fail with 403 until you click “Fix YouTube downloads”")
    if JS_OPTS:  # prove the runtime can be launched exactly the way yt-dlp will launch it
        check("JS runtime launch", lambda: subprocess.run([next(iter(JS_OPTS.values()))["path"], "--version"],
                                                          capture_output=True, text=True, timeout=10).stdout.split("\n")[0])
    check("Tk window", window)
    print("ALL GOOD" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="SonicGrabber Pro — music downloader")
    ap.add_argument("query", nargs="*", help="search text or a YouTube / SoundCloud link")
    ap.add_argument("-d", "--download", action="store_true",
                    help="start downloading immediately (links: everything; text: the best match)")
    ap.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest())
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        sys.exit("No display found — run SonicGrabber from a desktop session.")
    print(f"SonicGrabber Pro {VER} starting… (log: {LOG_FILE})", flush=True)
    try:
        app = App()
    except Exception:
        err = traceback.format_exc()
        print(err, file=sys.stderr)
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            (DATA_DIR / "crash.log").write_text(err, "utf-8")
            messagebox.showerror("SonicGrabber failed to start", err[-900:])
        except Exception:
            pass
        sys.exit(1)
    q = " ".join(args.query).strip()
    if q:
        app.after(500, lambda: app.start_cli(q, args.download))
    app.mainloop()


if __name__ == "__main__":
    main()
