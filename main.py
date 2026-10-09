#!/usr/bin/env python3
"""SonicGrabber Pro v5.3 – Fixed thumbnails, search bar, robust state."""

import os, re, sys, json, copy, shutil, subprocess, threading
import concurrent.futures
import io
import hashlib
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from typing import List

import customtkinter as ctk
from tkinter import filedialog, messagebox
import yt_dlp

try:
    from PIL import Image
    PIL_OK = True
except Exception:
    PIL_OK = False

try:
    import syncedlyrics; LYRICS_OK = True
except Exception: LYRICS_OK = False
try:
    from mutagen.id3 import ID3, USLT, ID3NoHeaderError
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4
    MUTAGEN_OK = True
except Exception: MUTAGEN_OK = False

APP = "SonicGrabber Pro"
VER = "5.3.0"
CFG = Path.home() / ".sonicgrabber_v5.json"
THUMB_CACHE = Path.home() / ".sonicgrabber_thumbs"

FORMATS = {
    "MP3 · 320 kbps":  {"codec": "mp3",  "q": "320", "ext": "mp3"},
    "FLAC · Lossless": {"codec": "flac", "q": "0",   "ext": "flac"},
    "M4A · AAC 256":   {"codec": "m4a",  "q": "256", "ext": "m4a"},
    "Opus · Best":     {"codec": "opus", "q": "0",   "ext": "opus"},
}
THREADS = {"1 · Sequential": 1, "3 · Parallel": 3, "5 · Parallel": 5}
DEFAULTS = {
    "output_dir": str(Path.home() / "Music" / "SonicGrabber"),
    "format": "MP3 · 320 kbps", "threads": "1 · Sequential",
    "embed_meta": True, "embed_lyrics": True, "save_lrc": False,
    "normalize": False, "skip_existing": True, "geometry": "1100x740",
}

BG = "#0d0d0f"; CARD = "#16161a"; HOVER = "#1e1e24"; BORDER = "#2a2a32"
ACCENT = "#7c5cfc"; ACCENT2 = "#6344e0"; GREEN = "#22c55e"; RED = "#ef4444"
AMBER = "#f59e0b"; DIM = "#8b8b96"; TEXT = "#f0f0f4"


def load_cfg():
    if CFG.exists():
        try: return {**DEFAULTS, **json.loads(CFG.read_text("utf-8"))}
        except: pass
    return dict(DEFAULTS)

def save_cfg(c):
    try: CFG.write_text(json.dumps(c, indent=2), "utf-8")
    except: pass

def safe_name(n):
    return re.sub(r'[\\/*?:"<>|]', "", n).strip().strip(".")[:150] or "Untitled"

def ensure_dir(p: Path) -> Path:
    try: p.mkdir(parents=True, exist_ok=True)
    except OSError: pass
    return p

def fmt_dur(s):
    if not s: return "—"
    m, sec = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

def is_url(text):
    return text.startswith("http://") or text.startswith("https://")

def is_music(entry):
    title = (entry.get("title") or "").lower()
    hard_skip = ["interview", "behind the scenes", "documentary",
                 "reaction video", "commentary", "making of"]
    return not any(k in title for k in hard_skip)

def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        d = str(Path(exe).resolve().parent)
        probe = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
        if (Path(d) / probe).exists(): return d
    if sys.platform == "win32":
        for c in [Path.home()/"AppData"/"Local"/"Microsoft"/"WinGet"/"Links",
                  Path.home()/"AppData"/"Local"/"Microsoft"/"WinGet"/"Packages",
                  Path(r"C:\ffmpeg\bin"), Path(r"C:\tools\ffmpeg\bin")]:
            if not c.exists(): continue
            hits = list(c.rglob("ffmpeg.exe")) if "Packages" in str(c) else [c/"ffmpeg.exe"]
            for h in hits:
                if h.exists(): return str(h.parent)
    elif sys.platform == "darwin":
        for d in ("/opt/homebrew/bin", "/usr/local/bin"):
            if Path(d, "ffmpeg").exists(): return d
    elif sys.platform.startswith("linux"):
        for d in ("/usr/bin", "/usr/local/bin", "/snap/bin"):
            if Path(d, "ffmpeg").exists(): return d
    return None

