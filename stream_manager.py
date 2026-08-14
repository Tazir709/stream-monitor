#!/usr/bin/env bash
""":"
exec "$(dirname "$0")/Stream_Venv/bin/python3" "$0" "$@"
":"""

import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")

# ============================================================
# SELF-RELAUNCH — always run under this project's own venv Python, no
# matter how the script was started (double-click, system Python, a bare
# `python stream_manager.py`, etc). Must happen before any non-stdlib
# import (PySide6) -- that only exists inside the venv. On Windows this
# targets pythonw.exe specifically, so double-clicking never pops up a
# console window behind the GUI.
# ============================================================
_script_dir = Path(__file__).resolve().parent
_venv_dir = _script_dir / "Stream_Venv"
if IS_WINDOWS:
    _target_python = _venv_dir / "Scripts" / "pythonw.exe"
    if not _target_python.exists():
        _target_python = _venv_dir / "Scripts" / "python.exe"
    _already_there = Path(sys.executable).resolve() == _target_python.resolve()
else:
    _target_python = _venv_dir / "bin" / "python3"
    _already_there = Path(sys.prefix).resolve() == _venv_dir.resolve()

if not _already_there:
    if not _target_python.exists():
        print(f"Stream_Venv not found at {_target_python}")
        print("Run setup.sh (Unix) or setup_windows.bat (Windows) first.")
        if IS_WINDOWS:
            input("\nPress Enter to close this window...")
        sys.exit(1)
    os.execv(str(_target_python), [str(_target_python), str(Path(__file__).resolve()), *sys.argv[1:]])

import subprocess
import tempfile
import time
import threading
import psutil
import re
import json
import shutil
from enum import Enum
from typing import Optional, Dict
from datetime import datetime
from queue import Queue, Empty
from dataclasses import dataclass, field
import traceback

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QLabel, QPushButton, QLineEdit,
    QTextEdit, QCheckBox, QStatusBar, QMessageBox, QHeaderView,
    QSplitter, QFrame, QSizePolicy, QDialog, QFileDialog, QComboBox,
    QFormLayout, QDialogButtonBox, QSpinBox
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QEvent, QSize, QDir, QUrl
from PySide6.QtGui import QImage, QPixmap, QFont, QColor, QPalette, QDesktopServices


# ─────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────

BROWSER = ""
BROWSER_DISPLAY = ""  # "Floorp"/"Zen"/"LibreWolf" when a fork preset is active, else derived from BROWSER directly
COOKIES_FILE = ""  # optional exported-cookies.txt fallback for Chromium browsers on Windows
USER_AGENT = ""
DOWNLOAD_OUTPUT = "Downloads" # Folder downloads are saved to. Settable from the Settings dialog; persisted across sessions.
MAX_DOWNLOAD_RESOLUTION = "1920x1080" # 3840x2160, 1920x1080, 1280x720, 960x540, 640x360
MAX_DOWNLOAD_FPS = 30 # 60 or 30

# Prevents unstable streams from continuously rejoining the download queue
# when the system is under heavy load. Prioritizing fewer stable downloads
# is preferable to having many unstable downloads repeatedly stopping and restarting.
# Settable from the Settings dialog; persisted across sessions. 0 to disable.
AUTO_DOWNLOAD_DISABLE_SECONDS = 300

# 0 = off (today's manual-only behavior), the safe default for a new opt-in
# feature. Otherwise, a stream disabled by the protection above gets its
# Auto-restart automatically re-checked after this many seconds.
AUTO_REENABLE_COOLDOWN_SECONDS = 0

DOWNLOAD_BITRATE_KBPS = { # Usage calculation, per resolution, per fps bucket.
    # 60fps entries are ~1.5x their 30fps counterpart (rough real-world
    # H.264 ratio -- bitrate scales sublinearly with fps, not 2x).
    "640x360":   {30: 896,  60: 1344},
    "960x540":   {30: 1696, 60: 2544},
    "1280x720":  {30: 3096, 60: 4644},
    "1920x1080": {30: 5128, 60: 7692},
    "3840x2160": {30: 7192, 60: 10788},
}


def get_bitrate_kbps(resolution: str, fps: int) -> int:
    """Estimate bitrate for a resolution, even when it is not one of the
    known exact buckets. For odd resolutions like 854x480 we scale from the
    nearest known resolution buckets and average the result across the closest
    bucket candidates instead of hardcoding every possible size."""
    if not resolution:
        return 0
    try:
        width, height = [int(part.strip()) for part in resolution.lower().split("x", 1)]
    except Exception:
        return 0
    if width <= 0 or height <= 0:
        return 0
    if not fps:
        fps = 30

    exact = DOWNLOAD_BITRATE_KBPS.get(resolution)
    if exact:
        closest = min(exact.keys(), key=lambda k: abs(k - fps))
        return int(exact.get(closest, 0))

    target_pixels = width * height
    candidates: list[tuple[int, int]] = []
    for known_res, buckets in DOWNLOAD_BITRATE_KBPS.items():
        try:
            known_w, known_h = [int(part.strip()) for part in known_res.lower().split("x", 1)]
        except Exception:
            continue
        if known_w <= 0 or known_h <= 0:
            continue
        known_pixels = known_w * known_h
        candidates.append((abs(target_pixels - known_pixels), known_res, known_pixels))

    if not candidates:
        return 0

    # Average the closest known-size estimates instead of assuming a single
    # exact match, which gives sensible results for resolutions like 854x480.
    closest = sorted(candidates, key=lambda item: item[0])[:3]
    estimates: list[int] = []
    for _, known_res, known_pixels in closest:
        buckets = DOWNLOAD_BITRATE_KBPS.get(known_res, {})
        if not buckets:
            continue
        if fps <= 30:
            fps_value = 30
        elif fps >= 60:
            fps_value = 60
        else:
            fps_value = 30 + (fps - 30) * (60 - 30) / (60 - 30)
        # Interpolate based on the nearest known fps bucket values.
        bucket_keys = sorted(buckets.keys())
        if fps_value <= bucket_keys[0]:
            sample = buckets[bucket_keys[0]]
        elif fps_value >= bucket_keys[-1]:
            sample = buckets[bucket_keys[-1]]
        else:
            lower = max(k for k in bucket_keys if k <= fps_value)
            upper = min(k for k in bucket_keys if k >= fps_value)
            low_val = buckets[lower]
            high_val = buckets[upper]
            if upper == lower:
                sample = low_val
            else:
                ratio = (fps_value - lower) / (upper - lower)
                sample = int(low_val + (high_val - low_val) * ratio)

        scale = target_pixels / known_pixels
        estimates.append(int(sample * scale))

    if not estimates:
        return 0
    return int(sum(estimates) / len(estimates))


def get_download_format_selector() -> str:
    max_height = int(MAX_DOWNLOAD_RESOLUTION.split("x")[1])
    max_fps = MAX_DOWNLOAD_FPS

    # Build a format selector with multiple fallback levels.
    return (
        # Level 1: Best video up to the preferred resolution and FPS
        f"bestvideo[height<={max_height}][fps<={max_fps}]+bestaudio/"
        # Level 2: Best video up to the preferred resolution, regardless of FPS
        f"bestvideo[height<={max_height}]+bestaudio/"
        # Level 3: Best video at or below the preferred FPS, regardless of resolution
        f"bestvideo[fps<={max_fps}]+bestaudio/"
        # Level 4: Best video and audio available, regardless of resolution or FPS
        f"bestvideo+bestaudio/"
        # Level 5: Best combined format available
        f"best"
    )


def get_user_agent() -> str:
    return (USER_AGENT or "").strip()


def get_cookie_args() -> list[str]:
    """--cookies <file> if a Chromium-browser cookies.txt fallback is
    configured and still exists, else the normal --cookies-from-browser."""
    real_browser = BROWSER.split(":", 1)[0]
    if COOKIES_FILE and real_browser in _CHROMIUM_BROWSERS and os.path.isfile(COOKIES_FILE):
        return ["--cookies", COOKIES_FILE]
    return ["--cookies-from-browser", BROWSER]


# ─────────────────────────────────────────────
#  Per-site yt-dlp overrides
# ─────────────────────────────────────────────
#
# Some sites need extra yt-dlp flags to work at all. Keyed by a substring
# that's matched (case-insensitively) against the stream URL. Add new
# sites here rather than sprinkling `if "sitename" in url` checks through
# the command-building code.
#
# "impersonate": True means pass `--impersonate {browser_type}`, reusing
#   whatever browser is already configured in Settings (the real_browser
#   part of BROWSER, e.g. "firefox" out of "firefox:default-release").
#   Set it to a specific string instead (e.g. "chrome") to force a
#   particular impersonate target regardless of the configured browser.
#   Used for both the check worker and the download worker.
# "extra_args": a flat list of additional yt-dlp CLI args appended as-is,
#   used by the download worker only (the check/probe calls stay generic).
# "ffmpeg_extra_args": a flat list of extra ffmpeg args for the *preview*
#   worker's capture command, inserted right after the input (-i) options,
#   e.g. for HLS containers ffmpeg needs extra flags to demux
#   (fmp4-in-.ts-style segments, non-standard extensions, etc).
# "referer": the Referer header to send on the preview worker's ffmpeg
#   capture request. Some sites 403 the HLS segments without it.
SITE_OVERRIDES: dict[str, dict] = {
    "chaturbate.com": {
        "referer": "https://chaturbate.com/",
    },
    "camsoda.com": {
        # Camsoda uses .hls.fmp4 segments; ffmpeg needs -allowed_extensions ALL
        # -extension_picky 0 to demux them, and returns 403 without --impersonate.
        "impersonate": True,
        "extra_args": [
            "--downloader-args", "ffmpeg_i:-extension_picky 0",
        ],
        "ffmpeg_extra_args": ["-allowed_extensions", "ALL", "-extension_picky", "0"],
        "referer": "https://www.camsoda.com/",
    },
    "bongacams.com": {
        # Regular .ts HLS segments; only --impersonate needed, for live checks.
        "impersonate": True,
    },

    # "othersite.com": {
    #     "impersonate": "chrome",  # force a specific target regardless of Settings
    #     "extra_args": ["--some-flag", "value"],
    #     "ffmpeg_extra_args": ["-some-ffmpeg-flag", "value"],
    #     "referer": "https://www.othersite.com/",
    # },

    # Stripchat.com
        # yt-dlp currently returns "Unable to extract data".
        # Wait for a yt-dlp update before adding site-specific handling.

    # cam4.com
        # CAM4 has inconsistent handling of private/spy streams. yt-dlp can report
        # these streams as "is_live" even when ffmpeg cannot actually access the
        # HLS stream, causing the preview to fail or time out.
        #
        # CAM4 could also report HTTP 400/403 errors or JSON parsing errors for
        # certain profiles depending on their current stream state.
        #
        # Auto-download required additional CAM4-specific handling so downloads
        # would wait for a successful preview confirmation rather than starting
        # immediately when yt-dlp reported "is_live".
        #
        # CAM4 support has therefore been disabled for now to avoid unreliable
        # previews, short/incomplete downloads, and confusing status changes.
        # Revisit this if yt-dlp or CAM4's stream handling becomes more reliable.
}


def _site_override(url: str) -> dict:
    """Return the SITE_OVERRIDES entry matching `url` (by substring, case
    insensitive), or {} if no site matches."""
    host = (url or "").lower()
    for site_key, cfg in SITE_OVERRIDES.items():
        if site_key in host:
            return cfg
    return {}


def _impersonate_args(cfg: dict) -> list[str]:
    impersonate = cfg.get("impersonate")
    if not impersonate:
        return []
    if impersonate is True:
        browser_type = BROWSER.split(":", 1)[0].strip()
    else:
        browser_type = str(impersonate)
    return ["--impersonate", browser_type] if browser_type else []


def get_site_args(url: str) -> list[str]:
    """Return extra yt-dlp CLI args (impersonate + extra_args) for whichever
    site `url` belongs to, based on SITE_OVERRIDES. Used by the download
    worker. Safe to call for URLs with no override configured (returns an
    empty list)."""
    cfg = _site_override(url)
    return _impersonate_args(cfg) + list(cfg.get("extra_args", []))


def get_site_check_args(url: str) -> list[str]:
    """Return extra yt-dlp CLI args for the live-status check / URL-resolve
    calls — currently just --impersonate where configured. Kept separate
    from get_site_args() since checks shouldn't need the download-only
    flags (e.g. --downloader-args)."""
    return _impersonate_args(_site_override(url))


def get_site_referer(url: str) -> str:
    """Return the Referer header to use for the preview worker's ffmpeg
    capture, or "" if none is configured for this site."""
    return _site_override(url).get("referer", "")


def get_site_ffmpeg_args(url: str) -> list[str]:
    """Return extra ffmpeg args for the preview worker's capture command,
    or [] if none is configured for this site."""
    return list(_site_override(url).get("ffmpeg_extra_args", []))


def _find_tool(name: str) -> str:
    """Resolve an external tool (yt-dlp, ffmpeg), preferring a copy
    installed in the venv this app is actually running from — via
    sys.prefix, so it works whether that venv was "activated" or its
    Python was invoked directly, without needing to know the venv's
    folder name in advance. Falls back to PATH if not found there.

    This matters specifically because the app calls these tools by bare
    name in subprocess calls, which only resolves via PATH — a venv-only
    pip install of yt-dlp does nothing unless that venv's bin folder is
    on PATH, which isn't guaranteed for a GUI app people often launch by
    double-click rather than from an activated terminal.
    """
    exe_name = f"{name}.exe" if sys.platform == "win32" else name
    venv_bin_dir = "Scripts" if sys.platform == "win32" else "bin"
    venv_path = os.path.join(sys.prefix, venv_bin_dir, exe_name)
    if os.path.isfile(venv_path):
        return venv_path
    return shutil.which(name) or name


