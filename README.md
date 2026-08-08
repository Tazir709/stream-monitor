# Stream Monitor

A desktop GUI for monitoring and auto-recording live Chaturbate streams on Windows, macOS, and Linux — tracks multiple models in parallel, shows live thumbnail previews, and downloads automatically via `yt-dlp` and `ffmpeg` the moment someone goes live.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
[![Latest Release](https://img.shields.io/github/v/release/Tazir709/stream-monitor)](https://github.com/Tazir709/stream-monitor/releases/latest)

---
### Tested on:
- Windows 10 / 11
- Fedora Linux (recent versions)
---

## ✨ Features

- Live stream status checking via `yt-dlp`, with adaptive polling — checks every 90s while a stream is live, backs off up to 300s for streams that stay offline, so idle rooms don't get hammered
- Real-time thumbnail previews, captured via a single-frame `ffmpeg` grab per stream
- Automatic recording via `yt-dlp` the moment a tracked stream goes live, with a 5-level format-selector fallback (exact resolution+FPS down to just "best available")
- Settings dialog for download output, cookies (read live from a real browser's cookie database), User-Agent, and video quality — every field persists across restarts
- Auto-download protection: streams that repeatedly produce very short recordings get temporarily disabled, instead of thrashing in a start/stop loop
- Multi-threaded design (Qt worker threads for status-checking, previews, and each active download) so the GUI never blocks
- Per-stream Auto-start toggle, so already-known streams pick back up automatically next launch

---
## 📸 Preview

The application showing multiple stream states:

![Dashboard Preview](app.png)
---

## 📦 Requirements

- Python 3.10+
- ffmpeg installed and available in PATH (can't be installed via pip — needs a real system-level install, see below)
- yt-dlp — either `pip install`ed into this project's own venv (recommended, see below) or available system-wide on PATH. The app looks for a copy inside its own venv first and falls back to PATH automatically, so either works without any extra setup.
- Optional but recommended: `curl_cffi`, so yt-dlp can impersonate a real browser's TLS fingerprint instead of Python's default — makes blocking less likely, independent of cookies/User-Agent. yt-dlp runs fine without it (you'll just see a one-line warning that impersonation is unavailable). If you do install it, this yt-dlp release only supports `curl_cffi==0.5.10` or `0.10.x`–`0.15.x` — 0.16+ isn't supported yet, and `pip install curl_cffi` alone will grab the latest (unsupported) version. Check yt-dlp's own `--list-impersonate-targets` output if you're not sure it's working.

---

## 🐍 Installation

### Step 1 — Install Python
- **Windows**: Download from [python.org](https://www.python.org/downloads/) — check **"Add Python to PATH"** during install
- **macOS**: `brew install python` or download from [python.org](https://www.python.org/downloads/)
- **Ubuntu/Debian**: `sudo apt install python3 python3-pip python3-venv git curl`
- **Arch Based**: `sudo pacman -Syu python python-pip python-venv git curl`
- **Fedora**: `sudo dnf install python3 python3-pip python3-venv git curl`

### Step 2 — Install ffmpeg
`yt-dlp` gets installed via pip in the next step, but `ffmpeg` needs a separate system-level install — there's no pip package for the actual binary:
- **Windows**: Download a build from [ffmpeg.org](https://ffmpeg.org/download.html) and add its `bin` folder to PATH
- **macOS**: `brew install ffmpeg`
- **Ubuntu/Debian**: `sudo apt install ffmpeg`
- **Arch Based**: `sudo pacman -S ffmpeg`
- **Fedora**: `sudo dnf install ffmpeg`

### Step 3 — Download and set up

**Windows (PowerShell):**
```powershell
git clone https://github.com/Tazir709/stream-monitor.git
cd stream-monitor
python -m venv Stream_Venv
Stream_Venv\Scripts\activate
pip install PySide6 psutil yt-dlp "curl_cffi<0.16"
python stream_manager.py
```

**macOS / Linux:**
```bash
git clone https://github.com/Tazir709/stream-monitor.git
cd stream-monitor
python3 -m venv Stream_Venv
source Stream_Venv/bin/activate
pip install PySide6 psutil yt-dlp "curl_cffi<0.16"
python stream_manager.py
```

> **What is a venv?** A virtual environment is an isolated folder that holds Python packages just for this project, so they don't clash with anything else on your system or other Python projects you have installed. Everything `pip install`s above goes into the `Stream_Venv` folder, not system-wide.

**Next time**, just run it directly with the venv's own Python — no need to reinstall anything, and you don't even need to reactivate the venv first, since the app finds its own `yt-dlp`/dependencies regardless:
```bash
Stream_Venv/bin/python stream_manager.py     # macOS/Linux
Stream_Venv\Scripts\python.exe stream_manager.py   # Windows
```

---

## ⚙️ Configuration

Most day-to-day settings — download output folder, cookies, User-Agent, and video quality — are configured from inside the app now: click **⚙ Settings** after launching (see [Settings](#️-settings) below). They persist automatically once set; you don't need to edit the script at all for normal use.

A couple of settings are still code-only, at the top of `stream_manager.py`, since they're one-time tuning rather than something you'd change per session:

```python
# Disable auto-download restarts for a stream if its recordings keep coming
# out shorter than this many seconds (prevents thrashing on a flaky stream)
AUTO_DOWNLOAD_DISABLE_SECONDS = 300  # 0 to disable

# Used only to estimate bandwidth usage per resolution — not a hard limit
DOWNLOAD_BITRATE_KBPS = {
    "640x360": 896,
    "960x540": 1696,
    "1280x720": 3096,
    "1920x1080": 5128,
    "3840x2160": 7192,
}
```

---

## 🚀 Usage

Run the application:

```bash
python stream_manager.py
```

1. Click **⚙ Settings** and set your download folder, cookies, and (if needed) User-Agent before adding anything — see below.
2. Paste a stream URL (e.g. `https://chaturbate.com/username/`) into the top field and click **+ Add Stream**.
3. Toggle **Auto** on a row to have that stream start recording automatically whenever it goes live, or click **Start**/**Stop** to control it manually.
4. **Stop All** immediately stops every active download — useful before closing the app if you don't want to wait for the confirmation-per-stream flow.

---

## ⚙️ Settings

Everything setup-related lives in one dialog (click **⚙ Settings** in the toolbar) instead of the main window, and every field here is saved automatically and restored next time you open the app.

**Download output** — the folder downloads are saved to. Type a path or use Browse.

**Video quality** — maximum resolution and FPS to download at (yt-dlp falls back gracefully through lower tiers if your exact preference isn't available for a given stream).

**Cookie source** — cookies are always used now; Chaturbate has been tightening access controls over time, and in practice most streams need them to work at all, not just private/age-restricted ones. Pick which browser you're logged into Chaturbate with from the dropdown:

`firefox`, `chrome`, `chromium`, `edge`, `brave`, `opera`, `safari`

If you use a Firefox-based browser that isn't in that list (Floorp, Zen, LibreWolf, etc.), leave the browser dropdown on `firefox` and browse directly to that browser's profile folder instead — it uses the same cookie storage format under the hood, so this works even though the browser itself isn't one yt-dlp recognizes by name.

**User-Agent** — a real browser's user-agent string, ideally matching the browser your cookies came from. Often needed to get past Cloudflare's Turnstile check, even with valid cookies — this has come up on macOS in particular. Find yours by searching "what is my user agent", or visit whatsmyua.info. Leave it blank to use yt-dlp's default.

---

## 📁 Output structure

Recordings are saved flat inside your configured Download output folder, one file per recording session:

```
Downloads/
├── some_model 2026-08-09 14_30.mp4
├── some_model 2026-08-09 18_05.mp4
└── another_model 2026-08-09 15_12.mp4
```

---

## 🔧 Troubleshooting

**`ffmpeg` errors with "No such file or directory" even though `yt-dlp` works fine** — `ffmpeg` can't be `pip install`ed (there's no package for the actual binary), so it always needs a real system-level install and to be on PATH — see Step 2 above. This is different from `yt-dlp`, which the app can find inside its own venv automatically even without activating it.

**A stream shows offline when you know it's live, or downloads fail immediately** — almost always missing cookies. Open Settings and confirm Cookie source is pointed at a browser you're actually logged into Chaturbate with.

**Still blocked even with valid cookies and a matching User-Agent** — two things worth checking: make sure `curl_cffi` is installed in a supported version (`0.5.10` or `0.10.x`–`0.15.x`, see Requirements above) so yt-dlp can impersonate a real browser's TLS fingerprint; and double-check your User-Agent actually matches the browser your cookies came from, not just "some" browser.

**Previews never show up** — preview capture needs `ffmpeg` specifically (separate from `yt-dlp`, which handles the actual downloads). Confirm `ffmpeg -version` works from the same terminal/environment the app is running in.

**App window doesn't seem to open** — a successful launch prints nothing to the terminal at all (this is normal Qt behavior); check for a new window rather than assuming it crashed. If it genuinely didn't start, run it from a terminal to see the actual traceback.

---

## ⚠️ Known limitations

- Session-based stream URLs may expire during long downloads
- Some streams may appear "online" but still require authentication for download access

## 📝 Changelog

### (August 2026)
- `yt-dlp`/`ffmpeg` are now found automatically if installed inside this project's own venv, without needing that venv to be activated first — previously the app called them by bare name, which only ever resolved via system PATH regardless of what was actually pip-installed
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