def ff_ok(d):
    if not d: return False
    exe = Path(d) / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    if not exe.exists(): return False
    try:
        subprocess.run([str(exe), "-version"], capture_output=True, timeout=5)
        return True
    except: return False

def embed_lyrics(fp, title, save_lrc, log):
    if not LYRICS_OK or not MUTAGEN_OK: return
    try:
        txt = syncedlyrics.search(title)
        if not txt: return
        if save_lrc: fp.with_suffix(".lrc").write_text(txt, "utf-8")
        s = fp.suffix.lower()
        if s == ".mp3":
            try: a = ID3(fp)
            except ID3NoHeaderError: a = ID3()
            a.add(USLT(encoding=3, lang="eng", desc="", text=txt))
            a.save(fp, v2_version=3)
        elif s == ".flac": a = FLAC(fp); a["LYRICS"] = txt; a.save()
        elif s == ".m4a": a = MP4(fp); a["\xa9lyr"] = [txt]; a.save()
        else: return
        log(f"✅ Lyrics → {fp.name}")
    except: pass

def fix_cover(fp, log):
    if not MUTAGEN_OK or fp.suffix.lower() != ".mp3" or not fp.exists(): return
    try: a = ID3(fp)
    except: return
    pics = a.getall("APIC")
    if not pics: return
    best = max(pics, key=lambda f: len(f.data))
    a.delall("APIC"); best.type = 3; best.desc = "Cover"
    if not best.mime or "/" not in best.mime:
        if best.data[:3] == b"\xff\xd8\xff": best.mime = "image/jpeg"
        elif best.data[:4] == b"\x89PNG": best.mime = "image/png"
        else: best.mime = "image/jpeg"
    a.add(best); a.save(fp, v2_version=3)

class YLog:
    TAGS = ("[download]", "[ExtractAudio]", "[Metadata]", "[EmbedThumbnail]")
    def __init__(s, cb): s.cb = cb
    def debug(s, m):
        if any(t in m for t in s.TAGS): s.cb(m)
    def info(s, m): s.cb(m)
    def warning(s, m): s.cb(f"⚠ {m}")
    def error(s, m): s.cb(f"❌ {m}")