def _subprocess_kwargs() -> dict:
    """Return Windows-specific subprocess options to keep console
    windows hidden when launching console applications from the GUI.

    This matters specifically because the app is normally launched
    without a console, but tools such as yt-dlp and ffmpeg are console
    applications and would otherwise briefly open a window on Windows.
    """
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


YTDLP_PATH = _find_tool("yt-dlp")
FFMPEG_PATH = _find_tool("ffmpeg")


# ─────────────────────────────────────────────
#  Data
# ─────────────────────────────────────────────

class StreamStatus(Enum):
    ONLINE  = "online"
    OFFLINE = "offline"
    PRIVATE = "private"
    AWAY    = "away"
    ERROR   = "error"


@dataclass
class StreamItem:
    url: str
    username: str = ""
    auto_start: bool = False
    current_status: StreamStatus = StreamStatus.OFFLINE
    download_active: bool = False
    row: int = -1
    last_check_time: float = 0
    download_start_time: float = 0
    resolution: str = ""
    fps: int = 30  # best-known fps for `resolution` -- probe-based until the live output reveals the real value
    pending_short_check: bool = False  # last download ended early; awaiting post-download status to decide on auto-disable


# ─────────────────────────────────────────────
#  Utilities
# ─────────────────────────────────────────────

class RateLimiter:
    """Thread-safe rate limiter — no more than 1 request per min_interval seconds."""
    def __init__(self, min_interval: float = 2.0):
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait_if_needed(self):
        with self._lock:
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()


def extract_username(url: str) -> str:
    url = url.rstrip("/")
    match = re.search(r'(?:https?://)?[^/]+/([^/?#]+)', url)
    return match.group(1) if match else url


# Friendly display names for known sites, keyed the same way as
# SITE_OVERRIDES (a substring matched against the URL's host). Sites with
# no entry here just fall back to their bare domain label (e.g. "example").
SITE_DISPLAY_NAMES: dict[str, str] = {
    "chaturbate.com": "Chaturbate",
    "camsoda.com": "Camsoda",
    "bongacams.com": "BongaCams",
}


def extract_site(url: str) -> str:
    """Return a short, human-readable site name from a stream URL, e.g.
    "https://chaturbate.com/someuser/" -> "Chaturbate". Falls back to the
    bare registrable domain label (e.g. "example" for example.com/tv) for
    sites with no entry in SITE_DISPLAY_NAMES."""
    host = (url or "").lower()
    for site_key, display_name in SITE_DISPLAY_NAMES.items():
        if site_key in host:
            return display_name

    match = re.search(r'(?:https?://)?(?:www\.)?([^/]+)', url)
    domain = match.group(1) if match else ""
    label = domain.split(".")[0] if domain else ""
    return label.capitalize() if label else "Unknown"


def log_exception(prefix: str) -> None:
    print(f"[Debug] {prefix}")
    traceback.print_exc()


CHROMIUM_COOKIE_ERROR_MSG = "🔒 Chrome cookie access failed — see Troubleshooting in the README"


def format_error_message(stderr: str, stdout: str = "") -> str:
    text = f"{stderr}\n{stdout}".lower()

    if "could not copy chrome cookie database" in text or "failed to decrypt with dpapi" in text:
        return CHROMIUM_COOKIE_ERROR_MSG
    if "403" in text or "forbidden" in text or "access denied" in text:
        return "🚫 Access blocked (403). Try setting a User-Agent or enabling cookies."
    if "age restricted" in text or "age-restricted" in text:
        return "🔒 Age-restricted stream. Enable cookies in config to access."
    if "timed out" in text or "timeout" in text or "connection timed out" in text:
        return "⏱️ Connection timed out. Check your internet and try again."
    if "video unavailable" in text or "not found" in text:
        return "📦 Stream not found. The streamer may be offline or the URL may be incorrect."
    if "rate limit" in text or "too many requests" in text:
        return "🚦 Rate limited. The tool is checking too frequently - wait a moment."
    if "ffmpeg" in text and "invalid data" in text:
        return "🎞️ Stream data error. The stream may have ended or changed format."
    if "code 4294957242" in text or "code -10054" in text:
        return "🌐 Network connection lost. The stream may have ended or your internet dropped."

    # Catch-all with a hint to check logs
    clean_error = stderr.replace("\n", " ").strip()[:100]
    return f"❌ Operation failed: {clean_error}... (Check the log for details)"


# ─────────────────────────────────────────────
#  Cookie access probe
# ─────────────────────────────────────────────

class CookieProbeWorker(QThread):
    """One-shot check of whether yt-dlp can actually read a given
    Chromium browser's cookies on this machine -- needs no URL, since
    cookie loading happens during yt-dlp's own init, before it ever
    checks for one."""
    result_signal = Signal(str, bool)  # (real_browser, accessible)

    def __init__(self, real_browser: str):
        super().__init__()
        self.real_browser = real_browser

    def run(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            tmp_path = tmp.name
        os.unlink(tmp_path)  # want a unique path, not an existing file
        accessible = True
        try:
            cmd = [YTDLP_PATH, "--cookies-from-browser", self.real_browser, "--cookies", tmp_path]
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                **_subprocess_kwargs(),
            )
            text = f"{r.stderr}\n{r.stdout}".lower()
            if "could not copy chrome cookie database" in text or "failed to decrypt with dpapi" in text:
                accessible = False
        except Exception:
            pass
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        self.result_signal.emit(self.real_browser, accessible)


# ─────────────────────────────────────────────
#  Preview worker
# ─────────────────────────────────────────────

class SharedPreviewWorker(QThread):
    preview_updated = Signal(str, QPixmap)

    CAPTURE_INTERVAL  = 90
    HLS_CACHE_LIFETIME = 150

    def __init__(self):
        super().__init__()
        self.setObjectName("SharedPreviewWorker")
        self._running = True
        self._lock = threading.Lock()
        self._live_urls: list[str] = []
        self._stream_url_cache: dict[str, tuple[str, float]] = {}
        self._pixmap_cache: dict[str, QPixmap] = {}
        self._last_capture: dict[str, float] = {}
        self._ffmpeg_processes: list[subprocess.Popen] = []
        self._current_status: dict[str, StreamStatus] = {}
        self._pending_captures: Queue = Queue()
        self._rate_limiter = RateLimiter(2.0)

    # ── public API ──────────────────────────────

    def update_status(self, url: str, status: StreamStatus):
        with self._lock:
            prev = self._current_status.get(url)
            self._current_status[url] = status
            if status == StreamStatus.ONLINE:
                if url not in self._live_urls:
                    self._live_urls.append(url)
                if prev != StreamStatus.ONLINE:
                    self._pending_captures.put(url)
            else:
                self._live_urls = [u for u in self._live_urls if u != url]
                self._pixmap_cache.pop(url, None)
                self._stream_url_cache.pop(url, None)

    def remove_url(self, url: str):
        with self._lock:
            self._live_urls = [u for u in self._live_urls if u != url]
            for d in (self._last_capture, self._pixmap_cache,
                      self._stream_url_cache, self._current_status):
                d.pop(url, None)

    def get_cached_pixmap(self, url: str) -> Optional[QPixmap]:
        return self._pixmap_cache.get(url)

    def stop(self):
        self._running = False
        with self._lock:
            for p in self._ffmpeg_processes:
                try:
                    p.kill(); p.wait(timeout=2)
                except Exception:
                    pass
            self._ffmpeg_processes.clear()
        if not self.wait(6000):
            print("[Debug] SharedPreviewWorker did not stop in time, terminating")
            self.terminate()
            self.wait(2000)

    # ── internals ───────────────────────────────

    def _get_stream_url(self, page_url: str) -> Optional[str]:
        cached = self._stream_url_cache.get(page_url)
        if cached:
            stream_url, expiry = cached
            if time.time() < expiry:
                return stream_url
        try:
            cmd = [YTDLP_PATH, "--get-url", "--no-playlist"]
            user_agent = get_user_agent()
            if user_agent:
                cmd.extend(["--user-agent", user_agent])
            cmd.extend(get_cookie_args())
            cmd.extend(get_site_check_args(page_url))
            cmd.append(page_url)
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                **_subprocess_kwargs(),
            )
            if r.returncode == 0 and r.stdout.strip():
                url = r.stdout.strip().split("\n")[0]
                self._stream_url_cache[page_url] = (url, time.time() + self.HLS_CACHE_LIFETIME)
                return url
            if r.returncode != 0:
                err = format_error_message(r.stderr, r.stdout)
                print(f"[Debug] PreviewWorker _get_stream_url failed for {page_url}: {err}")
        except Exception:
            log_exception(f"PreviewWorker _get_stream_url failed for {page_url}")
        return None

    def _capture(self, page_url: str):
        stream_url = self._get_stream_url(page_url)
        if not stream_url:
            return

        referer = get_site_referer(page_url)

        W, H = 320, 180
        user_agent = get_user_agent() or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        cmd = [
            FFMPEG_PATH,
            "-user_agent", user_agent,
            *([ "-headers", f"Referer: {referer}\r\n"] if referer else []),
            *get_site_ffmpeg_args(page_url),
            "-timeout", "10000000",
            "-i", stream_url,
            "-frames:v", "1",
            "-vf", (
                f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2"
            ),
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "pipe:1",
            "-loglevel", "error", "-nostats",
        ]

        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, **kwargs)
            with self._lock:
                self._ffmpeg_processes.append(proc)
            try:
                stdout, _ = proc.communicate(timeout=30)
                expected = W * H * 3
                if len(stdout) >= expected:
                    img = QImage(stdout[:expected], W, H, W * 3, QImage.Format_RGB888)
                    px = QPixmap.fromImage(img)
                    if not px.isNull():
                        self._pixmap_cache[page_url] = px
                        self.preview_updated.emit(page_url, px)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
                self._stream_url_cache.pop(page_url, None)
            finally:
                with self._lock:
                    if proc in self._ffmpeg_processes:
                        self._ffmpeg_processes.remove(proc)
        except Exception:
            log_exception(f"PreviewWorker _capture failed for {page_url}")
            self._stream_url_cache.pop(page_url, None)

    def run(self):
        index = 0
        last_capture: dict[str, float] = {}

        while self._running:
            try:
                # Drain pending (immediate) captures first — but cap to avoid starvation
                burst = 0
                while burst < 3:
                    try:
                        url = self._pending_captures.get_nowait()
                        with self._lock:
                            live = list(self._live_urls)
                        if url in live:
                            self._rate_limiter.wait_if_needed()
                            self._capture(url)
                            last_capture[url] = time.time()
                        burst += 1
                    except Empty:
                        break

                # Scheduled round-robin
                with self._lock:
                    urls = list(self._live_urls)

                if not urls:
                    time.sleep(2)
                    continue

                url = urls[index % len(urls)]
                index = (index + 1) % max(len(urls), 1)

                age = time.time() - last_capture.get(url, 0)
                if age >= self.CAPTURE_INTERVAL:
                    self._rate_limiter.wait_if_needed()
                    self._capture(url)
                    last_capture[url] = time.time()
                else:
                    time.sleep(min(2, self.CAPTURE_INTERVAL - age))
            except Exception:
                log_exception("PreviewWorker run loop failed")



# ─────────────────────────────────────────────
#  Download worker
# ─────────────────────────────────────────────

