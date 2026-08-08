# Chaturbate Stream Monitor & Downloader (GUI Tool)

A Python + PySide6 application for monitoring live streams, capturing previews, and downloading active streams using `yt-dlp` and `ffmpeg`.

The tool tracks multiple stream URLs, detects live status, shows real-time previews, and can automatically download active streams.

[![Latest Release](https://img.shields.io/github/v/release/Tazir709/stream-monitor)](https://github.com/Tazir709/stream-monitor/releases/latest)

---
### Tested on:
- Windows 10 / 11
- Fedora Linux (recent versions)
---

## ✨ Features

- Live stream status checker (yt-dlp based)
- Real-time status updates (online / offline / private / away / error)
- Automatic preview capture using ffmpeg
- Stream downloading via yt-dlp
- Download progress tracking
- Rate limiting to avoid blocking
- Multi-threaded workers (GUI stays responsive)
- Auto-retry on status changes (offline → online)
- Settings dialog for download output, cookies, User-Agent, and video quality — all persisted across restarts

---
## 📸 Preview

The application showing multiple stream states:

![Dashboard Preview](app.png)
---

## 📦 Requirements

- Python 3.10+
- ffmpeg installed and available in PATH
- yt-dlp installed and available in PATH
- Optional but recommended: `curl_cffi`, so yt-dlp can impersonate a real browser's TLS fingerprint instead of Python's default — makes blocking less likely, independent of cookies/User-Agent. yt-dlp will run fine without it (you'll just see a one-line warning about impersonation being unavailable). If you install it, this yt-dlp release needs a specific version range: `curl_cffi==0.5.10` or `0.10.x`–`0.15.x` (0.16+ isn't supported yet) — check yt-dlp's own `--list-impersonate-targets` output if you're not sure it's working.

---

## 🐍 Python dependencies

```bash
pip install PySide6 psutil yt-dlp "curl_cffi<0.16"
```

---

## 🚀 Usage

Run the application:

```bash
python stream_manager.py
```

Click **⚙ Settings** to configure download output, cookies, User-Agent, and video quality before adding streams — see below.

---

## ⚙️ Settings

Everything setup-related lives in one dialog (click **⚙ Settings** in the toolbar) instead of the main window, and every field here is saved automatically and restored next time you open the app.

**Download output** — the folder downloads are saved to. Type a path or use Browse.

**Video quality** — maximum resolution and FPS to download at (yt-dlp falls back gracefully if your exact preference isn't available for a given stream).

**Cookie source** — cookies are always used now; Chaturbate has been tightening access controls over time, and in practice most streams need them to work at all, not just private/age-restricted ones. Pick which browser you're logged into Chaturbate with from the dropdown:

`firefox`, `chrome`, `chromium`, `edge`, `brave`, `opera`, `safari`

If you use a Firefox-based browser that isn't in that list (Floorp, Zen, LibreWolf, etc.), leave the browser dropdown on `firefox` and browse directly to that browser's profile folder instead — it uses the same cookie storage format under the hood, so this works even though the browser itself isn't one yt-dlp recognizes by name.

**User-Agent** — a real browser's user-agent string, ideally matching the browser your cookies came from. Often needed to get past Cloudflare's Turnstile check, even with valid cookies — this has come up on macOS in particular. Find yours by searching "what is my user agent", or visit whatsmyua.info. Leave it blank to use yt-dlp's default.

---

## ⚠️ Known limitations

- Session-based stream URLs may expire during long downloads
- Some streams may appear “online” but still require authentication for download access

## 📝 Changelog

### (August 2026)
- Added a Settings dialog (download output, cookie source with a folder picker, User-Agent, video quality) — moved out of the cramped main toolbar, and every field now persists across restarts
- Cookies are now always used (no more on/off toggle) — Chaturbate's access controls have tightened enough that this is effectively required anyway; you can still point at a custom Firefox-based browser profile (Floorp, Zen, LibreWolf, etc.) that isn't in the browser list
- Fixed the stream table not stretching to fill the window width
- Removed duplicated shutdown logic and debug logging left over from development, including a `libshiboken`/"already deleted" error that could print on close
- Added user-agent support for macOS compatibility (improves compatibility with some platforms)
- Improved filename format: `username YYYY-MM-DD HH_MM.mp4`
- Added smart fallback for resolution selection when preferred formats are unavailable
- Added adaptive stream checking: offline streams now use gradually longer check intervals to reduce unnecessary requests while still detecting new streams quickly
- Added auto-download protection: streams that repeatedly produce very short downloads are temporarily disabled to prevent restart loops
- Improved error handling and user feedback throughout the application