@dataclass
class Track:
    title: str
    artist: str
    duration: str
    url: str
    thumbnail: str = ""
    views: str = ""
    selected: bool = False


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.cfg = load_cfg()
        self.ff = find_ffmpeg()
        self.ff_ok = ff_ok(self.ff)
        self.cancel_evt = threading.Event()
        self.lock = threading.Lock()
        self.results: List[Track] = []
        self.worker = None
        self.downloading = False
        self.search_gen = 0  # generation counter to kill stale thumbnail threads

        ensure_dir(THUMB_CACHE)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title(f"{APP} v{VER}")
        self.geometry(self.cfg.get("geometry", "1100x740"))
        self.minsize(900, 600)
        self.configure(fg_color=BG)

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._sidebar()
        self._pages()
        self._statusbar()
        self._nav("search")
        self.protocol("WM_DELETE_WINDOW", self._quit)
        self._log(f"🚀 {APP} v{VER}")
        self._log(f"🎬 FFmpeg: {'✔' if self.ff_ok else '✘'}")
        self._log(f"🖼  Thumbnails: {'✔' if PIL_OK else '✘ (pip install Pillow)'}")

    # ───────────────── SIDEBAR ──────────────────────────────────────────────
    def _sidebar(self):
        sb = ctk.CTkFrame(self, width=200, fg_color=CARD, corner_radius=0)
        sb.grid(row=0, column=0, sticky="nsew")
        sb.grid_propagate(False)

        ctk.CTkLabel(sb, text="🎧", font=ctk.CTkFont(size=30)).pack(anchor="w", padx=20, pady=(28, 4))
        ctk.CTkLabel(sb, text="SonicGrabber", font=ctk.CTkFont(size=17, weight="bold")).pack(anchor="w", padx=20)
        ctk.CTkLabel(sb, text=f"v{VER}", font=ctk.CTkFont(size=11), text_color=DIM).pack(anchor="w", padx=20, pady=(0, 28))

        self.nav_btns = {}
        for key, icon, label in [("search", "🔍", "Search"),
                                  ("settings", "⚙️", "Settings"),
                                  ("log", "📋", "Activity Log")]:
            b = ctk.CTkButton(sb, text=f"  {icon}  {label}", anchor="w", height=42,
                              font=ctk.CTkFont(size=13), corner_radius=10,
                              fg_color="transparent", hover_color=HOVER,
                              text_color=TEXT, command=lambda k=key: self._nav(k))
            b.pack(fill="x", padx=10, pady=3)
            self.nav_btns[key] = b

        ctk.CTkFrame(sb, fg_color="transparent").pack(fill="both", expand=True)

        ff = ctk.CTkFrame(sb, fg_color=HOVER, corner_radius=8)
        ff.pack(fill="x", padx=12, pady=(0, 16))
        c = GREEN if self.ff_ok else RED
        t = "FFmpeg Ready" if self.ff_ok else "FFmpeg Missing"
        ctk.CTkLabel(ff, text="●", text_color=c, font=ctk.CTkFont(size=11)).pack(side="left", padx=(12, 6), pady=9)
        ctk.CTkLabel(ff, text=t, font=ctk.CTkFont(size=11), text_color=DIM).pack(side="left", pady=9)

    # ───────────────── PAGES ────────────────────────────────────────────────
    def _pages(self):
        self.main = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self.main.grid(row=0, column=1, sticky="nsew")
        self.main.grid_columnconfigure(0, weight=1)
        self.main.grid_rowconfigure(0, weight=1)
        self.page_frames = {}
        self._page_search()
        self._page_settings()
        self._page_log()

    def _nav(self, key):
        for k, b in self.nav_btns.items():
            b.configure(fg_color=ACCENT if k == key else "transparent")
        for k, f in self.page_frames.items():
            if k == key: f.grid(row=0, column=0, sticky="nsew")
            else: f.grid_remove()

    # ── SEARCH PAGE ─────────────────────────────────────────────────────────
    def _page_search(self):
        pg = ctk.CTkFrame(self.main, fg_color="transparent")
        pg.grid_columnconfigure(0, weight=1)
        pg.grid_rowconfigure(2, weight=1)

        top = ctk.CTkFrame(pg, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=40, pady=(28, 0))
        top.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(top, text="Search music or paste a link",
                     font=ctk.CTkFont(size=24, weight="bold")).grid(row=0, column=0, sticky="w")

        bar = ctk.CTkFrame(top, fg_color=CARD, corner_radius=14, height=52)
        bar.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        bar.grid_columnconfigure(0, weight=1)
        bar.grid_propagate(False)

        self.search_entry = ctk.CTkEntry(
            bar, placeholder_text="Search songs, artists, playlists — or paste a URL",
            font=ctk.CTkFont(size=14), fg_color="transparent", border_width=0, height=52)
        self.search_entry.grid(row=0, column=0, sticky="ew", padx=(18, 8))
        self.search_entry.bind("<Return>", lambda e: self._search())

        self.search_btn = ctk.CTkButton(
            bar, text="Search", width=100, height=38,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=ACCENT, hover_color=ACCENT2, command=self._search)
        self.search_btn.grid(row=0, column=1, padx=7, pady=7)

        # controls
        ctrl = ctk.CTkFrame(pg, fg_color="transparent")
        ctrl.grid(row=1, column=0, sticky="ew", padx=40, pady=(12, 0))

        ctk.CTkLabel(ctrl, text="Format", font=ctk.CTkFont(size=12), text_color=DIM).pack(side="left")
        self.fmt_menu = ctk.CTkOptionMenu(ctrl, values=list(FORMATS), width=170, height=34,
                                          fg_color=HOVER, button_color=ACCENT,
                                          button_hover_color=ACCENT2, font=ctk.CTkFont(size=12))
        self.fmt_menu.set(self.cfg["format"])
        self.fmt_menu.pack(side="left", padx=(8, 20))

        ctk.CTkLabel(ctrl, text="Threads", font=ctk.CTkFont(size=12), text_color=DIM).pack(side="left")
        self.thr_menu = ctk.CTkOptionMenu(ctrl, values=list(THREADS), width=140, height=34,
                                          fg_color=HOVER, button_color=ACCENT,
                                          button_hover_color=ACCENT2, font=ctk.CTkFont(size=12))
        self.thr_menu.set(self.cfg["threads"])
        self.thr_menu.pack(side="left", padx=(8, 20))

        ctk.CTkLabel(ctrl, text="Save to", font=ctk.CTkFont(size=12), text_color=DIM).pack(side="left")
        self.dir_lbl = ctk.CTkLabel(ctrl, text=self.cfg["output_dir"],
                                    font=ctk.CTkFont(size=11), text_color=DIM, width=160, anchor="w")
        self.dir_lbl.pack(side="left", padx=(8, 8))
        ctk.CTkButton(ctrl, text="Change", width=70, height=30,
                      fg_color=HOVER, hover_color=BORDER,
                      font=ctk.CTkFont(size=11), command=self._browse).pack(side="left")

        self.dl_btn = ctk.CTkButton(
            ctrl, text="⬇  Download All", width=170, height=36,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=GREEN, hover_color="#16a34a",
            command=self._download_selected, state="disabled")
        self.dl_btn.pack(side="right")

        self.cancel_btn = ctk.CTkButton(
            ctrl, text="✖ Cancel", width=90, height=36,
            font=ctk.CTkFont(size=12), fg_color=RED, hover_color="#dc2626",
            command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="right", padx=(0, 10))

        # results
        rw = ctk.CTkFrame(pg, fg_color="transparent")
        rw.grid(row=2, column=0, sticky="nsew", padx=40, pady=(14, 24))
        rw.grid_columnconfigure(0, weight=1)
        rw.grid_rowconfigure(0, weight=1)

        self.results_scroll = ctk.CTkScrollableFrame(
            rw, fg_color="transparent",
            scrollbar_button_color=HOVER, scrollbar_button_hover_color=BORDER)
        self.results_scroll.grid(row=0, column=0, sticky="nsew")
        self.results_scroll.grid_columnconfigure(0, weight=1)

        self._show_hint("Search for music above.\nResults with thumbnails appear here.")
        self.page_frames["search"] = pg

    # ── SETTINGS ────────────────────────────────────────────────────────────
    def _page_settings(self):
        pg = ctk.CTkFrame(self.main, fg_color="transparent")
        pg.grid_columnconfigure(0, weight=1)
        pg.grid_columnconfigure(1, weight=1)
        pg.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(pg, text="Settings", font=ctk.CTkFont(size=24, weight="bold")
                     ).grid(row=0, column=0, columnspan=2, sticky="w", padx=40, pady=(28, 20))

        left = ctk.CTkFrame(pg, fg_color=CARD, corner_radius=14)
        left.grid(row=1, column=0, sticky="nsew", padx=(40, 10), pady=(0, 28))
        ctk.CTkLabel(left, text="Features", font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor="w", padx=24, pady=(20, 14))

        self.v_meta = ctk.BooleanVar(value=self.cfg["embed_meta"])
        self.v_lyr  = ctk.BooleanVar(value=self.cfg["embed_lyrics"])
        self.v_lrc  = ctk.BooleanVar(value=self.cfg["save_lrc"])
        self.v_norm = ctk.BooleanVar(value=self.cfg["normalize"])
        self.v_skip = ctk.BooleanVar(value=self.cfg["skip_existing"])

        for txt, var in [("Embed cover art & metadata", self.v_meta),
                         ("Fetch & embed lyrics", self.v_lyr),
                         ("Save .lrc file alongside", self.v_lrc),
                         ("Volume normalization", self.v_norm),
                         ("Skip existing (playlist sync)", self.v_skip)]:
            ctk.CTkCheckBox(left, text=txt, variable=var, font=ctk.CTkFont(size=13),
                            fg_color=ACCENT, hover_color=ACCENT2
                            ).pack(anchor="w", padx=24, pady=6)
        ctk.CTkFrame(left, height=20, fg_color="transparent").pack()

        right = ctk.CTkFrame(pg, fg_color=CARD, corner_radius=14)
        right.grid(row=1, column=1, sticky="nsew", padx=(10, 40), pady=(0, 28))
        ctk.CTkLabel(right, text="System", font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor="w", padx=24, pady=(20, 14))
        for ln in [f"Python    {sys.version.split()[0]}",
                   f"FFmpeg    {'✔' if self.ff_ok else '✘'}",
                   f"Lyrics    {'✔' if LYRICS_OK else '✘'}",
                   f"Mutagen   {'✔' if MUTAGEN_OK else '✘'}",
                   f"Pillow    {'✔' if PIL_OK else '✘'}"]:
            ctk.CTkLabel(right, text=ln, font=ctk.CTkFont(family="Consolas", size=12),
                         text_color=DIM).pack(anchor="w", padx=24, pady=3)
        ctk.CTkFrame(right, height=24, fg_color="transparent").pack()
        ctk.CTkButton(right, text="Reset All Settings", width=180, height=36,
                      fg_color=RED, hover_color="#dc2626",
                      font=ctk.CTkFont(size=13), command=self._reset
                      ).pack(anchor="w", padx=24, pady=(0, 20))
        self.page_frames["settings"] = pg

    # ── LOG ─────────────────────────────────────────────────────────────────
    def _page_log(self):
        pg = ctk.CTkFrame(self.main, fg_color="transparent")
        pg.grid_columnconfigure(0, weight=1)
        pg.grid_rowconfigure(1, weight=1)
        hdr = ctk.CTkFrame(pg, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=40, pady=(28, 12))
        ctk.CTkLabel(hdr, text="Activity Log", font=ctk.CTkFont(size=24, weight="bold")).pack(side="left")
        ctk.CTkButton(hdr, text="Copy", width=70, height=32, fg_color=HOVER, hover_color=BORDER,
                      font=ctk.CTkFont(size=12), command=self._copy_log).pack(side="right", padx=(8, 0))
        ctk.CTkButton(hdr, text="Clear", width=70, height=32, fg_color=HOVER, hover_color=BORDER,
                      font=ctk.CTkFont(size=12), command=self._clear_log).pack(side="right")
        self.log_text = ctk.CTkTextbox(pg, font=ctk.CTkFont(family="Consolas", size=12),
                                       fg_color=CARD, state="disabled", wrap="word",
                                       scrollbar_button_color=HOVER,
                                       scrollbar_button_hover_color=BORDER)
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=40, pady=(0, 24))
        self.page_frames["log"] = pg

    # ───────────────── STATUS BAR ───────────────────────────────────────────
    def _statusbar(self):
        sb = ctk.CTkFrame(self, height=30, fg_color=CARD, corner_radius=0)
        sb.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.sb_dot = ctk.CTkLabel(sb, text="●", text_color=DIM, font=ctk.CTkFont(size=11))
        self.sb_dot.pack(side="left", padx=(14, 6), pady=5)
        self.sb_txt = ctk.CTkLabel(sb, text="Ready", font=ctk.CTkFont(size=11), text_color=DIM)
        self.sb_txt.pack(side="left", pady=5)

    # ───────────────── SEARCH ───────────────────────────────────────────────
    def _show_hint(self, text):
        for w in self.results_scroll.winfo_children():
            w.destroy()
        ctk.CTkLabel(self.results_scroll, text=text,
                     font=ctk.CTkFont(size=14), text_color=DIM,
                     justify="center").pack(expand=True, pady=80)

    def _search(self):
        q = self.search_entry.get().strip()
        if not q: return

        if is_url(q):
            self._download_urls([q])
            return

        # Bump generation to invalidate any in-flight thumbnail loads
        self.search_gen += 1
        gen = self.search_gen

        self.search_btn.configure(state="disabled", text="…")
        self.dl_btn.configure(state="disabled")
        self.cancel_btn.configure(state="disabled")

        self._show_hint("🔍  Searching…")

        threading.Thread(target=self._search_worker, args=(q, gen), daemon=True).start()

    def _search_worker(self, query, gen):
        try:
            self._log(f"🔍 Searching: {query}")
            search_query = f"ytsearch25:{query}"
            opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
                    "skip_download": True}

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(search_query, download=False)

            if not info:
                self.after(0, lambda: self._show_results([], gen))
                return

            entries = info.get("entries") or []
            tracks = []
            for e in entries:
                if not e: continue
                if not is_music(e): continue
                vid = e.get("id") or ""
                if not vid:
                    url = e.get("url") or e.get("webpage_url") or ""
                    if "watch?v=" in url: vid = url.split("watch?v=")[-1].split("&")[0]
                    elif "youtu.be/" in url: vid = url.split("youtu.be/")[-1].split("?")[0]
                if not vid: continue

                thumb = e.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
                views = e.get("view_count")
                views_str = f"{views:,} views" if views else ""

                tracks.append(Track(
                    title=e.get("title") or "Unknown",
                    artist=e.get("channel") or e.get("uploader") or "",
                    duration=fmt_dur(e.get("duration")) if e.get("duration") else "—",
                    url=f"https://www.youtube.com/watch?v={vid}",
                    thumbnail=thumb,
                    views=views_str,
                ))

            self._log(f"   ↳ {len(tracks)} results")
            self.after(0, lambda: self._show_results(tracks, gen))

        except Exception as ex:
            self._log(f"❌ Search: {ex}")
            self.after(0, lambda: self._show_results([], gen))

    def _show_results(self, tracks, gen):
        # Always restore search button regardless of outcome
        self.search_btn.configure(state="normal", text="Search")

        # If a newer search started while this one was running, discard
        if gen != self.search_gen:
            return

        self.results = tracks

        for w in self.results_scroll.winfo_children():
            w.destroy()

        if not tracks:
            ctk.CTkLabel(self.results_scroll, text="No results found.",
                         font=ctk.CTkFont(size=14), text_color=DIM).pack(pady=60)
            self.dl_btn.configure(state="disabled")
            return

        for i, t in enumerate(tracks):
            self._result_card(t, i, gen)

        self.dl_btn.configure(state="normal", text=f"⬇  Download All ({len(tracks)})")

    def _result_card(self, t: Track, idx, gen):
        card = ctk.CTkFrame(self.results_scroll, fg_color=CARD, corner_radius=10,
                            border_width=1, border_color=BORDER)
        card.pack(fill="x", pady=3)
        card.grid_columnconfigure(3, weight=1)

        var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(card, text="", variable=var, width=28,
                        fg_color=ACCENT, hover_color=ACCENT2,
                        command=lambda v=var, tr=t: self._toggle(tr, v)
                        ).grid(row=0, column=0, padx=(14, 8), pady=10)

        thumb_lbl = ctk.CTkLabel(card, text="🎵", width=120, height=68,
                                 fg_color=HOVER, corner_radius=6,
                                 font=ctk.CTkFont(size=20))
        thumb_lbl.grid(row=0, column=1, padx=(0, 12), pady=10)

        if t.thumbnail and PIL_OK:
            threading.Thread(target=self._load_thumb,
                             args=(t.thumbnail, thumb_lbl, gen, idx),
                             daemon=True).start()

        info = ctk.CTkFrame(card, fg_color="transparent")
        info.grid(row=0, column=3, sticky="ew", padx=(0, 12), pady=10)
        ctk.CTkLabel(info, text=t.title, anchor="w",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(fill="x")
        meta = t.artist
        if t.duration != "—": meta += f"  ·  {t.duration}"
        if t.views: meta += f"  ·  {t.views}"
        ctk.CTkLabel(info, text=meta, anchor="w",
                     font=ctk.CTkFont(size=12), text_color=DIM).pack(fill="x", pady=(3, 0))

        ctk.CTkButton(card, text="⬇", width=38, height=34,
                      fg_color="transparent", hover_color=ACCENT,
                      text_color=DIM, font=ctk.CTkFont(size=15),
                      command=lambda u=t.url: self._download_urls([u])
                      ).grid(row=0, column=4, padx=(0, 14))

        card.bind("<Enter>", lambda e, c=card: c.configure(fg_color=HOVER))
        card.bind("<Leave>", lambda e, c=card: c.configure(fg_color=CARD))

    def _load_thumb(self, url, label, gen, idx):
        try:
            import hashlib
            cache_key = hashlib.md5(url.encode()).hexdigest()
            cache_path = THUMB_CACHE / f"{cache_key}.jpg"

            if cache_path.exists():
                data = cache_path.read_bytes()
            else:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = resp.read()
                cache_path.write_bytes(data)

            if gen != self.search_gen:
                return

            img = Image.open(io.BytesIO(data))
            img = img.resize((120, 68), Image.LANCZOS)
            ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(120, 68))

            def apply():
                if gen != self.search_gen:
                    return
                try:
                    if label.winfo_exists():
                        label.configure(image=ctk_img, text="", width=120, height=68)
                        label._img_ref = ctk_img
                except Exception:
                    pass

            self.after(0, apply)

        except Exception:
            pass

    def _toggle(self, track, var):
        track.selected = var.get()
        sel = sum(1 for t in self.results if t.selected)
        if sel:
            self.dl_btn.configure(text=f"⬇  Download Selected ({sel})")
        else:
            self.dl_btn.configure(text=f"⬇  Download All ({len(self.results)})")

    # ───────────────── DOWNLOAD ─────────────────────────────────────────────
    def _download_selected(self):
        sel = [t.url for t in self.results if t.selected]
        if not sel: sel = [t.url for t in self.results]
        if sel: self._download_urls(sel)

    def _download_urls(self, urls):
        if not urls: return
        if not self.ff_ok:
            messagebox.showerror("Error", "FFmpeg not found.")
            return
        self.downloading = True
        self.dl_btn.configure(state="disabled", text="⏳ Downloading…")
        self.cancel_btn.configure(state="normal")
        self.cancel_evt.clear()
        self._set_status("Downloading…", AMBER)
        self._log(f"🚀 Downloading {len(urls)} item(s)")
        self.worker = threading.Thread(target=self._dl_worker, args=(urls,), daemon=True)
        self.worker.start()

    def _cancel(self):
        self.cancel_evt.set()
        self._log("🛑 Cancel requested")
        self.cancel_btn.configure(state="disabled")

    def _dl_worker(self, urls):
        out = ensure_dir(Path(self.cfg["output_dir"]))
        fmt = self.fmt_menu.get()
        fi = FORMATS.get(fmt, FORMATS["MP3 · 320 kbps"])
        success = 0
        failed = 0

        for i, url in enumerate(urls, 1):
            if self.cancel_evt.is_set():
                self._log("🛑 Cancelled"); break
            self._set_status(f"[{i}/{len(urls)}] Downloading…", AMBER)
            try:
                self._dl_one(url, out, fi)
                success += 1
                self._log(f"✅ [{i}/{len(urls)}] Done")
            except yt_dlp.utils.DownloadCancelled:
                self._log("🛑 Cancelled"); break
            except Exception as e:
                failed += 1
                self._log(f"❌ [{i}/{len(urls)}] {str(e)[:120]}")

        self._set_status("Ready", DIM)
        self.downloading = False
        self.after(0, lambda: self.dl_btn.configure(state="normal", text="⬇  Download"))
        self.after(0, lambda: self.cancel_btn.configure(state="disabled"))
        self._log(f"🎉 Complete: {success} succeeded, {failed} failed")

    def _dl_one(self, url, out: Path, fi):
        is_playlist = "list=" in url and "watch?v=" not in url.split("list=")[0]

        pp = [{"key": "FFmpegExtractAudio",
               "preferredcodec": fi["codec"], "preferredquality": fi["q"]}]
        if self.v_meta.get():
            pp += [{"key": "FFmpegMetadata", "add_metadata": True},
                   {"key": "EmbedThumbnail"}]

        ppa = {}
        if fi["codec"] == "mp3":
            ppa["extractaudio"] = ["-id3v2_version", "3"]
            ppa["metadata"] = ["-id3v2_version", "3"]
        if self.v_norm.get():
            ppa.setdefault("extractaudio", []).extend(
                ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"])

        def hook(d):
            if self.cancel_evt.is_set(): raise yt_dlp.utils.DownloadCancelled
            if d.get("status") == "downloading":
                pct = d.get("_percent_str", "").strip()
                spd = d.get("_speed_str", "").strip()
                self._set_status(f"⬇ {pct}  {spd}", AMBER)

        if is_playlist:
            outtmpl = str(out / "%(playlist_title,Playlist)s" /
                          "%(playlist_index)02d - %(title)s.%(ext)s")
            noplaylist = False
        else:
            outtmpl = str(out / "%(title)s.%(ext)s")
            noplaylist = True

        opts = {
            "format": "bestaudio/best",
            "noplaylist": noplaylist,
            "socket_timeout": 30,
            "retries": 5,
            "fragment_retries": 5,
            "writethumbnail": self.v_meta.get(),
            "postprocessors": pp,
            "postprocessor_args": ppa,
            "progress_hooks": [hook],
            "logger": YLog(self._log),
            "quiet": False,
            "no_warnings": True,
            "ignoreerrors": True,
            "playlist_items": "1-500",
            "extractor_args": {
                "youtube": {"player_client": ["web", "android", "ios"]}
            },
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/125.0.0.0 Safari/537.36",
            },
            "outtmpl": outtmpl,
        }

        if self.v_skip.get():
            opts["download_archive"] = str(out / ".sg_archive.txt")
        if self.ff_ok and self.ff:
            opts["ffmpeg_location"] = self.ff

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        if info and (self.v_meta.get() or self.v_lyr.get()):
            for ent in (info.get("entries") or [info]):
                if not ent: continue
                try:
                    fn = ydl.prepare_filename(ent)
                    fp = Path(fn).with_suffix(f".{fi['ext']}")
                    if fp.exists():
                        if self.v_meta.get(): fix_cover(fp, self._log)
                        if self.v_lyr.get():
                            embed_lyrics(fp, ent.get("title", ""), self.v_lrc.get(), self._log)
                except Exception: pass

    # ───────────────── HELPERS ──────────────────────────────────────────────
    def _browse(self):
        d = filedialog.askdirectory()
        if d:
            self.cfg["output_dir"] = d
            self.dir_lbl.configure(text=d)
            save_cfg(self.cfg)

    def _reset(self):
        save_cfg(dict(DEFAULTS))
        messagebox.showinfo("Reset", "Settings reset. Restart to apply.")

    def _copy_log(self):
        self.log_text.configure(state="normal")
        c = self.log_text.get("1.0", "end")
        self.log_text.configure(state="disabled")
        self.clipboard_clear(); self.clipboard_append(c)

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _log(self, m):
        ts = datetime.now().strftime("%H:%M:%S")
        self.after(0, lambda: self._ap(f"[{ts}] {m}"))

    def _ap(self, t):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", t + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_status(self, txt, color):
        self.sb_txt.configure(text=txt)
        self.sb_dot.configure(text_color=color)

    def _quit(self):
        self.cancel_evt.set()
        if self.worker and self.worker.is_alive(): self.worker.join(timeout=3)
        self.cfg["geometry"] = self.geometry(); save_cfg(self.cfg)
        self.destroy()


if __name__ == "__main__":
    ff = find_ffmpeg()
    print("=" * 52)
    print(f"  {APP} v{VER}")
    print(f"  FFmpeg: {ff if ff_ok(ff) else 'NOT FOUND'}")
    print(f"  Pillow: {PIL_OK}  lyrics: {LYRICS_OK}  mutagen: {MUTAGEN_OK}")
    print("=" * 52)
    App().mainloop()