class DownloadWorker(QThread):
    log_signal                = Signal(str, str)   # (username, message)
    finished_signal           = Signal(str)        # url
    progress_signal           = Signal(str, int)   # (username, percent)
    resolution_signal         = Signal(str, str, int)  # (url, "1920x1080", fps)
    auto_download_disabled_signal = Signal(str)      # url
    short_download_signal     = Signal(str, int)   # (url, elapsed_seconds) — emitted when a download ends early

    _NOISY_PATTERNS = (
        "error reading http response",
        "end of file",
        "[https @",
        "non monotonous",
        "discontinuity",
        "invalid data found",
        "application provided invalid",
        "[in#",           # ffmpeg demuxer keepalive noise, e.g. "[in#0/hls @ ...]"
    )

    # Matches ffmpeg's stream description line, e.g.:
    #   Stream #0:0[0x0]: Video: h264 ..., 1280x720, 60 fps, 30 tbr, 90k tbn, ...
    # This is the ground truth for the format actually being downloaded --
    # some sites only expose a single format (e.g. 720p60), so this can
    # reveal a higher fps than the pre-download probe assumed.
    _STREAM_FORMAT_RE = re.compile(
        r"Video:.*?(\d+)x(\d+)[^,]*,\s*([\d.]+)\s*fps", re.IGNORECASE
    )

    def __init__(self, stream_url: str, username: str, output_path: str = "Downloads"):
        super().__init__()
        self.setObjectName(f"DownloadWorker-{username}")
        self.stream_url  = stream_url
        self.username    = username
        self.output_path = os.path.join(output_path, username)
        self.process: Optional[subprocess.Popen] = None
        self.is_running  = False
        self._rate_limiter = RateLimiter(2.0)
        self._line_queue: Queue = Queue()
        self._started_at = 0.0
        self._last_detected_format: Optional[tuple[str, int]] = None
        os.makedirs(self.output_path, exist_ok=True)

    def _probe_resolution(self) -> tuple[str, int]:
        """Ask yt-dlp for the selected format's resolution + fps before
        starting the download. This is a best-effort guess from metadata
        -- for sites that only expose one format, the live stream output
        parsed in run() is the more reliable source and will correct
        this once the download actually opens the stream."""
        try:
            cmd = [
                YTDLP_PATH,
                "--no-playlist",
                "--format", get_download_format_selector(),
                "--print", "%(width)sx%(height)s@%(fps)s",
            ]
            user_agent = get_user_agent()
            if user_agent:
                cmd.extend(["--user-agent", user_agent])
            cmd.extend(get_cookie_args())
            cmd.extend(get_site_args(self.stream_url))
            cmd.append(self.stream_url)
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=20,
                **_subprocess_kwargs(),
            )
            if r.returncode == 0:
                line = r.stdout.strip().split("\n")[0]
                m = re.match(r"^(\d+x\d+)@(.+)$", line)
                if m:
                    res = m.group(1)
                    try:
                        fps = int(round(float(m.group(2))))
                    except ValueError:
                        fps = MAX_DOWNLOAD_FPS  # yt-dlp printed "NA" or similar -- fall back to the configured cap
                    return res, fps
            if r.returncode != 0:
                err = format_error_message(r.stderr, r.stdout)
                print(f"[Debug] DownloadWorker probe failed for {self.username}: {err}")
        except Exception:
            pass
        return "", 0

    def _drain_stdout(self, proc: subprocess.Popen):
        """Runs in a tiny daemon thread — reads stdout and queues lines."""
        try:
            for line in iter(proc.stdout.readline, ""):
                self._line_queue.put(line)
        except Exception:
            log_exception(f"DownloadWorker _drain_stdout failed for {self.username}")
        finally:
            self._line_queue.put(None)  # sentinel: EOF

    def run(self):
        if self.is_running:
            return
        try:
            self.log_signal.emit(self.username, "🔍 Probing resolution…")
            res, fps = self._probe_resolution()
            if res:
                self.log_signal.emit(self.username, f"📐 Resolution: {res} ({fps or '?'} fps, probed)")
                self.resolution_signal.emit(self.stream_url, res, fps or MAX_DOWNLOAD_FPS)
                self._last_detected_format = (res, fps or MAX_DOWNLOAD_FPS)
            else:
                self.log_signal.emit(self.username, "📐 Resolution unknown")
                self.resolution_signal.emit(self.stream_url, "", MAX_DOWNLOAD_FPS)
                self._last_detected_format = None

            self._started_at = time.time()

            current_time = datetime.now().strftime("%Y-%m-%d %H_%M_%S")

            cmd = [
                YTDLP_PATH,
                "--no-simulate",
                "--format", get_download_format_selector(),
                "-o", os.path.join(
                    self.output_path,
                    f"{self.username} {current_time}.%(ext)s"
                ),
                "--no-overwrites", "--continue", "--no-part",
                "--skip-unavailable-fragments", "--hls-use-mpegts",
                "--limit-rate", "2M",
                "--no-live-from-start",
            ]
            user_agent = get_user_agent()
            if user_agent:
                cmd.extend(["--user-agent", user_agent])
            cmd.extend(get_cookie_args())
            cmd.extend(get_site_args(self.stream_url))
            cmd.append(self.stream_url)
            self._rate_limiter.wait_if_needed()

            kwargs: dict = dict(
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )
            if os.name == "nt":
                kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP |
                    subprocess.CREATE_NO_WINDOW
                )

            self.process = subprocess.Popen(cmd, **kwargs)
            self.is_running = True
            self.log_signal.emit(self.username, f"▶ Download started (PID: {self.process.pid})")

            drain = threading.Thread(target=self._drain_stdout, args=(self.process,), daemon=True)
            drain.start()

            while self.is_running:
                try:
                    line = self._line_queue.get(timeout=0.5)
                except Empty:
                    if self.process.poll() is not None:
                        break
                    continue

                if line is None:  # EOF sentinel
                    break

                line = line.strip()
                if not line:
                    continue
                if any(s in line.lower() for s in ("downloading webpage", "extracting", "download best")):
                    continue

                fmt_match = self._STREAM_FORMAT_RE.search(line)
                if fmt_match:
                    actual_res = f"{fmt_match.group(1)}x{fmt_match.group(2)}"
                    try:
                        actual_fps = int(round(float(fmt_match.group(3))))
                    except ValueError:
                        actual_fps = 0
                    detected = (actual_res, actual_fps or MAX_DOWNLOAD_FPS)
                    if detected != self._last_detected_format:
                        self._last_detected_format = detected
                        self.log_signal.emit(
                            self.username,
                            f"📐 Actual stream format: {detected[0]} ({detected[1]} fps)"
                        )
                        self.resolution_signal.emit(self.stream_url, detected[0], detected[1])
                    continue

                if "[download]" in line.lower():
                    if "completed" in line.lower():
                        self.log_signal.emit(self.username, "✓ Download completed")
                    else:
                        m = re.search(r"(\d+\.?\d*)%", line)
                        if m:
                            self.progress_signal.emit(self.username, int(float(m.group(1))))
                elif any(k in line.lower() for k in ("error", "warning", "finished")):
                    if not any(n in line.lower() for n in self._NOISY_PATTERNS):
                        message = line[:120]
                        self.log_signal.emit(self.username, message)

            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

            if self._started_at:
                elapsed = int(time.time() - self._started_at)
                if elapsed < AUTO_DOWNLOAD_DISABLE_SECONDS:
                    self.short_download_signal.emit(self.stream_url, elapsed)

        except Exception as e:
            hint = format_error_message(str(e))
            self.log_signal.emit(self.username, f"❌ Error: {str(e)[:100]} | {hint}")
            print(f"[Debug] DownloadWorker run failed for {self.username}: {e!r}")
            traceback.print_exc()
        finally:
            self.is_running = False
            self.finished_signal.emit(self.stream_url)


    def stop(self):
        """Signal the process tree to stop. Safe to call multiple times."""
        if not self.is_running:
            return

        proc = self.process
        if not proc:
            return

        self.log_signal.emit(self.username, "⏹ Stopping download…")
        self.is_running = False

        try:
            parent = psutil.Process(proc.pid)
            children = parent.children(recursive=True)

            for p in children:
                try:
                    p.terminate()
                except psutil.NoSuchProcess:
                    pass

            parent.terminate()

            gone, alive = psutil.wait_procs(
                children + [parent],
                timeout=5
            )

            for p in alive:
                try:
                    p.kill()
                except psutil.NoSuchProcess:
                    pass

        except (psutil.NoSuchProcess, ProcessLookupError):
            pass

        except Exception as e:
            self.log_signal.emit(
                self.username,
                f"❌ Stop error: {str(e)[:80]}"
            )
            print(
                f"[Debug] DownloadWorker stop failed for {self.username}: {e!r}"
            )
            traceback.print_exc()


# ─────────────────────────────────────────────
#  Stream checker
# ─────────────────────────────────────────────

class StreamChecker(QThread):
    status_signal = Signal(str, object, str)  # (url, StreamStatus, message)

    CHECK_INTERVAL = 90
    BACKOFF_STEP = 30
    MAX_CHECK_INTERVAL = 300

    def __init__(self, ytdlp_path: str = YTDLP_PATH):
        super().__init__()
        self.setObjectName("StreamChecker")
        self._ytdlp   = ytdlp_path
        self._running = True
        self._rate_limiter = RateLimiter(2.0)
        self._lock: threading.Lock = threading.Lock()
        self._tracked: dict[str, float] = {}
        self._check_intervals: dict[str, float] = {}

    # ── public API ──────────────────────────────

    def add_stream(self, url: str, force: bool = False):
        with self._lock:
            if force or url not in self._tracked:
                self._tracked[url] = 0.0
                self._check_intervals[url] = self.CHECK_INTERVAL

    def remove_stream(self, url: str):
        with self._lock:
            self._tracked.pop(url, None)
            self._check_intervals.pop(url, None)

    def force_check(self, url: str):
        """Reset timestamp so the URL is checked on the next loop tick."""
        with self._lock:
            if url in self._tracked:
                self._tracked[url] = 0.0
                self._check_intervals[url] = self.CHECK_INTERVAL

    def stop(self):
        self._running = False

    # ── worker loop ─────────────────────────────

    def run(self):
        while self._running:
            now = time.time()

            with self._lock:
                due = [
                    url for url, last in self._tracked.items()
                    if now - last >= self._check_intervals.get(url, self.CHECK_INTERVAL)
                ]

            for url in due:
                if not self._running:
                    break
                status, message = self._check(url)
                with self._lock:
                    if url in self._tracked:
                        self._tracked[url] = time.time()
                        if status == StreamStatus.ONLINE:
                            self._check_intervals[url] = self.CHECK_INTERVAL
                        elif status == StreamStatus.OFFLINE:
                            current = self._check_intervals.get(url, self.CHECK_INTERVAL)
                            self._check_intervals[url] = min(current + self.BACKOFF_STEP, self.MAX_CHECK_INTERVAL)
                self.status_signal.emit(url, status, message)

            time.sleep(1)

    def _check(self, url: str) -> tuple[StreamStatus, str]:
        self._rate_limiter.wait_if_needed()
        try:
            cmd = [self._ytdlp, "--simulate", "--print", "%(live_status)s"]
            user_agent = get_user_agent()
            if user_agent:
                cmd.extend(["--user-agent", user_agent])
            cmd.extend(get_cookie_args())
            cmd.extend(get_site_check_args(url))
            cmd.append(url)
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                **_subprocess_kwargs(),
            )
            stdout = r.stdout.strip().lower()
            stderr = r.stderr.lower()

            if r.returncode != 0:
                stderr_lower = stderr.lower()
                url_lower = url.lower()

                if "currently away" in stderr_lower:
                    return StreamStatus.AWAY, "🌙 Away"

                if "hidden session" in stderr_lower:
                    return StreamStatus.PRIVATE, "🔒 Hidden session (private)"

                if "private" in stderr_lower:
                    return StreamStatus.PRIVATE, "🔒 Private show"

                if "age restricted" in stderr_lower or "age-restricted" in stderr_lower:
                    return StreamStatus.PRIVATE, "🔒 Age restricted"

                if "offline" in stderr_lower:
                    return StreamStatus.OFFLINE, "💤 Offline"

                if "video unavailable" in stderr_lower or "not found" in stderr_lower:
                    return StreamStatus.OFFLINE, "💤 Stream not found"

                error_hint = format_error_message(stderr, stdout)
                print(f"[Debug] yt-dlp error for {url}: {error_hint}")
                return StreamStatus.ERROR, error_hint

            if stdout == "is_live":
                return StreamStatus.ONLINE, "🟢 LIVE"
            if stdout in ("was_live", "not_live", "post_live"):
                return StreamStatus.OFFLINE, "💤 Offline"
            if not stdout:
                return StreamStatus.ERROR, "❓ Unknown (empty response)"

            return StreamStatus.OFFLINE, f"💤 Unknown ({stdout})"

        except subprocess.TimeoutExpired:
            return StreamStatus.ERROR, "⏰ Timeout"
        except subprocess.SubprocessError:
            return StreamStatus.ERROR, "❌ Process error"
        except Exception as e:
            print(f"[Debug] Unexpected error in StreamChecker._check for {url}: {e!r}")
            traceback.print_exc()
            return StreamStatus.ERROR, "❌ Error"


# ─────────────────────────────────────────────
#  Duration label
# ─────────────────────────────────────────────

class DownloadTimer(QLabel):
    def __init__(self, parent=None):
        super().__init__("—", parent)
        self._start = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("font-family: 'Courier New', monospace; font-size: 11px; color: #aaa;")

    def start_timer(self):
        self._start = time.time()
        self._timer.start(1000)
        self._tick()

    def stop_timer(self):
        self._timer.stop()
        self.setText("—")
        self.setStyleSheet("font-family: 'Courier New', monospace; font-size: 11px; color: #aaa;")

    def _tick(self):
        e = int(time.time() - self._start)
        h, r = divmod(e, 3600)
        m, s = divmod(r, 60)
        self.setText(f"{h:02d}:{m:02d}:{s:02d}")
        self.setStyleSheet("font-family: 'Courier New', monospace; font-size: 11px; color: #4fc; font-weight: bold;")


# ─────────────────────────────────────────────
#  Main window
# ─────────────────────────────────────────────

