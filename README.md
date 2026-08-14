# Stream Monitor

A desktop GUI for monitoring and auto-recording live streams from **Chaturbate, Camsoda, and BongaCams** on Windows, macOS, and Linux — tracks multiple models in parallel, shows live thumbnail previews, and downloads automatically via `yt-dlp` and `ffmpeg` when someone goes live.


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
- Multi-site support for Chaturbate, Camsoda, and BongaCams, with per-site `yt-dlp` overrides where required
- Real-time thumbnail previews, captured via a single-frame `ffmpeg` grab per stream
- Automatic recording via `yt-dlp` the moment a tracked stream goes live, with a 5-level format-selector fallback (exact resolution+FPS down to just "best available")
- Settings dialog for download output, cookies (read live from a real browser's cookie database), User-Agent, and video quality — every field persists across restarts
- Auto-download protection: streams that repeatedly produce very short recordings get temporarily disabled, instead of thrashing in a start/stop loop
- Multi-threaded design (Qt worker threads for status-checking, previews, and each active download) so the GUI never blocks
- Per-stream Auto-start toggle, so already-known streams pick back up automatically next launch

---
## 📸 Preview

The application showing multiple stream states:
<img width="1022" height="816" alt="1" src="https://github.com/user-attachments/assets/5b309edf-2128-4e23-888b-7a78e6ec0a5c" />



Stream-monitor settings:
<img width="1023" height="814" alt="2" src="https://github.com/user-attachments/assets/fa0b86ba-4219-475d-983b-e1bb3990605b" />





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
- **Windows**: `winget install ffmpeg` (easiest — adds it to PATH automatically), or download a build from [ffmpeg.org](https://ffmpeg.org/download.html) and add its `bin` folder to PATH manually — see [this guide](https://phoenixnap.com/kb/ffmpeg-windows) if you need help with that step
- **macOS**: `brew install ffmpeg`
- **Ubuntu/Debian**: `sudo apt install ffmpeg`
- **Arch Based**: `sudo pacman -S ffmpeg`
- **Fedora**: `sudo dnf install ffmpeg`

### Step 3 — Download and set up

Clone the repo first:
```bash
git clone https://github.com/Tazir709/stream-monitor.git
cd stream-monitor
```

Then either run the setup script for your OS (creates the venv and installs everything in one go), or do it manually — both end up in the same place, the script is just a shortcut:

**Windows — script:**
```powershell
setup_windows.bat
```
**Windows — manual (PowerShell):**
```powershell
python -m venv Stream_Venv
Stream_Venv\Scripts\activate
pip install PySide6 psutil yt-dlp "curl_cffi<0.16"
```

**macOS / Linux — script:**
```bash
chmod +x setup.sh
./setup.sh
```
**macOS / Linux — manual:**
```bash
python3 -m venv Stream_Venv
source Stream_Venv/bin/activate
pip install PySide6 psutil yt-dlp "curl_cffi<0.16"
```

> **What is a venv?** A virtual environment is an isolated folder that holds Python packages just for this project, so they don't clash with anything else on your system or other Python projects you have installed. Everything `pip install`s above goes into the `Stream_Venv` folder, not system-wide.

Once that's done (whichever way you set it up), just double-click `stream_manager.py` (or run `./stream_manager.py` / `python stream_manager.py`) to launch it — every time, not just the first — it automatically relaunches itself under `Stream_Venv`'s own Python no matter how it's started, so the venv never needs activating or targeting manually. On Windows this launches without a console window popping up behind the GUI.

---

## ⚙️ Configuration

Most day-to-day settings — download output folder, cookies, User-Agent, video quality, and the auto-restart short-download protection — are configured from inside the app now: click **⚙ Settings** after launching (see [Settings](#️-settings) below). They persist automatically once set; you don't need to edit the script at all for normal use.

One setting is still code-only, at the top of `stream_manager.py`, since it's one-time tuning rather than something you'd change per session. `DOWNLOAD_BITRATE_KBPS` maps each resolution to an expected bitrate in kbps, used to power the **"Download info"** estimate at the bottom of the main window — it shows roughly how much data each active recording is using per hour, and a combined total across everything currently downloading. It's just an estimate, not a real measurement or a limit of any kind, so most people never need to touch it — the only reason to edit it is if you want more accurate MB/h-per-resolution numbers for your own typical streams than the defaults below give you:

```python
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

### Run the application

- **Windows:** double-click `stream_manager.py`
- **macOS/Linux:** `./stream_manager.py`

No venv activation or interpreter path needed either way — it relaunches itself automatically.

1. Click **⚙ Settings** and set your download folder, cookies, and (if needed) User-Agent before adding anything — see below.
2. Paste a stream URL (e.g. `https://chaturbate.com/username/`) into the top field and click **+ Add Stream**.
3. Toggle **Auto** on a row to have that stream start recording automatically whenever it goes live, or click **Start**/**Stop** to control it manually.
4. **Stop All** immediately stops every active download — useful before closing the app if you don't want to wait for the confirmation-per-stream flow.

---

## ⚙️ Settings

Everything setup-related lives in one dialog (click **⚙ Settings** in the toolbar) instead of the main window, and every field here is saved automatically and restored next time you open the app.

**Download output** — the folder downloads are saved to. Type a path or use Browse.

**Video quality** — maximum resolution and FPS to download at (yt-dlp falls back gracefully through lower tiers if your exact preference isn't available for a given stream).

**Disable auto-restart if shorter than** — if a stream's auto-restarted recording ends before this much time has passed, auto-restart is temporarily disabled for that stream, to protect against a restart-loop on flaky streams. Pick a value (2/5/10/15/20/30, or `Custom` for an exact number) and a unit (seconds/minutes), or check **Off** to disable this protection entirely.

**Auto re-enable after** — once auto-restart gets disabled for a flaky stream (above), automatically re-check Auto for it after this much time, giving the stream a chance to settle before retrying. Pick a value (5/10/15/30/60, or `Custom`) and a unit (minutes/hours), or check **Off** (the default) to require re-checking Auto yourself instead — no set-and-forget retry.

**Cookie source** — cookies are always used now; Chaturbate has been tightening access controls over time, and in practice most streams need them to work at all, not just private/age-restricted ones. Pick which browser you're logged into Chaturbate with from the dropdown:

`chrome`, `chromium`, `edge`, `brave`, `opera`, `safari`, `Firefox-based`

Picking `Firefox-based` reveals a second dropdown: `Firefox`, `Floorp`, `Zen`, `LibreWolf`, or `Other (browse manually)`. Firefox, Floorp, Zen, and LibreWolf all auto-detect their default profile automatically — the button next to the dropdowns just reads **Browse…** in that case, since nothing needs picking. If detection fails, or you pick `Other` (for any other Firefox-based browser not listed, or a non-default profile), click that button to point at the profile folder directly — once set, it shows the chosen path instead of "Browse…".

**Cookies file** (Windows only, Chrome/Chromium/Edge/Brave/Opera) — this row only appears on Windows when one of those browsers is selected, since Chromium-based browsers can fail to expose cookies to yt-dlp there (a known Windows-only limitation, not a bug in this app — closing the browser doesn't reliably fix it). If that happens, export your cookies (see [Exporting cookies](#-exporting-cookies) below) and select the file here — it's used instead of live browser extraction. Click **✕** to clear it and go back to normal extraction. Not needed at all on Linux/macOS, or for Firefox-based browsers.

**User-Agent** — a real browser's user-agent string, ideally matching the browser your cookies came from. Often needed to get past Cloudflare's Turnstile check, even with valid cookies — this has come up on macOS in particular. Find yours by searching "what is my user agent", or visit whatsmyua.info. Leave it blank to use yt-dlp's default.

---

## 🍪 Exporting cookies

Only relevant if the **Cookies file** fallback above ever comes up (Chromium browsers on Windows) — most users never need this.

1. Install [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) in the browser you're logged into Chaturbate with.
2. Log into chaturbate.com normally in that browser.
3. Click the extension's icon, export cookies (either just for chaturbate.com or all sites both work), and save the resulting `.txt` file somewhere you'll remember.
4. In Stream Monitor's Settings, with that Chromium browser selected, click the **Cookies file** row's button and pick the file you just saved.

---

## 📁 Output structure

Recordings are saved inside your configured Download output folder, one subfolder per model, one file per recording session:

```
Downloads/
├── some_model/
│   ├── some_model 2026-08-09 14_30_05.mp4
│   └── some_model 2026-08-09 18_05_41.mp4
└── another_model/
    └── another_model 2026-08-09 15_12_18.mp4
```

---

## 🔧 Troubleshooting

**`ffmpeg` errors with "No such file or directory" even though `yt-dlp` works fine** — `ffmpeg` can't be `pip install`ed (there's no package for the actual binary), so it always needs a real system-level install and to be on PATH — see Step 2 above. This is different from `yt-dlp`, which the app can find inside its own venv automatically even without activating it.

**A stream shows offline when you know it's live, or downloads fail immediately** — almost always missing cookies. Open Settings and confirm Cookie source is pointed at a browser you're actually logged into Chaturbate with.

**Still blocked even with valid cookies and a matching User-Agent** — two things worth checking: make sure `curl_cffi` is installed in a supported version (`0.5.10` or `0.10.x`–`0.15.x`, see Requirements above) so yt-dlp can impersonate a real browser's TLS fingerprint; and double-check your User-Agent actually matches the browser your cookies came from, not just "some" browser.

**Previews never show up** — preview capture needs `ffmpeg` specifically (separate from `yt-dlp`, which handles the actual downloads). Confirm `ffmpeg -version` works from the same terminal/environment the app is running in.

**A popup says Chrome/Chromium cookie access failed** — the app checks this automatically for Chrome/Edge/Brave/Opera on Windows and warns you if it fails; it's a known Chrome-on-Windows limitation, not a bug in this app, and closing the browser doesn't reliably fix it. Either export your cookies (see [Exporting cookies](#-exporting-cookies) above) and select the file in Settings' **Cookies file** row, or switch to a Firefox-based browser instead, which isn't affected.

**App window doesn't seem to open** — A successful launch prints nothing to the terminal at all (this is normal Qt behavior), so check for a new window rather than assuming it crashed. If it genuinely doesn't start, run it from a terminal to see the actual traceback.

*Windows-specific*: If double-clicking stream_manager.py does nothing, you can launch it directly using the project's own Python interpreter:
1. Right-click `stream_manager.py` → **Open with** → **Choose another app** → **Choose an app on your PC**
2. Navigate to your `stream-monitor` folder → `Stream_Venv` → `Scripts` → select **`pythonw.exe`**
3. If desired, check **"Always use this app to open .py files"** to make future double-clicks work automatically.

---

## ⚠️ Known limitations

* Some streams may appear "online" but still require authentication for download access — cookies fix this, see [Troubleshooting](#-troubleshooting) above
* **Chromium-based browsers on Windows:** cookie access can be unreliable — see [Troubleshooting](#-troubleshooting) above for how to fix it.

---

## 📝 Changelog

### (August 2026)
- `yt-dlp`/`ffmpeg` are now found automatically if installed inside this project's own venv, without needing that venv to be activated first — previously the app called them by bare name, which only ever resolved via system PATH regardless of what was actually pip-installed
- Added a Settings dialog (download output, cookie source with a folder picker, User-Agent, video quality) — moved out of the cramped main toolbar, and every field now persists across restarts
- Cookies are now always used (no more on/off toggle) — Chaturbate's access controls have tightened enough that this is effectively required anyway; you can still point at a custom Firefox-based browser profile (Floorp, Zen, LibreWolf, etc.) that isn't in the browser list
- Fixed the stream table not stretching to fill the window width
- Removed duplicated shutdown logic and debug logging left over from development, including a `libshiboken`/"already deleted" error that could print on close
- Added user-agent support for macOS compatibility (improves compatibility with some platforms)
- Improved filename format: `username YYYY-MM-DD HH_MM_SS.mp4` (includes seconds, so an auto-restart within the same minute as a previous recording ended doesn't collide with it and silently get skipped)
- Added smart fallback for resolution selection when preferred formats are unavailable
- Added adaptive stream checking: offline streams now use gradually longer check intervals to reduce unnecessary requests while still detecting new streams quickly
- Added auto-download protection: streams that repeatedly produce very short downloads are temporarily disabled to prevent restart loops
- Improved error handling and user feedback throughout the application
