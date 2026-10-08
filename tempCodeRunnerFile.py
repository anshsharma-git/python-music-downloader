# required modules
import os
import re
import sys
import json
import shutil
import subprocess
import threading
import concurrent.futures
from pathlib import Path
from datetime import datetime
from typing import Optional, List

import customtkinter as ctk
from tkinter import filedialog
import yt_dlp

try:
    from spotdl import Spotdl
    SPOTDL_AVAILABLE = True
except Exception:
    SPOTDL_AVAILABLE = False

try:
    import syncedlyrics
    SYNCEDLYRICS_AVAILABLE = True
except Exception:
    SYNCEDLYRICS_AVAILABLE = False

from mutagen.id3 import ID3, USLT, ID3NoHeaderError

# --------------------------------------------------------------------------
# CONFIG PERSISTENCE
# --------------------------------------------------------------------------
CONFIG_PATH = Path.home() / ".sonicgrabber_config.json"
DEFAULT_CONFIG = {
    "output_dir": str(Path.home() / "Desktop"),
    "format": "MP3 (320 kbps)",
    "threads": "1 Stream",
    "embed_meta": True,
    "embed_lyrics": True,
    "save_lrc": False,
    "normalize": False,
    "skip_existing": True,
    "appearance": "Dark",
}

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            merged = DEFAULT_CONFIG.copy()
            merged.update(data)
            return merged
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass

ctk.set_appearance_mode(load_config().get("appearance", "Dark"))
ctk.set_default_color_theme("blue")