DARK = """
QMainWindow, QWidget          { background: #1a1a1f; color: #e0e0e0; }
QSplitter::handle             { background: #2a2a32; }

/* Table */
QTableWidget                  { background: #1e1e26; gridline-color: #2d2d38;
                                 border: 1px solid #2d2d38; border-radius: 6px; }
QTableWidget::item            { padding: 4px 8px; border-bottom: 1px solid #25252f; }
QTableWidget::item:selected   { background: #2a3a5e; color: #fff; }
QHeaderView::section          { background: #16161c; color: #888; font-size: 11px;
                                 font-weight: 600; text-transform: uppercase;
                                 letter-spacing: 0.05em; padding: 6px 8px;
                                 border: none; border-bottom: 1px solid #2d2d38; }
QTableWidget QScrollBar:vertical   { background: #1a1a1f; width: 8px; }
QTableWidget QScrollBar::handle:vertical { background: #3a3a48; border-radius: 4px; }

/* Inputs */
QLineEdit   { background: #23232c; border: 1px solid #35354a; border-radius: 5px;
              padding: 6px 10px; color: #e0e0e0; font-size: 13px; }
QLineEdit:focus { border-color: #4a7bff; }

/* Buttons — base */
QPushButton { background: #26262f; border: 1px solid #35354a; border-radius: 5px;
              padding: 6px 10px; color: #ccc; font-size: 12px; min-height: 32px; }
QPushButton:hover   { background: #2e2e3a; color: #fff; border-color: #4a5570; }
QPushButton:pressed { background: #1e1e28; }
QPushButton:disabled{ background: #1e1e24; color: #444; border-color: #2a2a35; }

/* Accent buttons */
QPushButton#addBtn  { background: #1e3a5f; border-color: #2a5a9f; color: #7ab4ff; }
QPushButton#addBtn:hover { background: #234878; color: #acd0ff; }
QPushButton#stopAllBtn { background: #3a1e1e; border-color: #7f2a2a; color: #ff7a7a; }
QPushButton#stopAllBtn:hover { background: #4a2020; color: #ffaaaa; }
QPushButton#startBtn { background: #1e3a2a; border-color: #2a7f4a; color: #7affaa; padding: 6px 12px; }
QPushButton#startBtn:hover { background: #234838; }
QPushButton#stopBtn  { background: #3a1e1e; border-color: #7f2a2a; color: #ff8888; padding: 6px 12px; }
QPushButton#stopBtn:hover { background: #4a2222; }
QPushButton#removeBtn{ color: #888; padding: 6px 10px; }
QPushButton#removeBtn:hover { color: #ff6666; border-color: #7f2a2a; }

/* Log */
QTextEdit { background: #13131a; border: 1px solid #2a2a38; border-radius: 6px;
            padding: 4px; font-family: 'Courier New', monospace; font-size: 12px; }

/* Checkbox */
QCheckBox { color: #aaa; spacing: 6px; }
QCheckBox::indicator { width: 14px; height: 14px; border-radius: 3px;
                        border: 1px solid #45455a; background: #23232c; }
QCheckBox::indicator:checked { background: #2a6bff; border-color: #4a8bff; }

/* Status bar */
QStatusBar { background: #13131a; color: #666; font-size: 11px;
             border-top: 1px solid #2a2a38; }

/* Labels */
QLabel#sectionLabel { color: #555; font-size: 11px; font-weight: 600;
                       text-transform: uppercase; letter-spacing: 0.08em; }
QFrame#divider { background: #2a2a38; }
"""

STATUS_STYLE = {
    StreamStatus.ONLINE:  ("🟢 LIVE",    "#4fc", "#1a3a1a"),
    StreamStatus.OFFLINE: ("⚫ Offline",  "#666", "#1a1a1a"),
    StreamStatus.PRIVATE: ("🔒 Private", "#fa0", "#3a2a0a"),
    StreamStatus.AWAY:    ("🌙 Away",    "#a78", "#2a1f2a"),
    StreamStatus.ERROR:   ("⚠ Error",   "#f66", "#3a1a1a"),
}


# ─────────────────────────────────────────────
#  Persistence
# ─────────────────────────────────────────────

SAVE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "streams.json")


