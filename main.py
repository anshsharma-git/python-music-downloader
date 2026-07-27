import os
import sys
import shutil
import threading
import concurrent.futures
from pathlib import Path
import customtkinter as ctk
import yt_dlp
from spotdl import Spotdl

# --- LYRICS & METADATA IMPORTS ---
import syncedlyrics
from mutagen.id3 import ID3, USLT, ID3NoHeaderError

# --- VIRTUAL ENVIRONMENT CHECK ---
print("=" * 50)
print(f"🐍 Python Executable: {sys.executable}")
print(f"📦 Inside Virtual Env?: {sys.prefix != sys.base_prefix}")
print("=" * 50)

# Set CustomTkinter Visual Theme
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


def find_ffmpeg_path() -> str | None:
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


def embed_lyrics_to_file(file_path: Path, query_title: str, logger_func=print):
    """Fetch synced/unsynced lyrics and embed them into the MP3 ID3v2.3 tag for MusicBee."""
    if file_path.suffix.lower() != ".mp3":
        return

    try:
        logger_func(f"🎤 Fetching lyrics for: {query_title}...")
        lrc = syncedlyrics.search(query_title)
        if lrc:
            try:
                audio = ID3(file_path)
            except ID3NoHeaderError:
                audio = ID3()

            # Add USLT (Unsynchronized lyrics tag frame readable by MusicBee & phone players)
            audio.add(USLT(encoding=3, lang='eng', desc='', text=lrc))
            audio.save(file_path, v2_version=3)
            logger_func(f"✅ Embedded lyrics into: {file_path.name}")
        else:
            logger_func(f"⚠️ No online lyrics found for: {query_title}")
    except Exception as e:
        logger_func(f"⚠️ Lyrics embedding skipped for {file_path.name}: {e}")


class YTDLPLogger:
    """Redirect yt-dlp outputs directly to GUI Log Textbox."""
    def __init__(self, log_callback):
        self.log = log_callback

    def debug(self, msg):
        if "[download]" in msg or "[ExtractAudio]" in msg or "[Metadata]" in msg:
            self.log(msg)

    def info(self, msg):
        self.log(msg)

    def warning(self, msg):
        self.log(f"⚠️ {msg}")

    def error(self, msg):
        self.log(f"❌ {msg}")


class MusicDownloaderApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        # --- Window Config ---
        self.title("SonicGrabber Pro - Unified Music Client")
        self.geometry("780 x 740")
        self.minsize(700, 650)

        self.default_output = str(Path.home() / "Desktop")
        self.ffmpeg_dir = find_ffmpeg_path()

        self._build_ui()

    def _build_ui(self):
        # 1. Header Title
        title_label = ctk.CTkLabel(
            self, text="🎧 SonicGrabber Pro", font=ctk.CTkFont(size=26, weight="bold")
        )
        title_label.pack(pady=(20, 5))

        subtitle = ctk.CTkLabel(
            self, text="High-Res Downloader for Spotify, YouTube, SoundCloud & More", text_color="gray"
        )
        subtitle.pack(pady=(0, 15))

        # 2. URL Input Section
        input_frame = ctk.CTkFrame(self)
        input_frame.pack(fill="x", padx=20, pady=10)

        url_label = ctk.CTkLabel(input_frame, text="Track or Playlist Link:", font=ctk.CTkFont(weight="bold"))
        url_label.pack(anchor="w", padx=15, pady=(10, 0))

        self.url_entry = ctk.CTkEntry(
            input_frame, placeholder_text="Paste Spotify, YouTube, or SoundCloud URL here...", width=500
        )
        self.url_entry.pack(side="left", fill="x", expand=True, padx=15, pady=10)

        clear_btn = ctk.CTkButton(input_frame, text="Clear", width=60, fg_color="transparent", border_width=1, command=self._clear_url)
        clear_btn.pack(side="right", padx=(0, 15), pady=10)

        # 3. Features & Settings Frame
        settings_frame = ctk.CTkFrame(self)
        settings_frame.pack(fill="x", padx=20, pady=10)

        # Audio Quality / Format
        lbl_format = ctk.CTkLabel(settings_frame, text="Format Quality:", font=ctk.CTkFont(weight="bold"))
        lbl_format.grid(row=0, column=0, padx=15, pady=10, sticky="w")

        self.format_menu = ctk.CTkOptionMenu(
            settings_frame, values=["MP3 (320 kbps)", "FLAC (Lossless)", "M4A (AAC)"]
        )
        self.format_menu.grid(row=0, column=1, padx=10, pady=10)

        # Parallel Threads
        lbl_threads = ctk.CTkLabel(settings_frame, text="Parallel Threads:", font=ctk.CTkFont(weight="bold"))
        lbl_threads.grid(row=0, column=2, padx=15, pady=10, sticky="w")

        self.threads_menu = ctk.CTkOptionMenu(
            settings_frame, values=["1 Stream", "3 Parallel Streams", "5 Parallel Streams"]
        )
        self.threads_menu.grid(row=0, column=3, padx=10, pady=10)

        # Checkboxes: Metadata, Lyrics & Normalization
        self.meta_var = ctk.BooleanVar(value=True)
        self.meta_check = ctk.CTkCheckBox(settings_frame, text="Embed Cover Art & Tags", variable=self.meta_var)
        self.meta_check.grid(row=1, column=0, padx=15, pady=10, sticky="w")

        self.lyrics_var = ctk.BooleanVar(value=True)
        self.lyrics_check = ctk.CTkCheckBox(settings_frame, text="Fetch & Embed Lyrics", variable=self.lyrics_var)
        self.lyrics_check.grid(row=1, column=1, padx=15, pady=10, sticky="w")

        self.norm_var = ctk.BooleanVar(value=False)
        self.norm_check = ctk.CTkCheckBox(settings_frame, text="Volume Normalization", variable=self.norm_var)
        self.norm_check.grid(row=1, column=2, columnspan=2, padx=15, pady=10, sticky="w")

        # Destination Folder
        folder_frame = ctk.CTkFrame(self)
        folder_frame.pack(fill="x", padx=20, pady=10)

        folder_label = ctk.CTkLabel(folder_frame, text="Save Folder:", font=ctk.CTkFont(weight="bold"))
        folder_label.pack(side="left", padx=15, pady=10)

        self.folder_entry = ctk.CTkEntry(folder_frame)
        self.folder_entry.insert(0, self.default_output)
        self.folder_entry.pack(side="left", fill="x", expand=True, padx=10, pady=10)

        browse_btn = ctk.CTkButton(folder_frame, text="Browse", width=80, command=self._browse_folder)
        browse_btn.pack(side="right", padx=15, pady=10)

        # 4. Main Action Button & Progress Indicator
        self.download_btn = ctk.CTkButton(
            self, text="⚡ START DOWNLOAD", font=ctk.CTkFont(size=16, weight="bold"), height=45, command=self._start_download
        )
        self.download_btn.pack(fill="x", padx=20, pady=(10, 5))

        self.progress_bar = ctk.CTkProgressBar(self)
        self.progress_bar.pack(fill="x", padx=20, pady=5)
        self.progress_bar.set(0)

        # 5. Live Console Output Log
        log_label = ctk.CTkLabel(self, text="Activity Console Log:", font=ctk.CTkFont(weight="bold"))
        log_label.pack(anchor="w", padx=20, pady=(10, 0))

        self.log_textbox = ctk.CTkTextbox(self, height=180, font=ctk.CTkFont(family="Consolas", size=12))
        self.log_textbox.pack(fill="both", expand=True, padx=20, pady=(5, 20))

    # --- UI Helpers ---
    def _clear_url(self):
        self.url_entry.delete(0, "end")

    def _browse_folder(self):
        folder = ctk.filedialog.askdirectory()
        if folder:
            self.folder_entry.delete(0, "end")
            self.folder_entry.insert(0, folder)

    def log(self, message: str):
        """Thread-safe logging helper."""
        self.log_textbox.insert("end", message + "\n")
        self.log_textbox.see("end")

    # --- Core Download Router ---
    def _start_download(self):
        url = self.url_entry.get().strip()
        if not url:
            self.log("⚠️ Please paste a valid URL before downloading.")
            return

        self.download_btn.configure(state="disabled", text="⏳ Downloading...")
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start()

        threading.Thread(target=self._download_worker, args=(url,), daemon=True).start()

    def _download_worker(self, url: str):
        output_dir = Path(self.folder_entry.get().strip())
        selected_fmt = self.format_menu.get()
        threads_count = int(self.threads_menu.get().split()[0])

        self.log(f"\n🚀 Starting download session for: {url}")
        self.log(f"📁 Output Directory: {output_dir}")

        try:
            if "spotify.com" in url:
                self._process_spotify(url, output_dir, selected_fmt, threads_count)
            else:
                self._process_yt_dlp(url, output_dir, selected_fmt, threads_count)

            self.log("🎉 Download session complete!\n" + ("=" * 50))
        except Exception as e:
            self.log(f"❌ Error during execution: {e}")
        finally:
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate")
            self.progress_bar.set(1.0)
            self.download_btn.configure(state="normal", text="⚡ START DOWNLOAD")

    # --- Spotify Processing Engine ---
    def _process_spotify(self, url: str, output_dir: Path, fmt: str, threads: int):
        self.log("🟢 Platform: Spotify detected (Querying metadata & matching audio streams...)")
        
        spotdl_client = Spotdl(
            downsampling=False,
            headful=False,
            max_workers=threads
        )

        songs = spotdl_client.download_songs([url])
        self.log(f"✅ Downloaded {len(songs)} track(s) from Spotify!")

    # --- General (YouTube / SoundCloud) Engine ---
    def _process_yt_dlp(self, url: str, output_dir: Path, fmt: str, threads: int):
        self.log("🔴 Platform: YouTube / Web source detected.")

        codec = "mp3"
        quality = "320"
        if "FLAC" in fmt:
            codec = "flac"
            quality = "0"
        elif "M4A" in fmt:
            codec = "m4a"
            quality = "256"

        postprocessors = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': codec,
            'preferredquality': quality,
        }]

        if self.meta_var.get():
            postprocessors.append({'key': 'FFmpegMetadata'})
            postprocessors.append({'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'})
            postprocessors.append({'key': 'EmbedThumbnail'})

        # Build FFmpeg Postprocessor Arguments (Forces ID3v2.3 for MusicBee Compatibility!)
        ffmpeg_args = ['-id3v2_version', '3']
        if self.norm_var.get():
            ffmpeg_args.extend(['-af', 'loudnorm=I=-16:TP=-1.5:LRA=11'])

        ydl_opts = {
            'format': 'bestaudio/best',
            'noplaylist': False,
            'download_archive': str(output_dir / 'download_archive.txt'),
            'socket_timeout': 30,
            'retries': 10,
            'writethumbnails': self.meta_var.get(),
            'postprocessors': postprocessors,
            'postprocessor_args': {'ffmpeg': ffmpeg_args},
            'logger': YTDLPLogger(self.log),
            'quiet': False,
            'no_warnings': True,
        }

        if self.ffmpeg_dir:
            ydl_opts['ffmpeg_location'] = self.ffmpeg_dir

        # Helper to process single song lyrics after download
        def post_process_file(file_path: Path, title_hint: str):
            if self.lyrics_var.get() and file_path.exists():
                embed_lyrics_to_file(file_path, title_hint, self.log)

        # Multithreaded Execution
        if threads > 1:
            self.log(f"⚡ True Multithreading Enabled: {threads} workers active.")
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                self.log("🔍 Extracting playlist info...")
                info = ydl.extract_info(url, download=False)
                
                if 'entries' in info:
                    entries = list(info['entries'])
                    playlist_name = info.get('title', 'Music')
                    playlist_folder = output_dir / playlist_name
                    playlist_folder.mkdir(parents=True, exist_ok=True)
                    self.log(f"📋 Found Playlist: '{playlist_name}' with {len(entries)} tracks.")

                    def download_track(entry_data):
                        idx, entry = entry_data
                        if not entry: return
                        track_url = entry.get('webpage_url') or entry.get('url')
                        title = entry.get('title', f'Track_{idx}')
                        if not track_url: return

                        track_opts = ydl_opts.copy()
                        file_stem = f"{idx:02d} - {title}"
                        track_opts['outtmpl'] = str(playlist_folder / f"{file_stem}.%(ext)s")
                        
                        try:
                            with yt_dlp.YoutubeDL(track_opts) as worker_ydl:
                                worker_ydl.download([track_url])
                            
                            # Embed Lyrics
                            expected_file = playlist_folder / f"{file_stem}.{codec}"
                            post_process_file(expected_file, title)

                        except Exception as e:
                            self.log(f"⚠️ Error on track {idx}: {e}")

                    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
                        executor.map(download_track, enumerate(entries, start=1))
                    return

        # Sequential Execution
        ydl_opts['outtmpl'] = str(output_dir / '%(playlist_title,Music)s/%(playlist_index&{} - |)s%(title)s.%(ext)s')
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info and self.lyrics_var.get():
                title = info.get('title', '')
                filename = ydl.prepare_filename(info)
                file_path = Path(filename).with_suffix(f".{codec}")
                post_process_file(file_path, title)


if __name__ == "__main__":
    app = MusicDownloaderApp()
    app.mainloop()