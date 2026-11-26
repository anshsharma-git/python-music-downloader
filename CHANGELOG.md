# Changelog

## 6.1.0 — complete redesign (first Linux release)

**New interface**
- Sidebar navigation: Search · Queue · Library · Settings · Activity log, with keyboard shortcuts (`Ctrl+1…5`, `Ctrl+F`, `Ctrl+A`, `Esc`).
- Dark / light / system theme and an interface-scale setting for HiDPI screens.
- Toast notifications, a live status bar (overall progress and speed) and optional desktop notifications.
- Search results show thumbnails; click a card to select it, or use ▶ to preview a track before downloading.

**Downloading**
- Real download queue: per-song progress, speed and ETA; pause, cancel, retry, move to front, up to 5 parallel downloads.
- The queue survives restarts (unfinished downloads are restored).
- Paste a track or playlist link to browse and pick tracks; command-line mode (`python3 main.py -d "link"`).
- Automatic retry that tries different YouTube approaches, with the reason for a failure written to the Activity log.
- One-click **Fix YouTube downloads**: installs the Deno JavaScript runtime that YouTube now requires.
- Optional browser cookies (read once, in the background, with a timeout), speed limit and SponsorBlock trimming.

**Tags & library**
- Cover art (square-cropped), title, artist, year and lyrics written with mutagen for MP3 (ID3v2.3), FLAC, M4A and Opus.
- Optional MusicBrainz lookup for album, track number and official artwork; "Artist / Album" folder layout.
- Library with history, search, sorting, duplicate skipping and M3U export.

**Linux**
- Standalone executable (PyInstaller), desktop launcher installer, XDG config/data paths, `--selftest` command.

## 5.3.0
- Original single-window version: search, thumbnails, metadata, lyrics, playlist sync.