def load_saved_streams() -> dict:
    try:
        with open(SAVE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            streams = [
                {
                    "url": e["url"],
                    "auto_start": bool(e.get("auto_start", False)),
                }
                for e in data.get("streams", [])
                if isinstance(e, dict) and e.get("url")
            ]
            settings = data.get("settings", {})
            return {"streams": streams, "settings": settings}

        if isinstance(data, list):
            streams = [
                {
                    "url": e["url"],
                    "auto_start": bool(e.get("auto_start", False)),
                }
                for e in data
                if isinstance(e, dict) and e.get("url")
            ]
            settings = {
                "user_agent": data[0].get("user_agent", "") if data else "",
                "output_folder": data[0].get("output_folder", "") if data else "",
                "max_resolution": data[0].get("max_resolution", "") if data else "",
                "max_fps": data[0].get("max_fps", "") if data else "",
                "browser": data[0].get("browser", "") if data else "",
            }
            return {"streams": streams, "settings": settings}
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return {"streams": [], "settings": {}}


def save_streams(stream_items: dict) -> None:
    payload = {
        "settings": {
            "user_agent": USER_AGENT,
            "output_folder": DOWNLOAD_OUTPUT,
            "max_resolution": MAX_DOWNLOAD_RESOLUTION,
            "max_fps": MAX_DOWNLOAD_FPS,
            "auto_timeout_seconds": AUTO_DOWNLOAD_DISABLE_SECONDS,
            "auto_reenable_cooldown_seconds": AUTO_REENABLE_COOLDOWN_SECONDS,
            "browser": BROWSER,
            "browser_display": BROWSER_DISPLAY,
            "cookies_file": COOKIES_FILE,
        },
        "streams": [
            {"url": item.url, "auto_start": item.auto_start}
            for item in stream_items.values()
        ],
    }
    try:
        with open(SAVE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except OSError:
        pass


# ─────────────────────────────────────────────
#  Settings dialog
# ─────────────────────────────────────────────

_BROWSER_CHOICES = ["firefox", "chrome", "chromium", "edge", "brave", "opera", "safari"]
_CHROMIUM_BROWSERS = {"chrome", "chromium", "edge", "brave", "opera"}
# Polished display names for the dropdown -- yt-dlp itself only accepts the
# lowercase values in _BROWSER_CHOICES above, so these are translated back
# and forth rather than used as the actual --cookies-from-browser value.
_BROWSER_DISPLAY_NAMES = {
    "chrome": "Chrome", "chromium": "Chromium", "edge": "Edge",
    "brave": "Brave", "opera": "Opera", "safari": "Safari",
}
_BROWSER_DISPLAY_TO_REAL = {v: k for k, v in _BROWSER_DISPLAY_NAMES.items()}

# Firefox-based forks yt-dlp doesn't recognize by name. Base folders below
# are confirmed against real installs on Linux + Windows; Mac entries are
# inferred from the same per-browser naming convention (unverified on real
# hardware -- detection just fails gracefully to manual Browse if wrong).
_FIREFOX_FORK_BASE_DIRS = {
    "Floorp": {
        "linux": lambda: [os.path.expanduser("~/.floorp")],
        "win32": lambda: [os.path.join(os.environ.get("APPDATA", ""), "Floorp")],
        "darwin": lambda: [os.path.expanduser("~/Library/Application Support/Floorp")],
    },
    "Zen": {
        "linux": lambda: [os.path.expanduser("~/.config/zen")],
        "win32": lambda: [os.path.join(os.environ.get("APPDATA", ""), "zen")],
        "darwin": lambda: [os.path.expanduser("~/Library/Application Support/zen")],
    },
    "LibreWolf": {
        # Confirmed on Arch's official pacman package: nested one level
        # deeper (~/.config/librewolf/librewolf) than Zen's flat layout --
        # try that first, then fall back to the flat layout other install
        # methods (official tarball, other distros) may use instead.
        "linux": lambda: [
            os.path.expanduser("~/.config/librewolf/librewolf"),
            os.path.expanduser("~/.config/librewolf"),
        ],
        "win32": lambda: [os.path.join(os.environ.get("APPDATA", ""), "librewolf")],
        "darwin": lambda: [os.path.expanduser("~/Library/Application Support/librewolf")],
    },
}
_BROWSER_TOP_CHOICES = ["Firefox-based"] + [_BROWSER_DISPLAY_NAMES[c] for c in _BROWSER_CHOICES if c != "firefox"]
_FIREFOX_VARIANT_CHOICES = ["Firefox"] + list(_FIREFOX_FORK_BASE_DIRS) + ["Other (browse manually)"]


def _read_ini_sections(path: str) -> dict:
    sections, current = {}, None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            m = re.match(r"^\[(.+)\]$", line)
            if m:
                current = m.group(1)
                sections[current] = {}
            elif current and "=" in line:
                k, _, v = line.partition("=")
                sections[current][k.strip()] = v.strip()
    return sections


def _resolve_default_profile(base_dir: str) -> str:
    """Parse profiles.ini (+ installs.ini) the way Firefox-based browsers
    do, preferring an install-specific default over the plain Default=1
    flag -- confirmed necessary against real Zen/LibreWolf installs, where
    they disagree."""
    ini_path = os.path.join(base_dir, "profiles.ini")
    if not os.path.isfile(ini_path):
        return ""
    sections = _read_ini_sections(ini_path)
    installs_path = os.path.join(base_dir, "installs.ini")
    if os.path.isfile(installs_path):
        for key, values in _read_ini_sections(installs_path).items():
            sections[f"Install_{key}"] = values

    target = None
    for key, values in sections.items():
        if key.startswith("Install"):
            target = values.get("Default") or target
    if not target:
        for key, values in sections.items():
            if key.startswith("Profile") and values.get("Default") == "1":
                target = values.get("Path")
    if not target:
        return ""
    full = os.path.join(base_dir, target)
    return full if os.path.isdir(full) else ""


def _detect_fork_default_profile(fork_name: str) -> str:
    platform_key = "win32" if sys.platform == "win32" else "darwin" if sys.platform == "darwin" else "linux"
    getter = _FIREFOX_FORK_BASE_DIRS.get(fork_name, {}).get(platform_key)
    if not getter:
        return ""
    for base_dir in getter():
        if base_dir and os.path.isdir(base_dir):
            resolved = _resolve_default_profile(base_dir)
            if resolved:
                return resolved
    return ""


_RESOLUTION_CHOICES = ["640x360", "960x540", "1280x720", "1920x1080", "3840x2160"]
_FPS_CHOICES = ["30", "60"]
_AUTO_TIMEOUT_VALUE_CHOICES = ["2", "5", "10", "15", "20", "30", "Custom"]
_AUTO_TIMEOUT_UNIT_CHOICES = ["seconds", "minutes"]
_AUTO_TIMEOUT_PRESETS = [c for c in _AUTO_TIMEOUT_VALUE_CHOICES if c != "Custom"]


def _seconds_to_value_unit(seconds: int) -> tuple[str, str, int]:
    """Best-effort mapping of a stored AUTO_DOWNLOAD_DISABLE_SECONDS value
    back to a (display_value, unit, numeric_value) dropdown triple. Values
    that don't match a preset round-trip exactly via "Custom" + the numeric
    value (for the custom spin box), instead of being lost. Callers handle
    seconds <= 0 (the "Off" checkbox) separately."""
    if seconds % 60 == 0 and str(seconds // 60) in _AUTO_TIMEOUT_PRESETS:
        return str(seconds // 60), "minutes", seconds // 60
    if str(seconds) in _AUTO_TIMEOUT_PRESETS:
        return str(seconds), "seconds", seconds
    if seconds > 0 and seconds % 60 == 0:
        return "Custom", "minutes", seconds // 60
    return "Custom", "seconds", max(seconds, 1)


_COOLDOWN_VALUE_CHOICES = ["5", "10", "15", "30", "60", "Custom"]
_COOLDOWN_UNIT_CHOICES = ["minutes", "hours"]
_COOLDOWN_PRESETS = [c for c in _COOLDOWN_VALUE_CHOICES if c != "Custom"]


def _seconds_to_cooldown_value_unit(seconds: int) -> tuple[str, str, int]:
    """Same idea as _seconds_to_value_unit, but for the auto-re-enable
    cooldown's longer scale (minutes/hours instead of seconds/minutes)."""
    if seconds % 3600 == 0 and str(seconds // 3600) in _COOLDOWN_PRESETS:
        return str(seconds // 3600), "hours", seconds // 3600
    if seconds % 60 == 0 and str(seconds // 60) in _COOLDOWN_PRESETS:
        return str(seconds // 60), "minutes", seconds // 60
    if seconds % 3600 == 0:
        return "Custom", "hours", seconds // 3600
    if seconds % 60 == 0:
        return "Custom", "minutes", seconds // 60
    return "Custom", "minutes", max(seconds // 60, 1)


class SettingsDialog(QDialog):
    """Setup-once config — output folder, cookies, User-Agent — lives here
    instead of the main toolbar, so day-to-day use (paste a URL, go) stays
    uncluttered."""

    settings_changed = Signal()
    cookie_access_confirmed_broken = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)
        self._cookie_probe_cache: dict[str, bool] = {}  # real_browser -> accessible
        self._cookie_probe_workers: Dict[str, CookieProbeWorker] = {}  # real_browser -> in-flight worker

        form = QFormLayout(self)

        # ── Download output ──
        self.out_input = QLineEdit(DOWNLOAD_OUTPUT)
        self.out_input.setToolTip("Folder where downloads will be saved")
        out_browse = QPushButton("Browse…")
        out_browse.clicked.connect(self._browse_output)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_input)
        out_row.addWidget(out_browse)
        form.addRow("Download output:", out_row)

        # ── Video quality ──
        self.resolution_combo = QComboBox()
        self.resolution_combo.addItems(_RESOLUTION_CHOICES)
        self.fps_combo = QComboBox()
        self.fps_combo.addItems(_FPS_CHOICES)
        quality_row = QHBoxLayout()
        quality_row.addWidget(self.resolution_combo)
        quality_row.addWidget(QLabel("max,"))
        quality_row.addWidget(self.fps_combo)
        quality_row.addWidget(QLabel("fps max"))
        quality_row.addStretch(1)
        form.addRow("Video quality:", quality_row)

        # ── Auto-restart short-download protection ──
        self.auto_timeout_value_combo = QComboBox()
        self.auto_timeout_value_combo.addItems(_AUTO_TIMEOUT_VALUE_CHOICES)
        self.auto_timeout_custom_spin = QSpinBox()
        self.auto_timeout_custom_spin.setRange(1, 999)
        self.auto_timeout_custom_spin.setVisible(False)
        self.auto_timeout_unit_combo = QComboBox()
        self.auto_timeout_unit_combo.addItems(_AUTO_TIMEOUT_UNIT_CHOICES)
        self.auto_timeout_off_check = QCheckBox("Off")
        timeout_row = QHBoxLayout()
        timeout_row.addWidget(self.auto_timeout_value_combo)
        timeout_row.addWidget(self.auto_timeout_custom_spin)
        timeout_row.addWidget(self.auto_timeout_unit_combo)
        timeout_row.addWidget(self.auto_timeout_off_check)
        timeout_row.addStretch(1)
        form.addRow("Disable auto-restart if shorter than:", timeout_row)

        timeout_hint = QLabel(
            "If a stream's auto-restarted recording ends before this much time has\n"
            "passed, auto-restart is temporarily disabled for it (protects against a\n"
            "restart-loop on flaky streams). Check \"Off\" to disable this protection entirely."
        )
        timeout_hint.setStyleSheet("color: #888; font-size: 11px;")
        form.addRow("", timeout_hint)

        # ── Auto re-enable cooldown ──
        self.cooldown_value_combo = QComboBox()
        self.cooldown_value_combo.addItems(_COOLDOWN_VALUE_CHOICES)
        self.cooldown_custom_spin = QSpinBox()
        self.cooldown_custom_spin.setRange(1, 999)
        self.cooldown_custom_spin.setVisible(False)
        self.cooldown_unit_combo = QComboBox()
        self.cooldown_unit_combo.addItems(_COOLDOWN_UNIT_CHOICES)
        self.cooldown_off_check = QCheckBox("Off")
        cooldown_row = QHBoxLayout()
        cooldown_row.addWidget(self.cooldown_value_combo)
        cooldown_row.addWidget(self.cooldown_custom_spin)
        cooldown_row.addWidget(self.cooldown_unit_combo)
        cooldown_row.addWidget(self.cooldown_off_check)
        cooldown_row.addStretch(1)
        form.addRow("Auto re-enable after:", cooldown_row)

        cooldown_hint = QLabel(
            "Once auto-restart gets disabled for a flaky stream (above), automatically\n"
            "re-enable it after this much time — gives the stream a chance to settle\n"
            "before retrying. \"Off\" (default) means re-checking Auto yourself instead."
        )
        cooldown_hint.setStyleSheet("color: #888; font-size: 11px;")
        form.addRow("", cooldown_hint)

        # ── Cookies (always on — no toggle, just where to read them from) ──
        self.browser_combo = QComboBox()
        self.browser_combo.addItems(_BROWSER_TOP_CHOICES)
        self.firefox_variant_combo = QComboBox()
        self.firefox_variant_combo.addItems(_FIREFOX_VARIANT_CHOICES)
        self._cookie_path = ""
        self._cookie_path_btn = QPushButton("Browse…")
        self._cookie_path_btn.clicked.connect(self._browse_cookies)
        cookie_row = QHBoxLayout()
        cookie_row.addWidget(self.browser_combo)
        cookie_row.addWidget(self.firefox_variant_combo)
        cookie_row.addWidget(self._cookie_path_btn, 1)
        form.addRow("Cookie source:", cookie_row)

        self.fork_detect_warning = QLabel(
            "⚠ Couldn't auto-detect this browser's profile on this system — use Browse to point at it manually."
        )
        self.fork_detect_warning.setStyleSheet("color: #FF9800; font-size: 11px;")
        self.fork_detect_warning.setVisible(False)
        form.addRow("", self.fork_detect_warning)

        cookie_hint = QLabel(
            "Cookies are always used — most streams need them now. Pick your browser —\n"
            "for Firefox-based, Floorp/Zen/LibreWolf auto-detect their profile with no\n"
            "path needed; pick \"Other\" there (or Browse for any other browser) to\n"
            "point at a profile folder directly. Leave on Browse for the default profile."
        )
        cookie_hint.setStyleSheet("color: #888; font-size: 11px;")
        form.addRow("", cookie_hint)

        # ── Cookies file fallback (Chromium browsers on Windows only) ──
        self._cookies_file = ""
        self._cookies_file_btn = QPushButton("Browse…")
        self._cookies_file_btn.clicked.connect(self._browse_cookies_file)
        self._cookies_file_clear_btn = QPushButton("✕")
        self._cookies_file_clear_btn.setFixedWidth(28)
        self._cookies_file_clear_btn.clicked.connect(self._clear_cookies_file)
        self._cookies_file_clear_btn.setVisible(False)
        cookies_file_row = QHBoxLayout()
        cookies_file_row.addWidget(self._cookies_file_btn, 1)
        cookies_file_row.addWidget(self._cookies_file_clear_btn)
        self._cookies_file_label = QLabel("Cookies file:")
        form.addRow(self._cookies_file_label, cookies_file_row)

        self._cookies_file_hint = QLabel(
            "Optional fallback for Chrome/Edge/Brave/Opera on Windows, where browser<br>"
            "cookie access can fail (a known Windows-only Chromium limitation). See<br>"
            "<a href=\"https://github.com/Tazir709/stream-monitor#-exporting-cookies\">"
            "Exporting cookies</a> in the README for how to get a cookies.txt file,<br>"
            "then select it here to use it instead of live browser extraction."
        )
        self._cookies_file_hint.setTextFormat(Qt.TextFormat.RichText)
        self._cookies_file_hint.setOpenExternalLinks(True)
        self._cookies_file_hint.setStyleSheet("color: #888; font-size: 11px;")
        form.addRow("", self._cookies_file_hint)

        # ── User-Agent ──
        self.user_agent_input = QLineEdit()
        self.user_agent_input.setPlaceholderText("e.g. Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...")
        form.addRow("User-Agent:", self.user_agent_input)

        ua_hint = QLabel(
            "Often needed to get past Cloudflare's Turnstile check. Find yours by searching\n"
            "\"what is my user agent\", or visit whatsmyua.info — use the same browser your\n"
            "cookies came from."
        )
        ua_hint.setStyleSheet("color: #888; font-size: 11px;")
        form.addRow("", ua_hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.close)
        buttons.accepted.connect(self.close)
        form.addRow(buttons)

        self._load_from_globals()

        # Live-apply on change, same pattern the rest of the app already uses
        self.out_input.textChanged.connect(self._on_output_changed)
        self.resolution_combo.currentTextChanged.connect(self._on_resolution_changed)
        self.fps_combo.currentTextChanged.connect(self._on_fps_changed)
        self.auto_timeout_value_combo.currentTextChanged.connect(self._on_auto_timeout_changed)
        self.auto_timeout_unit_combo.currentTextChanged.connect(self._on_auto_timeout_changed)
        self.auto_timeout_custom_spin.valueChanged.connect(self._on_auto_timeout_changed)
        self.auto_timeout_off_check.toggled.connect(self._on_auto_timeout_changed)
        self.cooldown_value_combo.currentTextChanged.connect(self._on_cooldown_changed)
        self.cooldown_unit_combo.currentTextChanged.connect(self._on_cooldown_changed)
        self.cooldown_custom_spin.valueChanged.connect(self._on_cooldown_changed)
        self.cooldown_off_check.toggled.connect(self._on_cooldown_changed)
        self.browser_combo.currentTextChanged.connect(self._on_cookie_source_changed)
        self.firefox_variant_combo.currentTextChanged.connect(self._on_firefox_variant_changed)
        self.user_agent_input.textChanged.connect(self._on_user_agent_changed)

    def _load_from_globals(self):
        self.out_input.setText(DOWNLOAD_OUTPUT)
        if MAX_DOWNLOAD_RESOLUTION in _RESOLUTION_CHOICES:
            self.resolution_combo.setCurrentText(MAX_DOWNLOAD_RESOLUTION)
        if str(MAX_DOWNLOAD_FPS) in _FPS_CHOICES:
            self.fps_combo.setCurrentText(str(MAX_DOWNLOAD_FPS))
        is_off = AUTO_DOWNLOAD_DISABLE_SECONDS <= 0
        self.auto_timeout_off_check.setChecked(is_off)
        value, unit, numeric = _seconds_to_value_unit(
            AUTO_DOWNLOAD_DISABLE_SECONDS if not is_off else 300
        )
        self.auto_timeout_value_combo.setCurrentText(value)
        self.auto_timeout_unit_combo.setCurrentText(unit)
        self.auto_timeout_value_combo.setEnabled(not is_off)
        self.auto_timeout_unit_combo.setEnabled(not is_off)
        if value == "Custom":
            self.auto_timeout_custom_spin.setValue(numeric)
        self.auto_timeout_custom_spin.setVisible(value == "Custom")
        self.auto_timeout_custom_spin.setEnabled(not is_off)
        is_cooldown_off = AUTO_REENABLE_COOLDOWN_SECONDS <= 0
        self.cooldown_off_check.setChecked(is_cooldown_off)
        c_value, c_unit, c_numeric = _seconds_to_cooldown_value_unit(
            AUTO_REENABLE_COOLDOWN_SECONDS if not is_cooldown_off else 1800
        )
        self.cooldown_value_combo.setCurrentText(c_value)
        self.cooldown_unit_combo.setCurrentText(c_unit)
        self.cooldown_value_combo.setEnabled(not is_cooldown_off)
        self.cooldown_unit_combo.setEnabled(not is_cooldown_off)
        if c_value == "Custom":
            self.cooldown_custom_spin.setValue(c_numeric)
        self.cooldown_custom_spin.setVisible(c_value == "Custom")
        self.cooldown_custom_spin.setEnabled(not is_cooldown_off)
        self.user_agent_input.setText(USER_AGENT)
        browser, _, path = BROWSER.partition(":")
        if browser == "firefox":
            self.browser_combo.setCurrentText("Firefox-based")
            if BROWSER_DISPLAY in _FIREFOX_FORK_BASE_DIRS:
                self.firefox_variant_combo.setCurrentText(BROWSER_DISPLAY)
            elif path:
                self.firefox_variant_combo.setCurrentText("Other (browse manually)")
            else:
                self.firefox_variant_combo.setCurrentText("Firefox")
        elif browser in _BROWSER_CHOICES:
            self.browser_combo.setCurrentText(_BROWSER_DISPLAY_NAMES.get(browser, browser))
        self._set_cookie_path(path)
        self._set_cookies_file(COOKIES_FILE)
        self._on_cookie_source_changed()

    def _on_output_changed(self, text: str):
        global DOWNLOAD_OUTPUT
        DOWNLOAD_OUTPUT = text.strip() or "Downloads"
        self.settings_changed.emit()

    def _on_resolution_changed(self, text: str):
        global MAX_DOWNLOAD_RESOLUTION
        MAX_DOWNLOAD_RESOLUTION = text
        self.settings_changed.emit()

    def _on_fps_changed(self, text: str):
        global MAX_DOWNLOAD_FPS
        MAX_DOWNLOAD_FPS = int(text)
        self.settings_changed.emit()

    def _on_auto_timeout_changed(self, _=None):
        global AUTO_DOWNLOAD_DISABLE_SECONDS
        is_off = self.auto_timeout_off_check.isChecked()
        unit = self.auto_timeout_unit_combo.currentText()
        selected = self.auto_timeout_value_combo.currentText()
        self.auto_timeout_value_combo.setEnabled(not is_off)
        self.auto_timeout_unit_combo.setEnabled(not is_off)
        self.auto_timeout_custom_spin.setVisible(selected == "Custom")
        self.auto_timeout_custom_spin.setEnabled(not is_off)
        if is_off:
            AUTO_DOWNLOAD_DISABLE_SECONDS = 0
        else:
            if selected == "Custom":
                value = self.auto_timeout_custom_spin.value()
            else:
                value = int(selected)
            AUTO_DOWNLOAD_DISABLE_SECONDS = value * 60 if unit == "minutes" else value
        self.settings_changed.emit()

    def _on_cooldown_changed(self, _=None):
        global AUTO_REENABLE_COOLDOWN_SECONDS
        is_off = self.cooldown_off_check.isChecked()
        unit = self.cooldown_unit_combo.currentText()
        selected = self.cooldown_value_combo.currentText()
        self.cooldown_value_combo.setEnabled(not is_off)
        self.cooldown_unit_combo.setEnabled(not is_off)
        self.cooldown_custom_spin.setVisible(selected == "Custom")
        self.cooldown_custom_spin.setEnabled(not is_off)
        if is_off:
            AUTO_REENABLE_COOLDOWN_SECONDS = 0
        else:
            value = self.cooldown_custom_spin.value() if selected == "Custom" else int(selected)
            AUTO_REENABLE_COOLDOWN_SECONDS = value * 3600 if unit == "hours" else value * 60
        self.settings_changed.emit()

    def _browse_output(self):
        # No parent, so the dialog doesn't inherit the app's dark
        # stylesheet — the global QLineEdit padding rule clips text in the
        # dialog's inline "new folder" rename editor otherwise, since its
        # row height isn't sized for that extra padding.
        folder = QFileDialog.getExistingDirectory(
            None, "Choose download output folder", self.out_input.text() or "."
        )
        if folder:
            self.out_input.setText(folder)

    def _set_cookie_path(self, path: str):
        """Drives the single Browse button's display: shows "Browse…" when
        no path is set, or the (elided, full path in the tooltip) path
        itself once one is — instead of a separate always-visible text box
        squashed next to the button."""
        self._cookie_path = (path or "").strip()
        if self._cookie_path:
            metrics = self._cookie_path_btn.fontMetrics()
            elided = metrics.elidedText(self._cookie_path, Qt.TextElideMode.ElideMiddle, 260)
            self._cookie_path_btn.setText(elided)
            self._cookie_path_btn.setToolTip(self._cookie_path)
        else:
            self._cookie_path_btn.setText("Browse…")
            self._cookie_path_btn.setToolTip("")

    def _browse_cookies(self):
        # Browser profile dirs are almost always dotfolders (~/.mozilla,
        # ~/.floorp, …). The native Linux picker hides those by default and
        # offers no way to reveal them, so use Qt's own dialog instead,
        # which we can tell to show hidden entries. No parent, for the same
        # stylesheet-leak reason as _browse_output above.
        dialog = QFileDialog(
            None, "Choose browser profile folder",
            self._cookie_path or os.path.expanduser("~"),
        )
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setFilter(dialog.filter() | QDir.Filter.Hidden)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected = dialog.selectedFiles()
            if selected:
                self._set_cookie_path(selected[0])
                self._on_cookie_source_changed()

    def _set_cookies_file(self, path: str):
        self._cookies_file = (path or "").strip()
        if self._cookies_file:
            metrics = self._cookies_file_btn.fontMetrics()
            elided = metrics.elidedText(self._cookies_file, Qt.TextElideMode.ElideMiddle, 220)
            self._cookies_file_btn.setText(elided)
            self._cookies_file_btn.setToolTip(self._cookies_file)
        else:
            self._cookies_file_btn.setText("Browse…")
            self._cookies_file_btn.setToolTip("")
        self._cookies_file_clear_btn.setVisible(bool(self._cookies_file) and self._cookies_file_label.isVisible())

    def _browse_cookies_file(self):
        # No parent, same stylesheet-leak reason as _browse_output/_browse_cookies.
        path, _ = QFileDialog.getOpenFileName(
            None, "Choose cookies.txt file",
            self._cookies_file or os.path.expanduser("~"),
            "Cookies file (*.txt);;All files (*)",
        )
        if path:
            self._set_cookies_file(path)
            self._on_cookie_source_changed()

    def _clear_cookies_file(self):
        self._set_cookies_file("")
        self._on_cookie_source_changed()

    def _set_cookies_file_row_visible(self, visible: bool):
        self._cookies_file_label.setVisible(visible)
        self._cookies_file_btn.setVisible(visible)
        self._cookies_file_hint.setVisible(visible)
        self._cookies_file_clear_btn.setVisible(visible and bool(self._cookies_file))

    def _maybe_probe_cookie_access(self, real_browser: str):
        if real_browser in self._cookie_probe_cache:
            return
        if real_browser in self._cookie_probe_workers:
            return  # already probing this browser, don't orphan/overwrite it
        worker = CookieProbeWorker(real_browser)
        worker.result_signal.connect(self._on_cookie_probe_result)
        worker.finished.connect(lambda: self._cookie_probe_workers.pop(real_browser, None))
        self._cookie_probe_workers[real_browser] = worker
        worker.start()

    def _on_cookie_probe_result(self, real_browser: str, accessible: bool):
        self._cookie_probe_cache[real_browser] = accessible
        top = self.browser_combo.currentText()
        if _BROWSER_DISPLAY_TO_REAL.get(top, top) != real_browser:
            return  # user switched away before this finished -- stale, ignore
        if not accessible:
            self._set_cookies_file_row_visible(True)
            self.cookie_access_confirmed_broken.emit()

    def _on_firefox_variant_changed(self, variant):
        if variant in _FIREFOX_FORK_BASE_DIRS:
            self._set_cookie_path(_detect_fork_default_profile(variant))
        else:
            # "Firefox" (auto-resolved by yt-dlp) or "Other" (needs a fresh
            # manual pick) -- don't carry over a previous variant's path.
            self._set_cookie_path("")
        self._on_cookie_source_changed()

    def _on_cookie_source_changed(self, _=None):
        global BROWSER, BROWSER_DISPLAY, COOKIES_FILE
        COOKIES_FILE = self._cookies_file
        top = self.browser_combo.currentText()
        is_firefox_based = (top == "Firefox-based")
        self.firefox_variant_combo.setVisible(is_firefox_based)

        if not is_firefox_based:
            # A plain, directly-supported browser -- yt-dlp resolves its
            # profile on its own, nothing for the user to see or do.
            self.fork_detect_warning.setVisible(False)
            self._cookie_path_btn.setVisible(False)
            real_browser = _BROWSER_DISPLAY_TO_REAL.get(top, top)
            is_chromium_windows = sys.platform == "win32" and real_browser in _CHROMIUM_BROWSERS
            if is_chromium_windows and self._cookies_file:
                # Already configured (e.g. restored from streams.json) --
                # always show it regardless of probe state, so it stays
                # manageable/clearable.
                self._set_cookies_file_row_visible(True)
            elif is_chromium_windows and self._cookie_probe_cache.get(real_browser) is False:
                self._set_cookies_file_row_visible(True)
            else:
                self._set_cookies_file_row_visible(False)
            if is_chromium_windows and not self._cookies_file:
                self._maybe_probe_cookie_access(real_browser)
            BROWSER_DISPLAY = ""
            BROWSER = f"{real_browser}:{self._cookie_path}" if self._cookie_path else real_browser
            self.settings_changed.emit()
            return

        self._set_cookies_file_row_visible(False)
        variant = self.firefox_variant_combo.currentText()
        is_known_fork = variant in _FIREFOX_FORK_BASE_DIRS
        is_other = variant == "Other (browse manually)"
        detection_failed = is_known_fork and not self._cookie_path

        self.fork_detect_warning.setVisible(detection_failed)
        # Only show the button when the user actually has to do something:
        # they picked "Other", or a known fork's auto-detection failed.
        # Plain "Firefox" and a successfully-resolved fork show nothing.
        self._cookie_path_btn.setVisible(is_other or detection_failed)

        BROWSER_DISPLAY = variant if is_known_fork else ""
        BROWSER = f"firefox:{self._cookie_path}" if self._cookie_path else "firefox"
        self.settings_changed.emit()

    def _on_user_agent_changed(self, text: str):
        global USER_AGENT
        USER_AGENT = text.strip()
        self.settings_changed.emit()


class StreamDownloaderGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Stream Download Manager")
        self.setGeometry(100, 100, 920, 780)
        self.setMinimumSize(1020, 600)

        # Workers
        self.preview_worker = SharedPreviewWorker()
        self.preview_worker.setParent(self)
        self.preview_worker.preview_updated.connect(self._on_preview)
        self.preview_worker.finished.connect(lambda: print("[Debug] SharedPreviewWorker finished"))
        self.preview_worker.finished.connect(self.preview_worker.deleteLater)
        self.preview_worker.destroyed.connect(lambda: print("[Debug] SharedPreviewWorker destroyed"))
        self.preview_worker.start()

        self.checker = StreamChecker()
        self.checker.setParent(self)
        self.checker.status_signal.connect(self._on_status)
        self.checker.finished.connect(lambda: print("[Debug] StreamChecker finished"))
        self.checker.finished.connect(self.checker.deleteLater)
        self.checker.destroyed.connect(lambda: print("[Debug] StreamChecker destroyed"))
        self.checker.start()

        self.download_workers: Dict[str, DownloadWorker] = {}
        self.download_timers: Dict[str, DownloadTimer]   = {}
        self.stream_items: Dict[str, StreamItem]         = {}
        self.auto_checkboxes: Dict[str, QCheckBox] = {}
        self._auto_disabled: Dict[str, str] = {}  # url -> username, for the disabled-auto-restart banner
        self._reenable_timers: Dict[str, QTimer] = {}
        self._shown_chromium_cookie_dialog = False

        # Process health check
        self._proc_timer = QTimer(self)
        self._proc_timer.timeout.connect(self._check_processes)
        self._proc_timer.start(5000)

        self._settings_dialog = SettingsDialog(self)
        self._settings_dialog.settings_changed.connect(self._save)
        self._settings_dialog.cookie_access_confirmed_broken.connect(self._maybe_show_chromium_cookie_dialog)

        # Convenience references so the rest of the class can keep reading
        # these the same way it always has, without caring that they now
        # live inside the settings dialog instead of the main toolbar.
        self._out_input = self._settings_dialog.out_input
        self._user_agent_input = self._settings_dialog.user_agent_input

        self._build_ui()
        self.setStyleSheet(DARK)
        self._load_saved_streams()

        QApplication.instance().aboutToQuit.connect(self._shutdown_workers)

    # ── Persistence ─────────────────────────────

    def _load_saved_streams(self):
        saved = load_saved_streams()
        if not saved:
            return
        settings = saved.get("settings", {})

        saved_user_agent = str(settings.get("user_agent", "") or "").strip()
        if saved_user_agent:
            # Restore the last-used UA across sessions — but only if there
            # actually was one saved, so an empty entry (e.g. from before
            # a UA was ever configured) doesn't clobber a real config value.
            global USER_AGENT
            USER_AGENT = saved_user_agent
            if self._user_agent_input:
                self._user_agent_input.setText(USER_AGENT)

        saved_output = str(settings.get("output_folder", "") or "").strip()
        if saved_output:
            global DOWNLOAD_OUTPUT
            DOWNLOAD_OUTPUT = saved_output
            if self._out_input:
                self._out_input.setText(DOWNLOAD_OUTPUT)

        saved_resolution = str(settings.get("max_resolution", "") or "").strip()
        if saved_resolution in _RESOLUTION_CHOICES:
            global MAX_DOWNLOAD_RESOLUTION
            MAX_DOWNLOAD_RESOLUTION = saved_resolution
            self._settings_dialog.resolution_combo.setCurrentText(saved_resolution)

        saved_fps = str(settings.get("max_fps", "") or "").strip()
        if saved_fps in _FPS_CHOICES:
            global MAX_DOWNLOAD_FPS
            MAX_DOWNLOAD_FPS = int(saved_fps)
            self._settings_dialog.fps_combo.setCurrentText(saved_fps)

        saved_auto_timeout = settings.get("auto_timeout_seconds", None)
        if saved_auto_timeout is not None:
            try:
                saved_auto_timeout = int(saved_auto_timeout)
            except (TypeError, ValueError):
                saved_auto_timeout = None
        if saved_auto_timeout is not None:
            global AUTO_DOWNLOAD_DISABLE_SECONDS
            AUTO_DOWNLOAD_DISABLE_SECONDS = saved_auto_timeout
            is_off = AUTO_DOWNLOAD_DISABLE_SECONDS <= 0
            self._settings_dialog.auto_timeout_off_check.setChecked(is_off)
            value, unit, numeric = _seconds_to_value_unit(
                AUTO_DOWNLOAD_DISABLE_SECONDS if not is_off else 300
            )
            self._settings_dialog.auto_timeout_value_combo.setCurrentText(value)
            self._settings_dialog.auto_timeout_unit_combo.setCurrentText(unit)
            self._settings_dialog.auto_timeout_value_combo.setEnabled(not is_off)
            self._settings_dialog.auto_timeout_unit_combo.setEnabled(not is_off)
            if value == "Custom":
                self._settings_dialog.auto_timeout_custom_spin.setValue(numeric)
            self._settings_dialog.auto_timeout_custom_spin.setVisible(value == "Custom")
            self._settings_dialog.auto_timeout_custom_spin.setEnabled(not is_off)

        saved_cooldown = settings.get("auto_reenable_cooldown_seconds", None)
        if saved_cooldown is not None:
            try:
                saved_cooldown = int(saved_cooldown)
            except (TypeError, ValueError):
                saved_cooldown = None
        if saved_cooldown is not None:
            global AUTO_REENABLE_COOLDOWN_SECONDS
            AUTO_REENABLE_COOLDOWN_SECONDS = saved_cooldown
            is_cooldown_off = AUTO_REENABLE_COOLDOWN_SECONDS <= 0
            self._settings_dialog.cooldown_off_check.setChecked(is_cooldown_off)
            c_value, c_unit, c_numeric = _seconds_to_cooldown_value_unit(
                AUTO_REENABLE_COOLDOWN_SECONDS if not is_cooldown_off else 1800
            )
            self._settings_dialog.cooldown_value_combo.setCurrentText(c_value)
            self._settings_dialog.cooldown_unit_combo.setCurrentText(c_unit)
            self._settings_dialog.cooldown_value_combo.setEnabled(not is_cooldown_off)
            self._settings_dialog.cooldown_unit_combo.setEnabled(not is_cooldown_off)
            if c_value == "Custom":
                self._settings_dialog.cooldown_custom_spin.setValue(c_numeric)
            self._settings_dialog.cooldown_custom_spin.setVisible(c_value == "Custom")
            self._settings_dialog.cooldown_custom_spin.setEnabled(not is_cooldown_off)

        saved_cookies_file = str(settings.get("cookies_file", "") or "").strip()
        if saved_cookies_file and os.path.isfile(saved_cookies_file):
            global COOKIES_FILE
            COOKIES_FILE = saved_cookies_file
            self._settings_dialog._set_cookies_file(saved_cookies_file)

        saved_browser = str(settings.get("browser", "") or "").strip()
        saved_browser_display = str(settings.get("browser_display", "") or "").strip()
        if saved_browser:
            browser, _, path = saved_browser.partition(":")
            if browser in _BROWSER_CHOICES:
                if not path or os.path.isdir(path):
                    global BROWSER, BROWSER_DISPLAY
                    BROWSER = saved_browser
                    BROWSER_DISPLAY = saved_browser_display if saved_browser_display in _FIREFOX_FORK_BASE_DIRS else ""
                    if browser == "firefox":
                        self._settings_dialog.browser_combo.setCurrentText("Firefox-based")
                        if BROWSER_DISPLAY:
                            self._settings_dialog.firefox_variant_combo.setCurrentText(BROWSER_DISPLAY)
                        elif path:
                            self._settings_dialog.firefox_variant_combo.setCurrentText("Other (browse manually)")
                        else:
                            self._settings_dialog.firefox_variant_combo.setCurrentText("Firefox")
                    else:
                        self._settings_dialog.browser_combo.setCurrentText(_BROWSER_DISPLAY_NAMES.get(browser, browser))
                    self._settings_dialog._set_cookie_path(path)
                    self._settings_dialog._on_cookie_source_changed()

        for entry in saved.get("streams", []):
            url = entry["url"]
            auto = entry["auto_start"]
            self._add_stream(url=url, auto_start=auto, silent=True)
        if saved:
            restored_count = len(saved.get("streams", []))
            if restored_count:
                self._log_msg(
                    f"📂 Restored {restored_count} stream(s) from last session",
                    "#888"
                )

    def _save(self):
        save_streams(self.stream_items)

    def _open_settings(self):
        self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()

    def _maybe_show_chromium_cookie_dialog(self):
        if not self._shown_chromium_cookie_dialog:
            self._shown_chromium_cookie_dialog = True
            self._show_chromium_cookie_dialog()

    def _show_chromium_cookie_dialog(self):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Chromium Cookie Access Issue")
        box.setText(
            "Chrome and other Chromium-based browsers (Edge, Brave, Opera) can fail "
            "to give yt-dlp reliable cookie access on Windows — this is a known "
            "limitation in Chrome itself, not a bug in Stream Monitor.\n\n"
            "Click below for how to fix it: export your cookies to a file, or "
            "switch to a Firefox-based browser."
        )
        open_btn = box.addButton("Open Troubleshooting Guide", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() == open_btn:
            QDesktopServices.openUrl(QUrl("https://github.com/Tazir709/stream-monitor#-troubleshooting"))

    # ── UI construction ─────────────────────────

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        vbox = QVBoxLayout(root)
        vbox.setContentsMargins(12, 12, 12, 8)
        vbox.setSpacing(10)

        # ── Top bar ──
        bar = QHBoxLayout()
        bar.setSpacing(8)

        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("https://chaturbate.com/username/")
        self._url_input.returnPressed.connect(self._add_stream)
        bar.addWidget(self._url_input, 4)

        settings_btn = QPushButton("⚙  Settings")
        settings_btn.setObjectName("settingsBtn")
        settings_btn.clicked.connect(self._open_settings)
        bar.addWidget(settings_btn)

        add_btn = QPushButton("＋  Add Stream")
        add_btn.setObjectName("addBtn")
        add_btn.clicked.connect(self._add_stream)
        bar.addWidget(add_btn)

        stop_all_btn = QPushButton("⏹  Stop All")
        stop_all_btn.setObjectName("stopAllBtn")
        stop_all_btn.clicked.connect(self._stop_all)
        bar.addWidget(stop_all_btn)

        vbox.addLayout(bar)

        # ── Auto-restart-disabled banner (hidden until it has something to say) ──
        self._auto_disabled_banner = QLabel()
        self._auto_disabled_banner.setWordWrap(True)
        self._auto_disabled_banner.setStyleSheet(
            "background:#3a2a10; color:#FF9800; font-size:12px; font-weight:bold;"
            "padding:6px 10px; border-radius:4px; border:1px solid #FF9800;"
        )
        self._auto_disabled_banner.setVisible(False)
        vbox.addWidget(self._auto_disabled_banner)

        # ── Splitter: table / log ──
        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)

        # Table
        self._table = QTableWidget()
        self._table.setColumnCount(12)  # Was 11
        self._table.setHorizontalHeaderLabels([
            "Preview", "Site", "Username", "Status", "Duration",
            "Res", "FPS", "Auto", "DL", "Start", "Stop", "✕",
        ])
        # After setting the header labels, update the column settings:

        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Fixed)   # Preview
        hh.setSectionResizeMode(1, QHeaderView.Fixed)   # Site  <-- NEW
        hh.setSectionResizeMode(2, QHeaderView.Stretch) # Username
        hh.setSectionResizeMode(3, QHeaderView.Fixed)   # Status
        hh.setSectionResizeMode(4, QHeaderView.Fixed)   # Duration
        hh.setSectionResizeMode(5, QHeaderView.Fixed)   # Res
        hh.setSectionResizeMode(6, QHeaderView.Fixed)   # FPS  <-- NEW
        hh.setSectionResizeMode(7, QHeaderView.Fixed)   # Auto
        hh.setSectionResizeMode(8, QHeaderView.Fixed)   # DL
        hh.setSectionResizeMode(9, QHeaderView.Fixed)   # Start
        hh.setSectionResizeMode(10, QHeaderView.Fixed)  # Stop
        hh.setSectionResizeMode(11, QHeaderView.Fixed)  # ✕

        self._table.setColumnWidth(0, 112)   # Preview
        self._table.setColumnWidth(1, 90)    # Site  <-- NEW
        self._table.setColumnWidth(3, 90)    # Status
        self._table.setColumnWidth(4, 90)    # Duration
        self._table.setColumnWidth(5, 80)    # Res
        self._table.setColumnWidth(6, 48)    # FPS  <-- NEW
        self._table.setColumnWidth(7, 48)    # Auto
        self._table.setColumnWidth(8, 36)    # DL
        self._table.setColumnWidth(9, 85)    # Start
        self._table.setColumnWidth(10, 85)   # Stop
        self._table.setColumnWidth(11, 90)   # ✕
        self._table.verticalHeader().setDefaultSectionSize(64)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        splitter.addWidget(self._table)

        # Log panel
        log_container = QWidget()
        lc_layout = QVBoxLayout(log_container)
        lc_layout.setContentsMargins(0, 0, 0, 0)
        lc_layout.setSpacing(4)

        log_label = QLabel("Activity Log")
        log_label.setObjectName("sectionLabel")
        lc_layout.addWidget(log_label)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        lc_layout.addWidget(self._log)

        splitter.addWidget(log_container)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        vbox.addWidget(splitter)

        self._download_info_label = QLabel("Download info: idle")
        self._download_info_label.setObjectName("sectionLabel")
        self._download_info_label.setStyleSheet(
            "font-size:11px; color:#8fb3ff; padding:4px 0 0 0;"
        )
        vbox.addWidget(self._download_info_label)

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Ready")
        self._update_download_info()

        # Tooltips
        self._url_input.setToolTip("Enter a Chaturbate stream URL\nExample: https://chaturbate.com/username/")
        settings_btn.setToolTip("Download output folder, cookies, and User-Agent")
        add_btn.setToolTip("Add the stream to the monitoring list")
        stop_all_btn.setToolTip("Stop ALL active downloads immediately")

    # ── Stream management ───────────────────────

    def _add_stream(self, url: str = "", auto_start: bool = False, silent: bool = False):
        if not url:
            url = self._url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Warning", "Please enter a URL")
            return
        if url in self.stream_items:
            if not silent:
                QMessageBox.warning(self, "Warning", f"'{extract_username(url)}' is already in the list")
            return

        username = extract_username(url)
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setRowHeight(row, 64)

        item = StreamItem(url=url, username=username, row=row, auto_start=auto_start)
        self.stream_items[url] = item

        # Col 0 — thumbnail
        thumb = QLabel()
        thumb.setFixedSize(100, 56)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setText("No preview")
        thumb.setStyleSheet(
            "background:#111118; color:#444; font-size:10px;"
            "border:1px solid #2a2a38; border-radius:4px;"
        )
        self._table.setCellWidget(row, 0, thumb)

        # Col 1 — site  <-- NEW
        site_lbl = QLabel(extract_site(url))
        site_lbl.setAlignment(Qt.AlignCenter)
        site_lbl.setToolTip(url)
        site_lbl.setStyleSheet("font-size:11px; color:#8a8aa8;")
        self._table.setCellWidget(row, 1, site_lbl)

        # Col 2 — username
        u_item = QTableWidgetItem(username)
        u_item.setToolTip(url)
        u_item.setForeground(QColor("#d0d0e8"))
        self._table.setItem(row, 2, u_item)

        # Col 3 — status
        status_lbl = QLabel("⏳ Checking")
        status_lbl.setAlignment(Qt.AlignCenter)
        status_lbl.setStyleSheet("font-size:11px; color:#666;")
        self._table.setCellWidget(row, 3, status_lbl)

        # Col 4 — duration
        dur = DownloadTimer()
        self._table.setCellWidget(row, 4, dur)
        self.download_timers[url] = dur

            # Col 5 — resolution
        res_lbl = QLabel("—")
        res_lbl.setAlignment(Qt.AlignCenter)
        res_lbl.setStyleSheet("font-size:11px; color:#555; font-family:'Courier New',monospace;")
        self._table.setCellWidget(row, 5, res_lbl)

        # Col 6 — FPS  <-- NEW
        fps_lbl = QLabel("—")
        fps_lbl.setAlignment(Qt.AlignCenter)
        fps_lbl.setStyleSheet("font-size:11px; color:#555; font-family:'Courier New',monospace;")
        self._table.setCellWidget(row, 6, fps_lbl)

        # Col 7 — auto-start
        auto_cb = QCheckBox()
        auto_cb.setChecked(auto_start)
        auto_cb.setToolTip("Automatically start downloading when this stream goes live")
        auto_cb.stateChanged.connect(lambda s, u=url: self._toggle_auto(u, bool(s)))
        self.auto_checkboxes[url] = auto_cb
        cb_wrap = QWidget()
        cb_lay = QHBoxLayout(cb_wrap)
        cb_lay.addWidget(auto_cb)
        cb_lay.setAlignment(Qt.AlignCenter)
        cb_lay.setContentsMargins(0, 0, 0, 0)
        self._table.setCellWidget(row, 7, cb_wrap)

        # Col 8 — DL indicator
        dl_lbl = QLabel("—")
        dl_lbl.setAlignment(Qt.AlignCenter)
        dl_lbl.setStyleSheet("color:#444; font-size:14px;")
        self._table.setCellWidget(row, 8, dl_lbl)

        # Col 9 — Start
        start_btn = QPushButton("▶ Start")
        start_btn.setObjectName("startBtn")
        start_btn.setToolTip("Manually start downloading this stream now")
        start_btn.clicked.connect(lambda _, u=url: self._manual_start(u))
        self._table.setCellWidget(row, 9, start_btn)

        # Col 10 — Stop
        stop_btn = QPushButton("⏹ Stop")
        stop_btn.setObjectName("stopBtn")
        stop_btn.setEnabled(False)
        stop_btn.setToolTip("Stop the current download for this stream")
        stop_btn.clicked.connect(lambda _, u=url: self._stop_download(u))
        self._table.setCellWidget(row, 10, stop_btn)

        # Col 11 — Remove
        rm_btn = QPushButton("Remove")
        rm_btn.setObjectName("removeBtn")
        rm_btn.setToolTip("Remove this stream from the list")
        rm_btn.clicked.connect(lambda _, u=url: self._remove_stream(u))
        self._table.setCellWidget(row, 11, rm_btn)

        self.checker.add_stream(url, force=True)
        self._log_msg(f"➕ Added {username}", "#4CAF50")
        self._save()
        if not silent:
            self._url_input.clear()

    def _toggle_auto(self, url: str, checked: bool):
        item = self.stream_items.get(url)
        if not item:
            return
        item.auto_start = checked
        self._save()
        self._log_msg(
            f"{'✅' if checked else '❌'} Auto-start {item.username}: {'ON' if checked else 'OFF'}",
            "#FF9800" if checked else "#777"
        )
        if checked:
            old = self._reenable_timers.pop(url, None)
            if old:
                old.stop()
            if self._auto_disabled.pop(url, None) is not None:
                self._update_auto_disabled_banner()
            if item.current_status == StreamStatus.ONLINE and not item.download_active:
                self._manual_start(url)

    def _manual_start(self, url: str):
        worker = self.download_workers.get(url)
        if worker and worker.isRunning():
            self._log_msg(f"⚠ Already downloading {self.stream_items[url].username}", "#FF9800")
            return
        item = self.stream_items.get(url)
        if not item:
            return

        worker = DownloadWorker(url, item.username, self._out_input.text().strip() or "Downloads")
        worker.setParent(self)
        worker.log_signal.connect(self._on_dl_log)
        worker.finished_signal.connect(self._on_dl_finished)
        worker.finished.connect(worker.deleteLater)
        worker.progress_signal.connect(self._on_dl_progress)
        worker.resolution_signal.connect(self._on_resolution)
        worker.auto_download_disabled_signal.connect(self._on_auto_download_disabled)
        worker.short_download_signal.connect(self._on_short_download)
        self.download_workers[url] = worker
        worker.start()
        item.download_active = True
        item.download_start_time = time.time()

        timer = self.download_timers.get(url)
        if timer:
            timer.start_timer()

        self._update_dl_ui(url, True)
        self._update_download_info()
        self._log_msg(f"🚀 Started: {item.username}", "#4CAF50")

    def _stop_download(self, url: str):
        worker = self.download_workers.get(url)
        if worker:
            worker.stop()
            worker.wait(3000)
            self._cleanup_download(url)

    def _stop_all(self):
        count = len(self.download_workers)
        for url in list(self.download_workers.keys()):
            self._stop_download(url)
        if count:
            self._log_msg(f"🛑 Stopped {count} download(s)", "#F44336")

    def _remove_stream(self, url: str):
        item = self.stream_items.get(url)
        if not item:
            return
        if url in self.download_workers:
            ans = QMessageBox.question(
                self, "Confirm Remove",
                f"'{item.username}' is currently downloading. Stop and remove?",
                QMessageBox.Yes | QMessageBox.No
            )
            if ans != QMessageBox.Yes:
                return
            self._stop_download(url)

        self.preview_worker.remove_url(url)
        self.checker.remove_stream(url)
        row = item.row
        self._table.removeRow(row)

        self.download_timers.pop(url, None)
        self.auto_checkboxes.pop(url, None)
        old_timer = self._reenable_timers.pop(url, None)
        if old_timer:
            old_timer.stop()
        if self._auto_disabled.pop(url, None) is not None:
            self._update_auto_disabled_banner()
        del self.stream_items[url]

        for u, si in self.stream_items.items():
            if si.row > row:
                si.row -= 1

        self._log_msg(f"➖ Removed {item.username}", "#F44336")
        self._save()

    # ── Callbacks ───────────────────────────────

    def _on_preview(self, url: str, pixmap: QPixmap):
        item = self.stream_items.get(url)
        if not item:
            return
        thumb = self._table.cellWidget(item.row, 0)
        if thumb and not pixmap.isNull():
            scaled = pixmap.scaled(thumb.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            thumb.setPixmap(scaled)
            thumb.setStyleSheet("border:1px solid #2a5a2a; border-radius:4px;")

    def _on_status(self, url: str, status: StreamStatus, message: str):
        item = self.stream_items.get(url)
        if not item:
            return

        old = item.current_status
        item.current_status = status
        item.last_check_time = time.time()

        if status == StreamStatus.ERROR and message == CHROMIUM_COOKIE_ERROR_MSG:
            self._maybe_show_chromium_cookie_dialog()

        # If the last download ended early, this fresh check tells us whether
        # it was a normal end (offline) or a flaky-connection drop (still online).
        if item.pending_short_check:
            item.pending_short_check = False
            if status == StreamStatus.ONLINE:
                self._on_auto_download_disabled(url)

        self.preview_worker.update_status(url, status)

        # Clear thumbnail when going offline
        if status != StreamStatus.ONLINE and old == StreamStatus.ONLINE:
            thumb = self._table.cellWidget(item.row, 0)
            if thumb:
                thumb.clear()
                thumb.setText("No preview")
                thumb.setStyleSheet(
                    "background:#111118; color:#444; font-size:10px;"
                    "border:1px solid #2a2a38; border-radius:4px;"
                )

        # Update status cell
        text, fg, bg = STATUS_STYLE.get(status, ("?", "#888", "#1a1a1a"))
        lbl = self._table.cellWidget(item.row, 3)
        if lbl:
            lbl.setText(text)
            lbl.setStyleSheet(
                f"font-size:11px; color:{fg}; background:{bg};"
                "border-radius:4px; padding:2px 6px;"
            )

        # Log only on change
        if status != old:
            colors = {
                StreamStatus.ONLINE:  "#4CAF50",
                StreamStatus.OFFLINE: "#777",
                StreamStatus.PRIVATE: "#FF9800",
                StreamStatus.AWAY:    "#a78baa",
                StreamStatus.ERROR:   "#F44336",
            }
            self._log_msg(
                f"{text}  {item.username}",
                colors.get(status, "#aaa")
            )

        # Auto-start / force-stop logic
        if status == StreamStatus.ONLINE and item.auto_start and not item.download_active:
            self._log_msg(f"🎬 Auto-start: {item.username}", "#4CAF50")
            self._manual_start(url)
        elif status in (StreamStatus.OFFLINE, StreamStatus.PRIVATE, StreamStatus.AWAY) and item.download_active:
            self._log_msg(f"🛑 Force stop: {item.username} ({status.value})", "#F44336")
            self._stop_download(url)

    def _on_resolution(self, url: str, res: str, fps: int):
        item = self.stream_items.get(url)
        if not item:
            return
        item.resolution = res
        item.fps = fps or 30

        # Set the Res column (col 5)
        res_lbl = self._table.cellWidget(item.row, 5)
        if res_lbl:
            if res:
                res_lbl.setText(res)
                res_lbl.setStyleSheet(
                    "font-size:11px; color:#9df; font-family:'Courier New',monospace;"
                )
            else:
                res_lbl.setText("n/a")
                res_lbl.setStyleSheet(
                    "font-size:11px; color:#555; font-family:'Courier New',monospace;"
                )

        # Set the FPS column (col 6) - NEW
        fps_lbl = self._table.cellWidget(item.row, 6)
        if fps_lbl:
            if fps and fps > 0:
                fps_lbl.setText(f"{fps}")
                fps_lbl.setStyleSheet(
                    "font-size:11px; color:#9df; font-family:'Courier New',monospace;"
                )
            else:
                fps_lbl.setText("—")
                fps_lbl.setStyleSheet(
                    "font-size:11px; color:#555; font-family:'Courier New',monospace;"
                )

        self._update_download_info()

    def _on_dl_log(self, username: str, message: str):
        self._log_msg(f"[{username}] {message}", "#5b9bd5")

    def _on_dl_progress(self, username: str, percent: int):
        self._status_bar.showMessage(f"{username}: {percent}%", 1500)

    def _on_short_download(self, url: str, elapsed: int):
        """A download ended before AUTO_DOWNLOAD_DISABLE_SECONDS elapsed.

        Don't decide yet — wait for the post-download status check (see
        _post_download_check / _on_status) to see if the stream is still
        online. Only a still-online result indicates a flaky connection;
        an offline result means the stream simply ended normally.
        """
        item = self.stream_items.get(url)
        if item:
            item.pending_short_check = True

    def _on_auto_download_disabled(self, url: str):
        item = self.stream_items.get(url)
        if not item:
            return
        if item.auto_start:
            item.auto_start = False
            self._save()
            cb = self.auto_checkboxes.get(url)
            if cb:
                cb.setChecked(False)
            self._log_msg(
                f"⚠ Auto-start disabled for {item.username}: download ended unexpectedly while stream was still live",
                "#FF9800"
            )
            self._auto_disabled[url] = item.username
            self._update_auto_disabled_banner()
            if AUTO_REENABLE_COOLDOWN_SECONDS > 0:
                self._start_reenable_cooldown(url)

    def _start_reenable_cooldown(self, url: str):
        old = self._reenable_timers.pop(url, None)
        if old:
            old.stop()
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._reenable_after_cooldown(url))
        timer.start(AUTO_REENABLE_COOLDOWN_SECONDS * 1000)
        self._reenable_timers[url] = timer

    def _reenable_after_cooldown(self, url: str):
        self._reenable_timers.pop(url, None)
        item = self.stream_items.get(url)
        if not item or item.auto_start:
            return  # removed, or already manually re-enabled before this fired
        cb = self.auto_checkboxes.get(url)
        if cb:
            self._log_msg(
                f"⏰ Cooldown elapsed for {item.username}, re-enabling Auto-restart",
                "#4CAF50"
            )
            cb.setChecked(True)  # triggers _toggle_auto(url, True) via the existing signal

    def _update_auto_disabled_banner(self):
        if not self._auto_disabled:
            self._auto_disabled_banner.setVisible(False)
            return
        names = ", ".join(self._auto_disabled.values())
        self._auto_disabled_banner.setText(
            f"⚠ Auto-restart disabled for: {names} — re-check Auto on each to resume"
        )
        self._auto_disabled_banner.setVisible(True)

    def _on_dl_finished(self, url: str):
        item = self.stream_items.get(url)
        if item:
            self._log_msg(f"✓ Finished: {item.username}", "#4CAF50")
        self._cleanup_download(url)
        # Schedule a status re-check a few seconds after the download ends.
        # This avoids hammering the site immediately while still updating much
        # faster than waiting for the next full CHECK_INTERVAL cycle.
        QTimer.singleShot(5000, lambda: self._post_download_check(url))

    def _post_download_check(self, url: str):
        """Trigger one status check after a download finishes, if still tracked."""
        if url in self.stream_items:
            self.checker.force_check(url)

    # ── Helpers ─────────────────────────────────

    def _cleanup_download(self, url: str):
        worker = self.download_workers.get(url)
        if worker:
            print(f"[Debug] _cleanup_download: stopping worker {worker.objectName()}")
            worker.stop()
            if not worker.wait(5000):
                print(f"[Debug] _cleanup_download: worker {worker.objectName()} did not exit in time, terminating")
                worker.terminate()
                worker.wait(2000)
            worker.deleteLater()
            self.download_workers.pop(url, None)
        
        timer = self.download_timers.get(url)
        if timer:
            timer.stop_timer()
        item = self.stream_items.get(url)
        if item:
            item.download_active = False
            item.download_start_time = 0
            self._update_dl_ui(url, False)
        self._update_download_info()

    def _format_size(self, bytes_value: float) -> str:
        if bytes_value >= 1024 * 1024 * 1024:
            return f"{bytes_value / (1024 * 1024 * 1024):.2f} GB"
        if bytes_value >= 1024 * 1024:
            return f"{bytes_value / (1024 * 1024):.2f} MB"
        if bytes_value >= 1024:
            return f"{bytes_value / 1024:.2f} KB"
        return f"{bytes_value:.0f} B"

    def _estimate_download_info(self) -> tuple[str, float, float]:
        active_items = [item for item in self.stream_items.values() if item.download_active]
        if not active_items:
            return "No active downloads", 0.0, 0.0

        total_mbps = 0.0
        details: list[str] = []
        for item in active_items:
            resolution = item.resolution or ""
            kbps = get_bitrate_kbps(resolution, item.fps)
            if kbps <= 0:
                details.append(f"{item.username}: unknown")
                continue
            mbps = kbps / 1000.0
            total_mbps += mbps
            size_per_hour_mb = mbps * 3600 / 8.0
            if size_per_hour_mb >= 1024:
                size_label = f"{size_per_hour_mb / 1024:.2f} GB/h"
            else:
                size_label = f"{size_per_hour_mb:.2f} MB/h"
            fps_note = f" @{item.fps}fps" if item.fps and item.fps > 30 else ""
            details.append(f"{item.username}: {resolution}{fps_note} → {size_label}")

        total_per_hour_mb = total_mbps * 3600 / 8.0
        if total_per_hour_mb >= 1024:
            total_label = f"{total_per_hour_mb / 1024:.2f} GB/h"
        else:
            total_label = f"{total_per_hour_mb:.2f} MB/h"

        required_speed_mb_s = total_mbps / 8.0
        if required_speed_mb_s >= 1:
            speed_label = f"{required_speed_mb_s:.2f} MB/s"
        else:
            speed_label = f"{required_speed_mb_s * 1024:.2f} KB/s"

        summary = f"{len(active_items)} active • total {total_label} • needed {speed_label}"
        return summary, total_per_hour_mb, required_speed_mb_s

    def _update_download_info(self):
        summary, _, _ = self._estimate_download_info()
        if self._download_info_label:
            self._download_info_label.setText(f"Download info: {summary}")

    def _update_dl_ui(self, url: str, active: bool):
        item = self.stream_items.get(url)
        if not item:
            return
        row = item.row

        dl_lbl = self._table.cellWidget(row, 8)  # Was col 7, now col 8
        if dl_lbl:
            dl_lbl.setText("●" if active else "—")
            dl_lbl.setStyleSheet(
                f"color:{'#4fc' if active else '#444'}; font-size:{'16' if active else '14'}px;"
            )

        for col, enabled in ((9, not active), (10, active)):  # Start/Stop columns shifted
            btn = self._table.cellWidget(row, col)
            if btn:
                btn.setEnabled(enabled)

        if not active:
            res_lbl = self._table.cellWidget(row, 5)
            if res_lbl:
                res_lbl.setText("—")
                res_lbl.setStyleSheet(
                    "font-size:11px; color:#555; font-family:'Courier New',monospace;"
                )
            fps_lbl = self._table.cellWidget(row, 6)  # FPS column
            if fps_lbl:
                fps_lbl.setText("—")
                fps_lbl.setStyleSheet(
                    "font-size:11px; color:#555; font-family:'Courier New',monospace;"
                )
            item.resolution = ""
            item.fps = 30

    def _check_processes(self):
        """Cross-platform process health check."""
        for url, worker in list(self.download_workers.items()):
            if not (worker and worker.process):
                continue
            pid = worker.process.pid
            try:
                proc = psutil.Process(pid)
                if proc.status() == psutil.STATUS_ZOMBIE or not proc.is_running():
                    raise psutil.NoSuchProcess(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                item = self.stream_items.get(url)
                name = item.username if item else url
                self._log_msg(f"⚠ Process for {name} ended unexpectedly", "#F44336")
                self._cleanup_download(url)
                continue

            item = self.stream_items.get(url)
            if item and item.download_active:
                if time.time() - item.last_check_time > 60:
                    self.checker.force_check(url)
                    item.last_check_time = time.time()

    def _log_msg(self, message: str, color: str = "#ccc"):
        ts = datetime.now().strftime("%H:%M:%S")
        self._log.append(f'<span style="color:{color};">[{ts}] {message}</span>')
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())
        self._status_bar.showMessage(message, 3000)

    def _wait_for_stop(self, worker: QThread) -> bool:
        """Waits for a QThread that's already been signaled to stop,
        escalating to terminate() if it doesn't stop gracefully within
        5s, then giving it 2s more. Returns False if it's still alive
        even after that — caller decides what to do (e.g. a DownloadWorker
        that won't die gets kept around instead of dropped, to avoid a
        QThread destructor warning on a thread that's still running)."""
        if not worker.isRunning():
            return True
        if not worker.wait(5000):
            worker.terminate()
            if not worker.wait(2000):
                return False
        return True

    def _shutdown_workers(self):
        for url in list(self.download_workers.keys()):
            worker = self.download_workers.get(url)
            if worker:
                worker.stop()

        still_running = []
        for url in list(self.download_workers.keys()):
            worker = self.download_workers.get(url)
            if worker and not self._wait_for_stop(worker):
                still_running.append(url)
        self.download_workers = {url: w for url, w in self.download_workers.items() if url in still_running}

        self.checker.stop()
        self._wait_for_stop(self.checker)

        self.preview_worker.stop()
        self._wait_for_stop(self.preview_worker)

    def closeEvent(self, event: QEvent):
        if self.download_workers:
            ans = QMessageBox.question(
                self, "Exit",
                f"Stop {len(self.download_workers)} active download(s) and exit?",
                QMessageBox.Yes | QMessageBox.No
            )
            if ans != QMessageBox.Yes:
                event.ignore()
                return

        self._shutdown_workers()
        self._proc_timer.stop()
        self._save()
        event.accept()


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = StreamDownloaderGUI()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