def sanitize_filename(name: str) -> str:
    """Strip characters that are illegal in file names on Windows/Mac/Linux."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    return name.strip()[:150] or "Untitled"

def find_ffmpeg_path() -> Optional[str]:
    """Locate FFmpeg executable directory automatically."""
    ffmpeg_in_path = shutil.which("ffmpeg")
    if ffmpeg_in_path and shutil.which("ffprobe"):
        return str(Path(ffmpeg_in_path).parent)

    user_home = Path.home()
    winget_packages = user_home / "AppData/Local/Microsoft/WinGet/Packages"
    if winget_packages.exists():
        matches = list(winget_packages.rglob("ffprobe.exe"))
        if matches:
            return str(matches[0].parent)

    links_path = user_home / "AppData/Local/Microsoft/WinGet/Links"
    if (links_path / "ffmpeg.exe").exists():
        return str(links_path)

    return None

def embed_lyrics_to_file(file_path: Path, query_title: str, save_lrc: bool, logger_func=print):
    """Fetch synced/unsynced lyrics and embed them into MP3 ID3v2.3 tags."""
    if not SYNCEDLYRICS_AVAILABLE:
        logger_func("⚠️ syncedlyrics not installed, skipping lyrics.")
        return

    try:
        logger_func(f"🎤 Fetching lyrics for: {query_title}...")
        lrc = syncedlyrics.search(query_title)
        if not lrc:
            logger_func(f"⚠️ No online lyrics found for: {query_title}")
            return

        if save_lrc:
            lrc_path = file_path.with_suffix(".lrc")
            lrc_path.write_text(lrc, encoding="utf-8")
            logger_func(f"📝 Saved lyrics file: {lrc_path.name}")

        if file_path.suffix.lower() == ".mp3":
            try:
                audio = ID3(file_path)
            except ID3NoHeaderError:
                audio = ID3()
            audio.add(USLT(encoding=3, lang="eng", desc="", text=lrc))
            audio.save(file_path, v2_version=3)
            logger_func(f"✅ Embedded lyrics into: {file_path.name}")
    except Exception as e:
        logger_func(f"⚠️ Lyrics embedding skipped for {file_path.name}: {e}")

def fix_cover_art_tag(file_path: Path, logger_func=print):
    """Verify and normalize the embedded cover art so Windows Media Player and
    MusicBee actually display it."""
    if file_path.suffix.lower() != ".mp3" or not file_path.exists():
        return
    try:
        audio = ID3(file_path)
    except ID3NoHeaderError:
        logger_func(f"⚠️ {file_path.name}: no ID3 tag present, nothing to fix.")
        return
    except Exception as e:
        logger_func(f"⚠️ {file_path.name}: could not read tags ({e}).")
        return

    apic_frames = audio.getall("APIC")
    if not apic_frames:
        logger_func(f"⚠️ {file_path.name}: no cover art embedded to fix.")
        return

    # Keep only the largest picture (dedupe) and force it to front-cover type
    best = max(apic_frames, key=lambda f: len(f.data))
    audio.delall("APIC")
    best.type = 3  # "Cover (front)" — WMP/MusicBee filter on this
    best.desc = "Cover"
    
    # Safely detect MIME type instead of blindly forcing JPEG
    if not best.mime or "/" not in best.mime:
        if best.data.startswith(b'\xff\xd8\xff'):
            best.mime = "image/jpeg"
        elif best.data.startswith(b'\x89PNG'):
            best.mime = "image/png"
        else:
            best.mime = "image/jpeg" # fallback
            
    audio.add(best)
    audio.save(file_path, v2_version=3)
    logger_func(f"🖼️ Verified cover art (ID3v2.3, front cover) for: {file_path.name}")

class YTDLPLogger:
    """Redirect yt-dlp outputs directly to GUI Log Textbox."""
    def __init__(self, log_callback):
        self.log = log_callback

    def debug(self, msg):
        if "[download]" in msg or "[ExtractAudio]" in msg or "[Metadata]" in msg or "[EmbedThumbnail]" in msg:
            self.log(msg)

    def info(self, msg):
        self.log(msg)

    def warning(self, msg):
        self.log(f"⚠️ {msg}")

    def error(self, msg):
        self.log(f"❌ {msg}")

class CancelledError(Exception):
    pass

class MusicDownloaderApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.config_data = load_config()

        self.title("SonicGrabber Pro - Unified Music Client")
        self.geometry("820x780")
        self.minsize(760, 700)

        self.ffmpeg_dir = find_ffmpeg_path()
        self.cancel_event = threading.Event()
        self.last_output_dir = Path(self.config_data["output_dir"])

        self.completed_count = 0
        self.failed_count = 0
        self.total_count = 0

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        # Header
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.pack(fill="x", padx=20, pady=(15, 5))

        title_label = ctk.CTkLabel(
            header_frame, text="🎧 SonicGrabber Pro", font=ctk.CTkFont(size=26, weight="bold")
        )
        title_label.pack(side="left")

        self.theme_switch = ctk.CTkSegmentedButton(
            header_frame, values=["Dark", "Light"], command=self._change_theme
        )
        self.theme_switch.set(self.config_data.get("appearance", "Dark"))
        self.theme_switch.pack(side="right", pady=5)

        subtitle = ctk.CTkLabel(
            self, text="High-Res Downloader for Spotify, YouTube, SoundCloud & More", text_color="gray"
        )
        subtitle.pack(pady=(0, 10))

        if not self.ffmpeg_dir:
            warn = ctk.CTkLabel(
                self, text="⚠️ FFmpeg not found on this system — audio conversion will fail. Install FFmpeg first.",
                text_color="#e0a030"
            )
            warn.pack(pady=(0, 5))

        # URL Input Section
        input_frame = ctk.CTkFrame(self)
        input_frame.pack(fill="x", padx=20, pady=8)

        url_label = ctk.CTkLabel(
            input_frame, text="Track / Playlist Link(s) — one per line for batch downloads:",
            font=ctk.CTkFont(weight="bold")
        )
        url_label.pack(anchor="w", padx=15, pady=(10, 4))

        self.url_box = ctk.CTkTextbox(input_frame, height=70)
        self.url_box.pack(fill="x", padx=15, pady=(0, 10))

        btn_row = ctk.CTkFrame(input_frame, fg_color="transparent")
        btn_row.pack(fill="x", padx=15, pady=(0, 10))
        ctk.CTkButton(btn_row, text="Paste", width=70, command=self._paste_clipboard).pack(side="left")
        ctk.CTkButton(btn_row, text="Clear", width=70, fg_color="transparent", border_width=1,
                      command=lambda: self.url_box.delete("1.0", "end")).pack(side="left", padx=6)

        # Settings Frame
        settings_frame = ctk.CTkFrame(self)
        settings_frame.pack(fill="x", padx=20, pady=8)

        ctk.CTkLabel(settings_frame, text="Format Quality:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=15, pady=10, sticky="w")
        self.format_menu = ctk.CTkOptionMenu(
            settings_frame, values=["MP3 (320 kbps)", "FLAC (Lossless)", "M4A (AAC)", "Opus (Best)"]
        )
        self.format_menu.set(self.config_data.get("format", "MP3 (320 kbps)"))
        self.format_menu.grid(row=0, column=1, padx=10, pady=10)

        ctk.CTkLabel(settings_frame, text="Parallel Threads:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=2, padx=15, pady=10, sticky="w")
        self.threads_menu = ctk.CTkOptionMenu(
            settings_frame, values=["1 Stream", "3 Parallel Streams", "5 Parallel Streams"]
        )
        self.threads_menu.set(self.config_data.get("threads", "1 Stream"))
        self.threads_menu.grid(row=0, column=3, padx=10, pady=10)

        self.meta_var = ctk.BooleanVar(value=self.config_data.get("embed_meta", True))
        ctk.CTkCheckBox(settings_frame, text="Embed Cover Art & Tags", variable=self.meta_var).grid(
            row=1, column=0, padx=15, pady=8, sticky="w")

        self.lyrics_var = ctk.BooleanVar(value=self.config_data.get("embed_lyrics", True))
        ctk.CTkCheckBox(settings_frame, text="Fetch & Embed Lyrics", variable=self.lyrics_var).grid(
            row=1, column=1, padx=15, pady=8, sticky="w")

        self.lrc_var = ctk.BooleanVar(value=self.config_data.get("save_lrc", False))
        ctk.CTkCheckBox(settings_frame, text="Also Save .lrc File", variable=self.lrc_var).grid(
            row=1, column=2, padx=15, pady=8, sticky="w")

        self.norm_var = ctk.BooleanVar(value=self.config_data.get("normalize", False))
        ctk.CTkCheckBox(settings_frame, text="Volume Normalization", variable=self.norm_var).grid(
            row=1, column=3, padx=15, pady=8, sticky="w")

        self.skip_var = ctk.BooleanVar(value=self.config_data.get("skip_existing", True))
        ctk.CTkCheckBox(settings_frame, text="Skip Already Downloaded Files", variable=self.skip_var).grid(
            row=2, column=0, columnspan=2, padx=15, pady=(0, 10), sticky="w")

        # Destination Folder
        folder_frame = ctk.CTkFrame(self)
        folder_frame.pack(fill="x", padx=20, pady=8)

        ctk.CTkLabel(folder_frame, text="Save Folder:", font=ctk.CTkFont(weight="bold")).pack(
            side="left", padx=15, pady=10)

        self.folder_entry = ctk.CTkEntry(folder_frame)
        self.folder_entry.insert(0, str(self.last_output_dir))
        self.folder_entry.pack(side="left", fill="x", expand=True, padx=10, pady=10)

        ctk.CTkButton(folder_frame, text="Browse", width=80, command=self._browse_folder).pack(
            side="right", padx=(0, 15), pady=10)
        ctk.CTkButton(folder_frame, text="Open", width=70, fg_color="transparent", border_width=1,
                      command=self._open_output_folder).pack(side="right", padx=(0, 6), pady=10)

        # Action Buttons Row
        action_frame = ctk.CTkFrame(self, fg_color="transparent")
        action_frame.pack(fill="x", padx=20, pady=(10, 5))

        self.download_btn = ctk.CTkButton(
            action_frame, text="⚡ START DOWNLOAD", font=ctk.CTkFont(size=16, weight="bold"),
            height=45, command=self._start_download
        )
        self.download_btn.pack(side="left", fill="x", expand=True)

        self.cancel_btn = ctk.CTkButton(
            action_frame, text="✖ Cancel", height=45, width=110, fg_color="#a83232",
            hover_color="#832525", state="disabled", command=self._cancel_download
        )
        self.cancel_btn.pack(side="left", padx=(10, 0))

        # Progress
        progress_frame = ctk.CTkFrame(self, fg_color="transparent")
        progress_frame.pack(fill="x", padx=20, pady=(5, 0))

        self.status_label = ctk.CTkLabel(progress_frame, text="Idle", anchor="w")
        self.status_label.pack(fill="x")

        self.progress_bar = ctk.CTkProgressBar(progress_frame)
        self.progress_bar.pack(fill="x", pady=5)
        self.progress_bar.set(0)

        self.counter_label = ctk.CTkLabel(progress_frame, text="Completed: 0   Failed: 0   Total: 0",
                                           text_color="gray")
        self.counter_label.pack(anchor="w")

        # Log
        log_header = ctk.CTkFrame(self, fg_color="transparent")
        log_header.pack(fill="x", padx=20, pady=(10, 0))
        ctk.CTkLabel(log_header, text="Activity Console Log:", font=ctk.CTkFont(weight="bold")).pack(side="left")
        ctk.CTkButton(log_header, text="Clear Log", width=80, fg_color="transparent", border_width=1,
                      command=self._clear_log).pack(side="right")

        self.log_textbox = ctk.CTkTextbox(self, height=170, font=ctk.CTkFont(family="Consolas", size=12))
        self.log_textbox.pack(fill="both", expand=True, padx=20, pady=(5, 20))
        
        # Make log read-only to prevent accidental typing
        self.log_textbox.configure(state="disabled")

    # ------------------------------------------------------------------
    # UI HELPERS
    # ------------------------------------------------------------------
    def _change_theme(self, value):
        ctk.set_appearance_mode(value)
        self.config_data["appearance"] = value
        save_config(self.config_data)

    def _paste_clipboard(self):
        try:
            clip = self.clipboard_get()
            self.url_box.insert("end", clip)
        except Exception:
            pass

    def _browse_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.folder_entry.delete(0, "end")
            self.folder_entry.insert(0, folder)

    def _open_output_folder(self):
        path = Path(self.folder_entry.get().strip() or self.last_output_dir)
        path.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except Exception as e:
            self.log(f"⚠️ Could not open folder: {e}")

    def log(self, message: str):
        """Thread-safe logging helper."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.after(0, lambda: self._append_log(f"[{timestamp}] {message}"))

    def _append_log(self, message: str):
        self.log_textbox.configure(state="normal")
        self.log_textbox.insert("end", message + "\n")
        self.log_textbox.see("end")
        self.log_textbox.configure(state="disabled")

    def _clear_log(self):
        self.log_textbox.configure(state="normal")
        self.log_textbox.delete("1.0", "end")
        self.log_textbox.configure(state="disabled")

    def _set_status(self, text: str):
        self.after(0, lambda: self.status_label.configure(text=text))

    def _set_progress(self, value: float):
        self.after(0, lambda: self.progress_bar.set(value))

    def _update_counters(self):
        self.after(0, lambda: self.counter_label.configure(
            text=f"Completed: {self.completed_count}   Failed: {self.failed_count}   Total: {self.total_count}"
        ))

    def _on_close(self):
        self.cancel_event.set()
        self.destroy()

    # ------------------------------------------------------------------
    # DOWNLOAD ORCHESTRATION
    # ------------------------------------------------------------------
    def _start_download(self):
        raw_text = self.url_box.get("1.0", "end").strip()
        urls = [u.strip() for u in raw_text.splitlines() if u.strip()]
        if not urls:
            self.log("⚠️ Please paste at least one valid URL before downloading.")
            return

        # Safely extract UI variables on the main thread to prevent Tkinter threading crashes
        output_dir_str = self.folder_entry.get().strip()
        selected_fmt = self.format_menu.get()
        threads_str = self.threads_menu.get()
        embed_meta = self.meta_var.get()
        lyrics_var = self.lyrics_var.get()
        save_lrc = self.lrc_var.get()
        norm_var = self.norm_var.get()
        skip_existing = self.skip_var.get()

        self.config_data.update({
            "output_dir": output_dir_str,
            "format": selected_fmt,
            "threads": threads_str,
            "embed_meta": embed_meta,
            "embed_lyrics": lyrics_var,
            "save_lrc": save_lrc,
            "normalize": norm_var,
            "skip_existing": skip_existing,
        })
        save_config(self.config_data)

        self.cancel_event.clear()
        self.completed_count = 0
        self.failed_count = 0
        self.total_count = len(urls)
        self._update_counters()

        self.download_btn.configure(state="disabled", text="⏳ Downloading...")
        self.cancel_btn.configure(state="normal")
        self.progress_bar.set(0)

        threading.Thread(
            target=self._download_worker, 
            args=(urls, output_dir_str, selected_fmt, threads_str, embed_meta, lyrics_var, save_lrc, norm_var, skip_existing), 
            daemon=True
        ).start()

    def _cancel_download(self):
        self.cancel_event.set()
        self.log("🛑 Cancel requested — stopping after the current file...")
        self.cancel_btn.configure(state="disabled")

    def _download_worker(self, urls: List[str], output_dir_str: str, selected_fmt: str, 
                         threads_str: str, embed_meta: bool, lyrics_var: bool, 
                         save_lrc: bool, norm_var: bool, skip_existing: bool):
        
        output_dir = Path(output_dir_str or self.last_output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        threads_count = int(threads_str.split()[0])

        self.log(f"🚀 Starting session with {len(urls)} link(s)")
        self.log(f"📁 Output Directory: {output_dir}")

        for i, url in enumerate(urls, start=1):
            if self.cancel_event.is_set():
                self.log("🛑 Session cancelled by user.")
                break

            self._set_status(f"Processing link {i}/{len(urls)}")
            try:
                if "spotify.com" in url:
                    self._process_spotify(url, output_dir, selected_fmt, threads_count, save_lrc)
                else:
                    self._process_yt_dlp(url, output_dir, selected_fmt, threads_count, embed_meta, lyrics_var, save_lrc, norm_var, skip_existing)
                self.completed_count += 1
            except CancelledError:
                self.log("🛑 Session cancelled by user.")
                break
            except Exception as e:
                self.failed_count += 1
                self.log(f"❌ Failed to process {url}: {e}")

            self._update_counters()
            self._set_progress(i / len(urls))

        self.log("🎉 Session finished!\n" + ("=" * 50))
        self._set_status("Idle")
        self.after(0, self._reset_buttons)

    def _reset_buttons(self):
        self.download_btn.configure(state="normal", text="⚡ START DOWNLOAD")
        self.cancel_btn.configure(state="disabled")

    # ------------------------------------------------------------------
    # SPOTIFY ENGINE
    # ------------------------------------------------------------------
    def _process_spotify(self, url: str, output_dir: Path, fmt: str, threads: int, save_lrc: bool):
        if not SPOTDL_AVAILABLE:
            self.log("❌ spotdl is not installed — cannot process Spotify links.")
            return

        self.log("🟢 Platform: Spotify detected (metadata, cover art & lyrics are handled natively by spotdl).")

        try:
            # spotdl v4+ API wrapper
            spotdl_client = Spotdl(
                client_id="5f573c95c0e1422390450877988d0509", 
                client_secret="39570878b52945058c4571950887988d", 
                downsampling=False,
                headless=True,
                max_workers=threads,
            )
            
            try:
                spotdl_client.downloader.settings["output"] = str(output_dir / "{artists} - {title}.{output-ext}")
                spotdl_client.downloader.settings["format"] = fmt.split()[0].lower()
            except Exception:
                pass  # older/newer spotdl versions expose settings differently; safe to skip

            songs = spotdl_client.download_songs([url])
            self.log(f"✅ Downloaded {len(songs)} track(s) from Spotify (cover art & tags embedded automatically).")
        except Exception as e:
            self.log(f"⚠️ spotdl failed or API changed: {e}.")

    # ------------------------------------------------------------------
    # YT-DLP ENGINE (YouTube / SoundCloud / everything else)
    # ------------------------------------------------------------------
    def _process_yt_dlp(self, url: str, output_dir: Path, fmt: str, threads: int, 
                        embed_meta: bool, lyrics_var: bool, save_lrc: bool, 
                        norm_var: bool, skip_existing: bool):
        self.log("🔴 Platform: YouTube / Web source detected.")

        codec_settings = {
            "MP3 (320 kbps)": ("mp3", "320"),
            "FLAC (Lossless)": ("flac", "0"),
            "M4A (AAC)": ("m4a", "256"),
            "Opus (Best)": ("opus", "0"),
        }
        codec, quality = codec_settings.get(fmt, ("mp3", "320"))

        postprocessors = [
            {"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": quality},
        ]
        if embed_meta:
            postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})
            postprocessors.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
            postprocessors.append({"key": "EmbedThumbnail"})

        postprocessor_args = {}
        if codec == "mp3":
            postprocessor_args["extractaudio"] = ["-id3v2_version", "3"]
            postprocessor_args["metadata"] = ["-id3v2_version", "3"]
        if norm_var:
            postprocessor_args.setdefault("extractaudio", []).extend(["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"])

        def progress_hook(d):
            if self.cancel_event.is_set():
                raise CancelledError("Cancelled by user")
            if d.get("status") == "downloading":
                pct = d.get("_percent_str", "").strip()
                speed = d.get("_speed_str", "").strip()
                self._set_status(f"Downloading: {d.get('filename', '')} ({pct} @ {speed})")

        ydl_opts = {
            "format": "bestaudio/best",
            "noplaylist": False,
            "download_archive": str(output_dir / "download_archive.txt") if skip_existing else None,
            "socket_timeout": 30,
            "retries": 10,
            "writethumbnail": embed_meta,
            "postprocessors": postprocessors,
            "postprocessor_args": postprocessor_args,
            "progress_hooks": [progress_hook],
            "logger": YTDLPLogger(self.log),
            "quiet": False,
            "no_warnings": True,
            "restrictfilenames": False,
        }
        if not skip_existing:
            ydl_opts.pop("download_archive", None)

        if self.ffmpeg_dir:
            ydl_opts["ffmpeg_location"] = self.ffmpeg_dir

        def post_process_file(file_path: Path, title_hint: str):
            if embed_meta and file_path.exists():
                fix_cover_art_tag(file_path, self.log)
            if lyrics_var and file_path.exists():
                embed_lyrics_to_file(file_path, title_hint, save_lrc, self.log)

        # --- Multithreaded playlist download ---
        if threads > 1:
            self.log(f"⚡ Multithreading enabled: {threads} workers active.")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                self.log("🔍 Extracting playlist info...")
                info = ydl.extract_info(url, download=False)

                if info and "entries" in info and info["entries"]:
                    entries = [e for e in info["entries"] if e]
                    playlist_name = sanitize_filename(info.get("title", "Music"))
                    playlist_folder = output_dir / playlist_name
                    playlist_folder.mkdir(parents=True, exist_ok=True)
                    self.log(f"📋 Found Playlist: '{playlist_name}' with {len(entries)} tracks.")

                    def download_track(entry_data):
                        idx, entry = entry_data
                        if self.cancel_event.is_set():
                            return
                        track_url = entry.get("webpage_url") or entry.get("url")
                        title = sanitize_filename(entry.get("title", f"Track_{idx}"))
                        if not track_url:
                            return

                        track_opts = dict(ydl_opts)
                        file_stem = f"{idx:02d} - {title}"
                        track_opts["outtmpl"] = str(playlist_folder / f"{file_stem}.%(ext)s")

                        try:
                            with yt_dlp.YoutubeDL(track_opts) as worker_ydl:
                                worker_ydl.download([track_url])
                            expected_file = playlist_folder / f"{file_stem}.{codec}"
                            post_process_file(expected_file, title)
                        except CancelledError:
                            pass
                        except Exception as e:
                            self.log(f"⚠️ Error on track {idx}: {e}")

                    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
                        list(executor.map(download_track, enumerate(entries, start=1)))
                    return

        # --- Sequential single-track / playlist download ---
        ydl_opts["outtmpl"] = str(
            output_dir / "%(playlist_title,Music)s/%(playlist_index&{} - |)s%(title)s.%(ext)s"
        )
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # Fix for processing all files in single-thread mode (not just the last one)
            if info and (lyrics_var or embed_meta):
                if "entries" in info and info["entries"]:
                    for entry in info["entries"]:
                        if entry:
                            title = entry.get("title", "")
                            filename = ydl.prepare_filename(entry)
                            file_path = Path(filename).with_suffix(f".{codec}")
                            if file_path.exists():
                                post_process_file(file_path, title)
                else:
                    title = info.get("title", "")
                    filename = ydl.prepare_filename(info)
                    file_path = Path(filename).with_suffix(f".{codec}")
                    if file_path.exists():
                        post_process_file(file_path, title)

if __name__ == "__main__":
    print("=" * 50)
    print(f"🐍 Python Executable: {sys.executable}")
    print(f"📦 Inside Virtual Env?: {sys.prefix != sys.base_prefix}")
    print(f"🎬 FFmpeg found: {find_ffmpeg_path() or 'NOT FOUND — please install FFmpeg'}")
    print(f"🎵 spotdl available: {SPOTDL_AVAILABLE}")
    print(f"🎤 syncedlyrics available: {SYNCEDLYRICS_AVAILABLE}")
    print("=" * 50)

    app = MusicDownloaderApp()
    app.mainloop()