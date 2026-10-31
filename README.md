# Stream Monitor

A desktop GUI for monitoring and automatically recording live streams from multiple cam sites on Windows, macOS, and Linux.

**Supported sites**

- [chaturbate.com](https://chaturbate.com)
- [stripchat.com](https://stripchat.com)
- [bongacams.com](https://bongacams.com)
- [camsoda.com](https://camsoda.com)
- [xlovecam.com](https://www.xlovecam.com)

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Latest Release](https://img.shields.io/github/v/release/Tazir709/stream-monitor)](https://github.com/Tazir709/stream-monitor/releases/latest)

## Features

- Monitor multiple models, with live status and thumbnail previews.
- Record manually or automatically when a tracked stream goes live.
- Configure recording quality, filenames, output folders, and file-size splitting.
- Use browser cookies and a custom User-Agent when a site requires them.
- Track recording progress and manage downloads from the app.

## Screenshots

![Stream Monitor showing tracked streams](https://github.com/user-attachments/assets/5b309edf-2128-4e23-888b-7a78e6ec0a5c)

![Stream Monitor settings](https://github.com/user-attachments/assets/6662865c-b07b-4da8-883b-21deac0208e8)

## Install

**Requirements:** Python 3.10+, FFmpeg available on your `PATH`, and an internet connection.

### 1. Install Python and FFmpeg

- **Windows:** Install [Python](https://www.python.org/downloads/) and FFmpeg using `winget install ffmpeg`.
- **macOS:** `brew install python ffmpeg`
- **Ubuntu/Debian:** `sudo apt install python3 python3-venv ffmpeg`
- **Fedora:** `sudo dnf install python3 ffmpeg`
- **Arch Linux:** `sudo pacman -S python python-pip python-venv ffmpeg`

### 2. Download Stream Monitor

Choose either method:

- **Download ZIP:** Download the latest release ZIP from the [Releases page](https://github.com/Tazir709/stream-monitor/releases) and extract it.
- **Clone with Git:**

  ```bash
  git clone https://github.com/Tazir709/stream-monitor.git
  cd stream-monitor
  ```

### 3. Run the setup script

Open the extracted or cloned `stream-monitor` folder.

- **Windows:** Run `setup_windows.bat`, then `launch.bat` to start Stream Monitor.
- **macOS/Linux:** Run `chmod +x setup.sh && ./setup.sh`, then follow the launch instructions below.

The setup script creates `Stream_Venv` and installs the Python dependencies. FFmpeg must be installed separately as a system application.

## Use

Launch `stream_manager.py` (double-click on Windows, or run `python stream_manager.py` from a terminal). The app starts with its project virtual environment automatically.

On Windows, you can also use the launchers in the project folder:

- Double-click `launch.bat` for normal use without a console window.
- Double-click `launch_debug.bat` to keep a console open and see error output.

1. Open **Settings** to choose your download folder and configure browser cookies if needed.
2. Paste a supported model URL, such as `https://chaturbate.com/username/`, and select **Add Stream**.
3. Use **Start**/**Stop** to control recording, or enable **Auto** to record when the model goes live.

## Settings at a glance

- **General:** download folders, low-disk-space fallback, auto-restart protection, and bandwidth limits.
- **Browser & Cookies:** browser/profile, optional exported cookies file, and User-Agent.
- **Recording:** maximum resolution and FPS, filename template, and optional file-size splitting.

Settings are saved automatically. The default filename template is `{username} {date} {time}`; recordings are already organized into a folder for each model. Templates support `{site}`, `{username}`, `{date}`, `{time}`, `{resolution}`, and `{fps}`. Use forward slashes (`/`) to define folders from the selected output directory, for example `{site}/{username}/{username} {date} {time}`.

File-size splitting can happen during recording (with brief gaps between segments) or after recording (which temporarily needs additional disk space). Actual segment sizes may vary with stream bitrate and keyframes.

## Troubleshooting

- **Stream appears offline or recording fails:** if the site requires login, select a browser you are logged into under **Settings → Browser & Cookies**. A matching browser User-Agent may also help.
- **Chrome/Chromium cookie access fails on Windows:** export cookies from your browser with [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc), then select the exported file in Settings.
- **FFmpeg not found:** verify `ffmpeg -version` works in a terminal and that FFmpeg is on `PATH`.
- **Previews are missing:** previews require FFmpeg. Stripchat previews also require an FFmpeg build with the native H.264 decoder.
- **App does not open:** run `python stream_manager.py` from a terminal to see any startup errors.

## Output

Recordings are saved in the selected output folder, in a subfolder for each model. The default filename includes the model name and recording date/time.
