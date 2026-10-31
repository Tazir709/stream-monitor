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
    _venv_python = _venv_dir / "Scripts" / "python.exe"
    _venv_pythonw = _venv_dir / "Scripts" / "pythonw.exe"
    _target_python = _venv_pythonw if _venv_pythonw.exists() else _venv_python
    _already_there = Path(sys.executable).resolve() in {
        _venv_python.resolve(),
        _venv_pythonw.resolve(),
    }
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
import math
import psutil
import re
import json
import shutil
import string
from enum import Enum
from typing import Optional, Dict, Callable
from datetime import datetime
from queue import Queue, Empty
from dataclasses import dataclass
import traceback
import requests

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QLabel, QPushButton, QLineEdit,
    QTextEdit, QCheckBox, QStatusBar, QMessageBox, QHeaderView,
    QSplitter, QDialog, QFileDialog, QComboBox,
    QFormLayout, QDialogButtonBox, QSpinBox, QTabWidget, QScrollArea,
    QMenu, QSlider
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QEvent, QDir, QUrl, QObject
from PySide6.QtGui import QImage, QPixmap, QColor, QDesktopServices
from sites import (
    check_args_for_url,
    check_status_for_url,
    display_name_for_url,
    download_args_for_url,
    extract_username,
    ffmpeg_args_for_url,
    referer_for_url,
    requires_authentication,
    SiteCheckContext,
    get_site_policy,
    hostname_from_url,
)
from sites.xlovecam import (
    XloveCamSiteAdapter,
    username_from_url as xlovecam_username_from_url,
)

try:
    from sites.stripchat import (
        StripchatSiteAdapter,
        FFMPEG_HAS_NATIVE_H264_DECODER,
    )
except ImportError:  # pragma: no cover - used when the helper module is unavailable
    StripchatSiteAdapter = None
    FFMPEG_HAS_NATIVE_H264_DECODER = False

STRIPCHAT_ADAPTER = StripchatSiteAdapter() if StripchatSiteAdapter else None
XLOVECAM_ADAPTER = XloveCamSiteAdapter()


# ─────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────

BROWSER = ""
BROWSER_DISPLAY = ""  # "Floorp"/"Zen"/"LibreWolf" when a fork preset is active, else derived from BROWSER directly
COOKIES_FILE = ""  # optional exported-cookies.txt fallback for Chromium browsers on Windows
USER_AGENT = ""
DOWNLOAD_OUTPUT = "Downloads" # Folder downloads are saved to. Settable from the Settings dialog; persisted across sessions.
SECONDARY_DOWNLOAD_ENABLED = False
SECONDARY_DOWNLOAD_OUTPUT = ""
MINIMUM_FREE_SPACE_GB = 5
FILENAME_TEMPLATE = "{username}/{username} {date} {time}"
_LEGACY_DEFAULT_FILENAME_TEMPLATE = "{username} {date} {time}"
MAX_DOWNLOAD_RESOLUTION = "1920x1080" # 3840x2160, 1920x1080, 1280x720, 960x540, 640x360
MAX_DOWNLOAD_FPS = 30 # 60 or 30
SPLIT_BY_FILESIZE_ENABLED = False
SPLIT_FILESIZE_MB = 500
SPLIT_FILESIZE_MODE = "during"
_SPLIT_FILESIZE_MODES = {
    "Split during recording": "during",
    "Split after recording": "after",
}

_FILENAME_TEMPLATE_FIELDS = {
    "site", "username", "date", "time", "resolution", "fps",
}
_INVALID_FILENAME_CHARACTERS = set('<>:"/\\|?*')


def _format_filename_template(template: str, values: dict[str, str]) -> str:
    """Format a relative output path using only the supported template fields."""
    try:
        for _, field_name, format_spec, conversion in string.Formatter().parse(template):
            if field_name is None:
                continue
            if field_name not in _FILENAME_TEMPLATE_FIELDS or format_spec or conversion:
                raise ValueError(f"Unsupported template field: {{{field_name}}}")
        return template.format_map(values)
    except (KeyError, ValueError) as exc:
        raise ValueError(str(exc)) from exc


def _filename_validation_error(filename: str) -> Optional[str]:
    if not filename.strip():
        return "The template produces an empty filename."
    if len(filename) > 240:
        return "The filename and folders are too long (maximum 240 characters)."
    if "\\" in filename:
        return "Use forward slashes (/) to separate folders in the template."
    components = filename.split("/")
    if any(not component for component in components):
        return "Folder names and the filename cannot be empty."
    for component in components:
        if component in (".", ".."):
            return "Folder paths cannot contain '.' or '..' components."
        if any(char in _INVALID_FILENAME_CHARACTERS or ord(char) < 32 for char in component):
            return 'A folder or filename contains a character Windows does not allow: <>:"\\|?*.'
        if component.endswith((" ", ".")):
            return "A folder or filename cannot end with a space or period."
        device_name = component.split(".", 1)[0].upper()
        if device_name in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(
            r"(COM|LPT)[1-9]", device_name
        ):
            return f'"{device_name}" is reserved by Windows.'
    return None


def _filename_template_preview(template: str) -> tuple[str, Optional[str]]:
    now = datetime.now()
    values = {
        "site": "Stripchat",
        "username": "sample_user",
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H-%M-%S"),
        "resolution": "1920x1080",
        "fps": "30",
    }
    try:
        filename = _format_filename_template(template, values)
    except ValueError as exc:
        return "", str(exc)
    return filename, _filename_validation_error(filename)


def _build_download_filename_stem(
    template: str,
    url: str,
    username: str,
    started_at: datetime,
    resolution: str,
    fps: int,
    segment_number: Optional[int] = None,
) -> str:
    values = {
        "site": extract_site(url),
        "username": username,
        "date": started_at.strftime("%Y-%m-%d"),
        "time": started_at.strftime("%H-%M-%S"),
        "resolution": resolution or "unknown",
        "fps": str(fps or 0),
    }
    for key, value in values.items():
        values[key] = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).rstrip(" .")
    filename = _format_filename_template(template, values)
    if segment_number is not None:
        filename += f" segment {segment_number:03d}"
    error = _filename_validation_error(filename)
    if error:
        raise ValueError(f"Invalid filename template result: {error}")
    return filename


DOWNLOAD_BANDWIDTH_LIMIT_ENABLED = False
DOWNLOAD_BANDWIDTH_ESTIMATED_MB_S = 0.0
DOWNLOAD_BANDWIDTH_SAFE_LIMIT_MB_S = 0.0
DOWNLOAD_BANDWIDTH_HEADROOM_MB_S = 0.25
DOWNLOAD_BANDWIDTH_PERCENT = 80


def select_download_output(
    primary_folder: str,
    secondary_enabled: bool,
    secondary_folder: str,
    minimum_free_space_gb: int,
) -> tuple[Optional[str], Optional[str]]:
    """Choose a destination with enough room, creating configured folders if needed."""
    threshold_bytes = max(1, minimum_free_space_gb) * 1024 ** 3

    def prepare(folder: str) -> tuple[Optional[str], Optional[str]]:
        path = os.path.abspath(os.path.expanduser(folder.strip()))
        try:
            os.makedirs(path, exist_ok=True)
            if not os.path.isdir(path):
                return None, f"Destination is not a folder: {path}"
            return path, None
        except OSError as exc:
            return None, f"Cannot access destination folder {path}: {exc}"

    primary_path, primary_error = prepare(primary_folder or "Downloads")
    if primary_error:
        return None, primary_error
    try:
        primary_free = shutil.disk_usage(primary_path).free
    except OSError as exc:
        return None, f"Cannot check free space for primary destination {primary_path}: {exc}"
    if primary_free >= threshold_bytes:
        return primary_path, None

    primary_free_gb = primary_free / 1024 ** 3
    if not secondary_enabled:
        return None, (
            f"Primary download drive is low on space "
            f"({primary_free_gb:.1f} GB free; threshold {minimum_free_space_gb} GB). "
            "Enable and configure a secondary download location in Settings."
        )
    if not secondary_folder.strip():
        return None, (
            f"Primary download drive is low on space ({primary_free_gb:.1f} GB free), "
            "and the secondary download folder is not configured."
        )

    secondary_path, secondary_error = prepare(secondary_folder)
    if secondary_error:
        return None, (
            f"Primary download drive is low on space ({primary_free_gb:.1f} GB free). "
            f"The secondary location is unavailable: {secondary_error}"
        )
    try:
        secondary_free = shutil.disk_usage(secondary_path).free
    except OSError as exc:
        return None, (
            f"Primary download drive is low on space ({primary_free_gb:.1f} GB free). "
            f"Cannot check free space for secondary destination {secondary_path}: {exc}"
        )
    if secondary_free < threshold_bytes:
        secondary_free_gb = secondary_free / 1024 ** 3
        return None, (
            f"Both download locations are low on space. Primary: {primary_free_gb:.1f} GB free; "
            f"secondary: {secondary_free_gb:.1f} GB free; threshold: {minimum_free_space_gb} GB."
        )
    return secondary_path, None


def format_speed_mb_s(value_mb_s: float) -> str:
    if value_mb_s <= 0:
        return "0.0 MB/s"
    if value_mb_s >= 1024:
        return f"{value_mb_s / 1024:.2f} GB/s"
    if value_mb_s >= 1:
        return f"{value_mb_s:.1f} MB/s"
    return f"{value_mb_s * 1024:.1f} KB/s"


def format_hours_value(value_mb_per_hour: float) -> str:
    if value_mb_per_hour >= 1024:
        return f"{value_mb_per_hour / 1024:.2f} GB/h"
    return f"{value_mb_per_hour:.2f} MB/h"


def measure_download_speed(test_size_mb: int = 30) -> float:
    """Measure the current internet download speed in MB/s using Cloudflare's speed test endpoint.

    The measured speed is used by the optional bandwidth limiter to calculate a safe
    download limit. The limiter uses this value to control how much bandwidth active
    streams are allowed to consume and queues additional streams when the limit is reached.

    Returns:
        The measured download speed in MB/s, or 0.0 if the test fails.
    """
    total_requested_bytes = test_size_mb * 1024 * 1024
    test_url = f"https://speed.cloudflare.com/__down?bytes={total_requested_bytes}"

    try:
        start_time = time.time()
        response = requests.get(test_url, stream=True, timeout=30)
        response.raise_for_status()

        total_bytes = 0
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            total_bytes += len(chunk)

        elapsed = time.time() - start_time

        if total_bytes == 0 or elapsed <= 0:
            return 0.0

        speed_mb_s = (total_bytes / elapsed) / (1024 * 1024)
        return round(speed_mb_s, 2)

    except requests.exceptions.RequestException:
        return 0.0
    except Exception:
        return 0.0


# Prevents unstable streams from continuously rejoining the download queue
# when the system is under heavy load. Prioritizing fewer stable downloads
# is preferable to having many unstable downloads repeatedly stopping and restarting.
# Settable from the Settings dialog; persisted across sessions. 0 to disable.
AUTO_DOWNLOAD_DISABLE_SECONDS = 300

# 0 = off (today's manual-only behavior), the safe default for a new opt-in
# feature. Otherwise, a stream disabled by the protection above gets its
# Auto-restart automatically re-checked after this many seconds.
AUTO_REENABLE_COOLDOWN_SECONDS = 0

DOWNLOAD_BITRATE_KBPS = {
    "640x360":   {30: 935,  60: 1344}, # 60fps untested
    "960x540":   {30: 1729, 60: 2544}, # 60fps untested
    "1280x720":  {30: 3115, 60: 4150},   # tested
    "720x960":  {30: 1670, 60: 2360},
    "1920x1080": {30: 5128, 60: 7146},   # tested
    "2560x1440": {30: 7192, 60: 8205}, # 30fps untested
    "3840x2160": {30: 8550, 60: 10788}, # untested, rare to find
}

_SEGMENT_SIZE_CHOICES = {
    "250 MB": 250,
    "500 MB": 500,
    "750 MB": 750,
    "1 GB": 1024,
    "1.5 GB": 1536,
    "2 GB": 2048,
    "2.5 GB": 2560,
    "3 GB": 3072,
}

# Safety margins for split-by-size estimation.
# See calculate_segment_duration_seconds() for details.
BITRATE_SAFETY_FACTOR = 1.03
KEYFRAME_SAFETY_FACTOR = 0.96
BITRATE_SAFETY_FACTOR_SC = 1.12   # was 1.10
KEYFRAME_SAFETY_FACTOR_SC = 0.96  # unchanged
STRIPCHAT_60FPS_BITRATE_FACTOR = 1.05
SPLIT_CALIBRATION_SIZE_FACTOR = 0.90


def calculate_segment_duration_seconds(
    size_mb: int,
    resolution: str,
    fps: int,
    bitrate_safety_factor: float = BITRATE_SAFETY_FACTOR,
    keyframe_safety_factor: float = KEYFRAME_SAFETY_FACTOR,
) -> int:
    """Estimate a recording duration from the selected target size and bitrate.

    Applies a bitrate margin to compensate for streams exceeding the table's
    estimate and a keyframe margin to compensate for segment-boundary cuts.
    The default margins are used for other sites; Stripchat uses its own
    larger bitrate margin in calculate_stream_segment_duration_seconds().
    """
    bitrate_kbps = get_bitrate_kbps(resolution, fps)
    if size_mb <= 0 or bitrate_kbps <= 0:
        return 0
    adjusted_kbps = bitrate_kbps * bitrate_safety_factor
    target_bytes = size_mb * 1024 * 1024
    bytes_per_second = adjusted_kbps * 1000 / 8
    base_seconds = target_bytes / bytes_per_second
    adjusted_seconds = base_seconds * keyframe_safety_factor
    return max(30, int(adjusted_seconds))


def calculate_stream_segment_duration_seconds(
    stream_url: str,
    size_mb: int,
    resolution: str,
    fps: int,
) -> int:
    """Estimate a split duration using the safety margins for the stream site."""
    if STRIPCHAT_ADAPTER and STRIPCHAT_ADAPTER.matches(stream_url):
        bitrate_safety_factor = BITRATE_SAFETY_FACTOR_SC
        if fps >= 50:
            bitrate_safety_factor *= STRIPCHAT_60FPS_BITRATE_FACTOR
        return calculate_segment_duration_seconds(
            size_mb,
            resolution,
            fps,
            bitrate_safety_factor,
            KEYFRAME_SAFETY_FACTOR_SC,
        )
    return calculate_segment_duration_seconds(size_mb, resolution, fps)


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


def estimate_bandwidth_need_mb_s(resolution: str, fps: int) -> float:
    """Estimated download throughput for a resolution/fps, in MB/s.

    Shared by the queue admission check (StreamDownloaderGUI._stream_need_mb_s)
    and the per-download rate cap (DownloadWorker.run) so both always agree
    on what a stream "needs".
    """
    bitrate_kbps = get_bitrate_kbps(resolution or MAX_DOWNLOAD_RESOLUTION, fps or MAX_DOWNLOAD_FPS)
    if bitrate_kbps <= 0:
        return 0.0
    return (bitrate_kbps / 1000.0) / 8.0


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
    if not real_browser:
        return []
    if COOKIES_FILE and real_browser in _CHROMIUM_BROWSERS and os.path.isfile(COOKIES_FILE):
        return ["--cookies", COOKIES_FILE]
    return ["--cookies-from-browser", BROWSER]


# ─────────────────────────────────────────────
BROWSER_REQUIRED_MSG = "No browser configured in Settings — required for this site's cookies/impersonate"


def get_site_args(url: str) -> list[str]:
    """Return site-specific yt-dlp download arguments."""
    return download_args_for_url(url, BROWSER)


def get_site_check_args(url: str) -> list[str]:
    """Return extra yt-dlp CLI args for the live-status check / URL-resolve
    calls — currently just --impersonate where configured. Kept separate
    from get_site_args() since checks shouldn't need the download-only
    flags (e.g. --downloader-args)."""
    return check_args_for_url(url, BROWSER)


def get_site_referer(url: str) -> str:
    """Return the Referer header to use for the preview worker's ffmpeg
    capture, or "" if none is configured for this site."""
    return referer_for_url(url)


def get_site_ffmpeg_args(url: str) -> list[str]:
    """Return extra ffmpeg args for the preview worker's capture command,
    or [] if none is configured for this site."""
    return ffmpeg_args_for_url(url)


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
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


YTDLP_PATH = _find_tool("yt-dlp")
FFMPEG_PATH = _find_tool("ffmpeg")
FFPROBE_PATH = _find_tool("ffprobe")


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
    pending_short_check: bool = False
    queued: bool = False


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


def extract_site(url: str) -> str:
    """Return a short, human-readable site name from a stream URL."""
    return display_name_for_url(url)


def extract_stream_username(url: str) -> str:
    """Extract the site-specific model name used for labels and output paths."""
    if XLOVECAM_ADAPTER.matches(url):
        return xlovecam_username_from_url(url)
    return extract_username(url)


def log_exception(prefix: str) -> None:
    print(f"[Debug] {prefix}")
    traceback.print_exc()


CHROMIUM_COOKIE_ERROR_MSG = "🔒 Chrome cookie access failed — see Troubleshooting in the README"


def format_error_message(stderr: str, stdout: str = "") -> str:
    text = f"{stderr}\n{stdout}".lower()

    if "no browser configured" in text:
        return f"⚠ {BROWSER_REQUIRED_MSG}"
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
            log_exception(f"CookieProbeWorker failed for {self.real_browser}")
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        self.result_signal.emit(self.real_browser, accessible)


# ─────────────────────────────────────────────
#  Preview worker
# ─────────────────────────────────────────────

class SharedPreviewWorker(QThread):
    preview_updated = Signal(str, QPixmap)

    CAPTURE_INTERVAL, HLS_CACHE_LIFETIME = 90, 5


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
                except Exception as e:
                    print(f"[Debug] SharedPreviewWorker.stop: ffmpeg process already gone or failed to kill: {e!r}")
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

        if STRIPCHAT_ADAPTER and STRIPCHAT_ADAPTER.matches(page_url):
            try:
                frame_data = STRIPCHAT_ADAPTER.preview_data(page_url, user_agent=get_user_agent())
                if frame_data:
                    self._stream_url_cache[page_url] = ("stripchat_preview", time.time() + self.HLS_CACHE_LIFETIME)
                    self._stripchat_preview_cache[page_url] = frame_data
                    return "stripchat_preview"
                print(f"[Debug] Stripchat preview data unavailable for {page_url}; no preview available")
            except Exception as e:
                print(f"[Debug] Stripchat preview failed: {e!r}")
                return None

        if requires_authentication(page_url, BROWSER):
            print(f"[Debug] PreviewWorker _get_stream_url skipped for {page_url}: {BROWSER_REQUIRED_MSG}")
            return None

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
        if STRIPCHAT_ADAPTER and STRIPCHAT_ADAPTER.matches(page_url):
            frame_data = STRIPCHAT_ADAPTER.preview_data(page_url, user_agent=get_user_agent())
            if frame_data is None:
                print(f"[Debug] Stripchat preview frame was empty for {page_url}")
                return
            image_bytes, width, height = frame_data
            if not image_bytes:
                print(f"[Debug] Stripchat preview frame was empty for {page_url}")
                return
            try:
                img = QImage(image_bytes, width, height, width * 3, QImage.Format_RGB888)
                px = QPixmap.fromImage(img)
                if not px.isNull():
                    self._pixmap_cache[page_url] = px
                    self.preview_updated.emit(page_url, px)
                    return
                print(f"[Debug] Stripchat preview produced null pixmap for {page_url}")
            except Exception as e:
                print(f"[Debug] Stripchat preview QImage conversion failed for {page_url}: {e!r}")
            return

        if XLOVECAM_ADAPTER.matches(page_url):
            try:
                frame_data = XLOVECAM_ADAPTER.preview_data(
                    page_url,
                    user_agent=get_user_agent(),
                    ffmpeg_path=FFMPEG_PATH,
                    subprocess_kwargs=_subprocess_kwargs(),
                )
                if frame_data:
                    image_bytes, width, height = frame_data
                    img = QImage(
                        image_bytes, width, height, width * 3, QImage.Format_RGB888
                    )
                    pixmap = QPixmap.fromImage(img)
                    if not pixmap.isNull():
                        self._pixmap_cache[page_url] = pixmap
                        self.preview_updated.emit(page_url, pixmap)
                        return
                print(f"[Debug] XloveCam preview unavailable for {page_url}")
            except Exception as exc:
                print(f"[Debug] XloveCam preview failed for {page_url}: {exc!r}")
            return

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
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0  # SW_HIDE
            kwargs["startupinfo"] = startupinfo
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            print(f"[Debug] ffmpeg preview command for {page_url}: {' '.join(cmd)}")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, **kwargs)
            with self._lock:
                self._ffmpeg_processes.append(proc)
            try:
                stdout, stderr = proc.communicate(timeout=30)
                print(f"[Debug] ffmpeg preview stderr for {page_url}: {stderr.decode('utf-8', 'replace')[:1000]}")
                expected = W * H * 3
                if len(stdout) >= expected:
                    img = QImage(stdout[:expected], W, H, W * 3, QImage.Format_RGB888)
                    px = QPixmap.fromImage(img)
                    if not px.isNull():
                        self._pixmap_cache[page_url] = px
                        self.preview_updated.emit(page_url, px)
                    else:
                        print(f"[Debug] ffmpeg preview produced null pixmap for {page_url}")
                else:
                    print(f"[Debug] ffmpeg preview produced {len(stdout)} bytes for {page_url}, expected >= {expected}")
            except subprocess.TimeoutExpired:
                print(f"[Debug] ffmpeg preview timed out for {page_url}")
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
    remuxing_signal           = Signal(str, bool)  # (url, is_remuxing)

    _NOISY_PATTERNS = (
        "error reading http response",
        "end of file",
        "[https @",
        "non monotonous",
        "discontinuity",
        "invalid data found",
        "application provided invalid",
        "[in#",           # ffmpeg demuxer keepalive noise, e.g. "[in#0/hls @ ...]"
        "http error 404",  # Normal when an HLS stream ends
        "404 not found",
    )

    # Matches ffmpeg's stream description line, e.g.:
    #   Stream #0:0[0x0]: Video: h264 ..., 1280x720, 60 fps, 30 tbr, 90k tbn, ...
    # This is the ground truth for the format actually being downloaded --
    # some sites only expose a single format (e.g. 720p60), so this can
    # reveal a higher fps than the pre-download probe assumed.
    _STREAM_FORMAT_RE = re.compile(
        r"Video:.*?(\d+)x(\d+)[^,]*,\s*([\d.]+)\s*fps", re.IGNORECASE
    )

    def __init__(
        self,
        stream_url: str,
        username: str,
        output_path: str = "Downloads",
        segment_number: Optional[int] = None,
        cached_probe: Optional[tuple[str, int]] = None,
        probe_cache_generation: int = 0,
    ):
        super().__init__()
        self.setObjectName(f"DownloadWorker-{username}")
        self.stream_url  = stream_url
        self.username    = username
        self.output_path = os.path.join(output_path, username)
        self.filename_template = FILENAME_TEMPLATE
        self.process: Optional[subprocess.Popen] = None
        self.is_running  = False
        self.manual_stop = False
        self._run_finished = False
        self._rate_limiter = RateLimiter(2.0)
        self._line_queue: Queue = Queue()
        self._started_at = 0.0
        self._last_detected_format: Optional[tuple[str, int]] = None
        self.segment_number = segment_number
        self.split_enabled = SPLIT_BY_FILESIZE_ENABLED
        self.split_mode = SPLIT_FILESIZE_MODE
        self._during_split = (
            self.split_enabled and self.split_mode == "during"
            and segment_number is not None
        )
        self.segment_size_mb = SPLIT_FILESIZE_MB
        self.cached_probe = cached_probe
        self.probe_cache_generation = probe_cache_generation
        self.target_duration_seconds = 0
        self.target_segment_count = 0
        self.automatic_split = False
        self._expected_output_stem = ""
        os.makedirs(self.output_path, exist_ok=True)

    def _run_stripchat_download(self) -> bool:
        """Run the native Stripchat session through its site adapter."""
        if not STRIPCHAT_ADAPTER or not STRIPCHAT_ADAPTER.matches(self.stream_url):
            return False

        self.is_running = True
        self._stripchat_stop = False

        # Wrap on_log so we can detect when the adapter starts remuxing
        # and flip the "remuxing" flag on the GUI row.
        def wrapped_log(message: str):
            if "Remuxing with FFmpeg" in message:
                self.remuxing_signal.emit(self.stream_url, True)
            elif "Remuxed to:" in message:
                self.remuxing_signal.emit(self.stream_url, False)
            self.log_signal.emit(self.username, message)

        def report_resolution(resolution: str, fps: int) -> Optional[int]:
            if self.cached_probe:
                resolution, fps = self.cached_probe
            detected_fps = fps or MAX_DOWNLOAD_FPS
            self._last_detected_format = (
                resolution or MAX_DOWNLOAD_RESOLUTION,
                detected_fps,
            )
            self.resolution_signal.emit(self.stream_url, resolution, detected_fps)
            if self.split_enabled and self.split_mode == "during":
                self.target_duration_seconds = calculate_stream_segment_duration_seconds(
                    self.stream_url,
                    self.segment_size_mb,
                    resolution or MAX_DOWNLOAD_RESOLUTION,
                    detected_fps,
                )
                self.log_signal.emit(
                    self.username,
                    f"📦 {'Initial ' if self._during_split and STRIPCHAT_ADAPTER and STRIPCHAT_ADAPTER.matches(self.stream_url) else ''}"
                    "split target (during recording): "
                    f"{self.segment_size_mb} MB "
                    f"({self.target_duration_seconds}s at {resolution or MAX_DOWNLOAD_RESOLUTION}, "
                    f"{detected_fps} fps)",
                )
                if self._during_split:
                    return self.target_duration_seconds or None
            elif self.split_enabled:
                self.log_signal.emit(
                    self.username,
                    f"📦 Post-recording split size limit: {self.segment_size_mb} MB per clip",
                )
            return None

        def make_stripchat_output_file(resolution: str, fps: int) -> str:
            path = os.path.join(
                self.output_path,
                _build_download_filename_stem(
                    self.filename_template,
                    self.stream_url,
                    self.username,
                    datetime.now(),
                    resolution,
                    fps,
                    self.segment_number if self._during_split else None,
                ) + ".mp4",
            )
            os.makedirs(os.path.dirname(path), exist_ok=True)
            return path

        result = STRIPCHAT_ADAPTER.download(
            stream_url=self.stream_url,
            username=self.username,
            output_directory=self.output_path,
            output_file_factory=make_stripchat_output_file,
            target_height=int(MAX_DOWNLOAD_RESOLUTION.split("x", 1)[1]),
            user_agent=USER_AGENT or None,
            ffmpeg_path=FFMPEG_PATH,
            on_log=wrapped_log,
            on_progress=lambda percent: self.progress_signal.emit(self.username, percent),
            on_error=lambda message: self.log_signal.emit(self.username, f"❌ Stripchat: {message}"),
            should_stop=lambda: self.manual_stop or self._stripchat_stop,
            on_resolution=report_resolution,
            segment_number=self.segment_number if self._during_split else None,
            split_by_filesize=self.split_enabled,
            max_duration_seconds=(
                self.target_duration_seconds if self._during_split else 0
            ),
            target_size_mb=self.segment_size_mb if self._during_split else 0,
        )

        # Always clear the remuxing flag when we exit, even on error
        self.remuxing_signal.emit(self.stream_url, False)

        self._stripchat_stop = False
        self.is_running = False
        self.automatic_split = result.split_requested and not self.manual_stop
        if (
            self.split_enabled
            and self.split_mode == "after"
            and result.output_file
            and result.status in ("success", "stopped")
        ):
            self._post_recording_split(result.output_file)
        return True

    def _run_xlovecam_download(self) -> bool:
        """Record XloveCam's native HLS playlist through FFmpeg."""
        if not XLOVECAM_ADAPTER.matches(self.stream_url):
            return False

        self.is_running = True
        self._xlovecam_stop = False
        self._started_at = time.time()
        max_duration_seconds = 0
        if self.split_enabled and self.split_mode == "during":
            max_duration_seconds = calculate_stream_segment_duration_seconds(
                self.stream_url,
                self.segment_size_mb,
                MAX_DOWNLOAD_RESOLUTION,
                MAX_DOWNLOAD_FPS,
            )
            self.log_signal.emit(
                self.username,
                f"📦 XloveCam split target: {self.segment_size_mb} MB "
                f"(estimated {max_duration_seconds}s)",
            )
        elif self.split_enabled:
            self.log_signal.emit(
                self.username,
                f"📦 Post-recording split size limit: {self.segment_size_mb} MB per clip",
            )

        def report_resolution(resolution: str, fps: int) -> None:
            self._last_detected_format = (resolution, fps)
            self.resolution_signal.emit(self.stream_url, resolution, fps)
            self.log_signal.emit(
                self.username,
                f"📐 XloveCam stream format: {resolution} ({fps} fps)",
            )

        def wrapped_log(message: str) -> None:
            if "Remuxing with FFmpeg" in message:
                self.remuxing_signal.emit(self.stream_url, True)
            elif "Remuxed to:" in message:
                self.remuxing_signal.emit(self.stream_url, False)
            self.log_signal.emit(self.username, message)

        output_file = os.path.join(
            self.output_path,
            _build_download_filename_stem(
                self.filename_template,
                self.stream_url,
                self.username,
                datetime.now(),
                "unknown",
                0,
                self.segment_number if self._during_split else None,
            ) + ".mp4",
        )
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        self.resolution_signal.emit(self.stream_url, "", 0)
        if self.manual_stop:
            self.is_running = False
            return True

        result = XLOVECAM_ADAPTER.download(
            stream_url=self.stream_url,
            output_file=output_file,
            user_agent=get_user_agent() or None,
            ffmpeg_path=FFMPEG_PATH,
            on_log=wrapped_log,
            should_stop=lambda: self.manual_stop or self._xlovecam_stop,
            max_duration_seconds=max_duration_seconds,
            subprocess_kwargs=_subprocess_kwargs(),
            on_resolution=report_resolution,
        )
        self.remuxing_signal.emit(self.stream_url, False)
        output_exists = bool(
            result.output_file
            and os.path.isfile(result.output_file)
            and os.path.getsize(result.output_file) > 0
        )
        self.automatic_split = (
            result.split_requested and output_exists and not self.manual_stop
        )
        if result.status == "error":
            self.log_signal.emit(self.username, f"❌ XloveCam: {result.message}")
        elif result.status == "success":
            self.log_signal.emit(self.username, "✓ XloveCam recording completed")
            elapsed = int(time.time() - self._started_at)
            if not self.automatic_split and elapsed < AUTO_DOWNLOAD_DISABLE_SECONDS:
                self.short_download_signal.emit(self.stream_url, elapsed)
        elif result.status == "stopped":
            self.log_signal.emit(self.username, "⏹ XloveCam recording stopped")

        if (
            self.split_enabled
            and self.split_mode == "after"
            and output_exists
            and result.output_file
            and result.status in ("success", "stopped")
        ):
            self._post_recording_split(result.output_file)
        self._xlovecam_stop = False
        self.is_running = False
        return True

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
                        fps = round(float(m.group(2)))
                    except ValueError:
                        fps = MAX_DOWNLOAD_FPS  # yt-dlp printed "NA" or similar -- fall back to the configured cap
                    return res, fps
            if r.returncode != 0:
                err = format_error_message(r.stderr, r.stdout)
                print(f"[Debug] DownloadWorker probe failed for {self.username}: {err}")
        except Exception as e:
            print(f"[Debug] DownloadWorker probe failed for {self.username}: {e!r}")
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
        self.is_running = True
        try:
            if self.manual_stop:
                return
            if self._run_xlovecam_download():
                return
            if STRIPCHAT_ADAPTER and STRIPCHAT_ADAPTER.matches(self.stream_url):
                if self.split_enabled and self.split_mode == "during":
                    self.target_duration_seconds = calculate_stream_segment_duration_seconds(
                        self.stream_url,
                        self.segment_size_mb,
                        MAX_DOWNLOAD_RESOLUTION,
                        MAX_DOWNLOAD_FPS,
                    )
                    self.log_signal.emit(
                        self.username,
                        f"📦 Initial split target fallback: {self.segment_size_mb} MB "
                        f"({self.target_duration_seconds}s at {MAX_DOWNLOAD_RESOLUTION}, "
                        f"{MAX_DOWNLOAD_FPS} fps)",
                    )
                if self.manual_stop:
                    return
                handled = self._run_stripchat_download()
                if handled:
                    return

            if self.manual_stop:
                return
            if requires_authentication(self.stream_url, BROWSER):
                raise RuntimeError(BROWSER_REQUIRED_MSG)

            if self.cached_probe and self.split_enabled and self.split_mode == "during":
                res, fps = self.cached_probe
                self.log_signal.emit(self.username, "📐 Reusing cached stream format")
            else:
                self.log_signal.emit(self.username, "🔍 Probing stream format…")
                res, fps = self._probe_resolution()

            if res:
                self.log_signal.emit(
                    self.username,
                    f"📐 Estimated format: {res} ({fps or '?'} fps)"
                )
                self.resolution_signal.emit(
                    self.stream_url,
                    res,
                    fps or MAX_DOWNLOAD_FPS
                )
                self._last_detected_format = (res, fps or MAX_DOWNLOAD_FPS)
            else:
                self.log_signal.emit(self.username, "📐 Stream format unknown")
                self.resolution_signal.emit(
                    self.stream_url,
                    "",
                    MAX_DOWNLOAD_FPS
                )
                self._last_detected_format = None

            if self.split_enabled and self.split_mode == "during":
                if self._during_split:
                    self.target_duration_seconds = self._calibrate_segment_duration(
                        res or MAX_DOWNLOAD_RESOLUTION,
                        fps or MAX_DOWNLOAD_FPS,
                    )
                    if self.manual_stop:
                        return
                else:
                    self.target_duration_seconds = calculate_stream_segment_duration_seconds(
                        self.stream_url,
                        self.segment_size_mb,
                        res or MAX_DOWNLOAD_RESOLUTION,
                        fps or MAX_DOWNLOAD_FPS,
                    )
                    if self.target_duration_seconds <= 0:
                        raise RuntimeError(
                            "Unable to estimate a segment duration from the configured bitrate table"
                        )
                self.log_signal.emit(
                    self.username,
                    f"📦 Split target (during recording): {self.segment_size_mb} MB "
                    f"({self.target_duration_seconds}s at "
                    f"{res or MAX_DOWNLOAD_RESOLUTION}, {fps or MAX_DOWNLOAD_FPS} fps)",
                )
            elif self.split_enabled:
                self.log_signal.emit(
                    self.username,
                    f"📦 Post-recording split size limit: {self.segment_size_mb} MB per clip",
                )

            if fps and fps > MAX_DOWNLOAD_FPS:
                self.log_signal.emit(
                    self.username,
                    f"⚠ {MAX_DOWNLOAD_FPS}fps not available at {res}; "
                    f"using {fps}fps instead"
                )

            self._started_at = time.time()

            filename_stem = _build_download_filename_stem(
                self.filename_template,
                self.stream_url,
                self.username,
                datetime.now(),
                res or MAX_DOWNLOAD_RESOLUTION,
                fps or MAX_DOWNLOAD_FPS,
                self.segment_number if self._during_split else None,
            )
            self._expected_output_stem = os.path.join(self.output_path, filename_stem)
            os.makedirs(os.path.dirname(self._expected_output_stem), exist_ok=True)
            if self.manual_stop:
                return

            cmd = [
                YTDLP_PATH,
                "--no-simulate",
                "--format", get_download_format_selector(),
            ]
            if self._during_split:
                cmd.extend([
                    "--download-sections",
                    f"*0-{self.target_duration_seconds}",
                ])
            cmd.extend([
                "-o", os.path.join(
                    self.output_path,
                    f"{filename_stem}.%(ext)s"
                ),
                "--no-overwrites", "--continue", "--no-part",
                "--skip-unavailable-fragments", "--hls-use-mpegts",
                "--no-live-from-start",
            ])
            if DOWNLOAD_BANDWIDTH_LIMIT_ENABLED and DOWNLOAD_BANDWIDTH_SAFE_LIMIT_MB_S > 0:
                # Cap this stream at its own admission-time estimate so actual
                # throughput can't run past what the queue already assumed it
                # would use -- without this, admission control alone can't
                # keep total usage under the configured limit.
                need_mb_s = estimate_bandwidth_need_mb_s(res, fps)
                if need_mb_s > 0:
                    cmd.extend(["--limit-rate", f"{need_mb_s:.2f}M"])
            user_agent = get_user_agent()
            if user_agent:
                cmd.extend(["--user-agent", user_agent])
            cmd.extend(get_cookie_args())
            cmd.extend(get_site_args(self.stream_url))
            cmd.append(self.stream_url)
            self._rate_limiter.wait_if_needed()

            kwargs: dict = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "bufsize": 1,
                "universal_newlines": True,
            }
            if os.name == "nt":
                kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP |
                    subprocess.CREATE_NO_WINDOW
                )

            self.process = subprocess.Popen(cmd, **kwargs)
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
                        actual_fps = round(float(fmt_match.group(3)))
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
                elif any(k in line.lower() for k in ("error", "warning", "finished")) and not any(
                    n in line.lower() for n in self._NOISY_PATTERNS
                ):
                    message = line[:120]
                    self.log_signal.emit(self.username, message)

            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._force_stop_process(self.process)
                self.process.wait(timeout=5)

            has_nonempty_output = self._has_nonempty_output() if self.split_enabled else False
            output_duration = (
                self._get_output_duration_seconds() if has_nonempty_output else None
            )
            if output_duration is not None and self._during_split:
                self.log_signal.emit(
                    self.username,
                    f"📦 Segment duration: {output_duration:.1f}s "
                    f"(target {self.target_duration_seconds}s)",
                )
            split_duration_completed = (
                self._during_split
                and self.process.returncode == 0
                and output_duration is not None
                and output_duration >= max(1, self.target_duration_seconds - 2)
            )
            if (
                self._started_at
                and not self.manual_stop
                and not split_duration_completed
            ):
                elapsed = int(time.time() - self._started_at)
                if elapsed < AUTO_DOWNLOAD_DISABLE_SECONDS:
                    self.short_download_signal.emit(self.stream_url, elapsed)

            if self._during_split and not self.manual_stop:
                self.automatic_split = split_duration_completed and has_nonempty_output
                if not self.automatic_split:
                    self.log_signal.emit(
                        self.username,
                        "⚠ Segment ended before reaching its target duration, or its output "
                        "could not be validated; not restarting",
                    )
            elif self.split_enabled and self.split_mode == "after" and has_nonempty_output:
                output_path = self._find_output_file()
                if output_path:
                    self._post_recording_split(output_path)

        except Exception as e:
            hint = format_error_message(str(e))
            self.log_signal.emit(self.username, f"❌ Error: {str(e)[:100]} | {hint}")
            print(f"[Debug] DownloadWorker run failed for {self.username}: {e!r}")
            traceback.print_exc()
        finally:
            self.is_running = False
            self._run_finished = True
            self.finished_signal.emit(self.stream_url)

    def _has_nonempty_output(self) -> bool:
        return self._find_output_file() is not None

    def _calibrate_segment_duration(self, resolution: str, fps: int) -> int:
        """Measure a short live sample and estimate the size target from its bytes."""
        sample_duration_seconds = 2
        with tempfile.TemporaryDirectory(
            prefix=".split-calibration-", dir=self.output_path
        ) as sample_directory:
            sample_template = os.path.join(sample_directory, "calibration.%(ext)s")
            cmd = [
                YTDLP_PATH,
                "--no-simulate",
                "--no-playlist",
                "--format", get_download_format_selector(),
                "--download-sections", f"*0-{sample_duration_seconds}",
                "-o", sample_template,
                "--no-overwrites", "--no-part",
                "--skip-unavailable-fragments", "--hls-use-mpegts",
                "--no-live-from-start",
            ]
            if DOWNLOAD_BANDWIDTH_LIMIT_ENABLED and DOWNLOAD_BANDWIDTH_SAFE_LIMIT_MB_S > 0:
                need_mb_s = estimate_bandwidth_need_mb_s(resolution, fps)
                if need_mb_s > 0:
                    cmd.extend(["--limit-rate", f"{need_mb_s:.2f}M"])
            user_agent = get_user_agent()
            if user_agent:
                cmd.extend(["--user-agent", user_agent])
            cmd.extend(get_cookie_args())
            cmd.extend(get_site_args(self.stream_url))
            cmd.append(self.stream_url)

            self.log_signal.emit(
                self.username,
                f"📦 Measuring a {sample_duration_seconds}s sample to calibrate "
                "the split size…",
            )
            self._rate_limiter.wait_if_needed()
            if self.manual_stop:
                return 0
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                **_subprocess_kwargs(),
            )
            self.process = proc
            try:
                output, _ = proc.communicate(timeout=45)
            except subprocess.TimeoutExpired:
                self._force_stop_process(proc)
                proc.communicate()
                if self.manual_stop:
                    return 0
                raise RuntimeError("Timed out while measuring the split-size sample")
            finally:
                if self.process is proc:
                    self.process = None

            if self.manual_stop:
                return 0
            if proc.returncode != 0:
                raise RuntimeError(
                    "Could not download the split-size calibration sample: "
                    f"{format_error_message(output or '')}"
                )

            sample_paths = [
                os.path.join(sample_directory, name)
                for name in os.listdir(sample_directory)
                if not name.lower().endswith((".part", ".temp"))
            ]
            if not sample_paths:
                raise RuntimeError("The split-size calibration produced no sample file")
            sample_path = max(sample_paths, key=os.path.getsize)
            sample_bytes = os.path.getsize(sample_path)
            measured_duration = self._probe_file_duration(sample_path)
            if sample_bytes <= 0 or measured_duration is None:
                raise RuntimeError(
                    "The split-size calibration sample was empty or had no readable duration"
                )

            target_bytes = self.segment_size_mb * 1024 * 1024
            segment_count = max(
                1,
                int(SPLIT_CALIBRATION_SIZE_FACTOR * target_bytes / sample_bytes),
            )
            self.target_segment_count = segment_count
            target_duration = max(1, math.ceil(segment_count * measured_duration))
            self.log_signal.emit(
                self.username,
                f"📦 Calibrated from first sample ({measured_duration:.2f}s, "
                f"{sample_bytes:,} bytes): targeting {segment_count} sample-sized "
                f"segments ({target_duration}s) for {self.segment_size_mb} MB",
            )
            return target_duration

    def _find_output_file(self) -> Optional[str]:
        if not self._expected_output_stem:
            return None
        output_directory = os.path.dirname(self._expected_output_stem)
        try:
            names = os.listdir(output_directory)
        except OSError as exc:
            self.log_signal.emit(self.username, f"❌ Could not inspect segment output: {exc}")
            return None
        prefix = os.path.basename(self._expected_output_stem) + "."
        found: list[str] = []
        for name in names:
            if not name.startswith(prefix) or name.lower().endswith((".part", ".temp")):
                continue
            path = os.path.join(output_directory, name)
            try:
                if os.path.getsize(path) > 0:
                    found.append(path)
                else:
                    os.remove(path)
            except OSError as exc:
                self.log_signal.emit(self.username, f"⚠ Could not inspect segment file {name}: {exc}")
        return max(found, key=os.path.getmtime) if found else None

    def _get_output_duration_seconds(self) -> Optional[float]:
        path = self._find_output_file()
        return self._probe_file_duration(path) if path else None

    def _probe_file_duration(self, path: str) -> Optional[float]:
        try:
            result = subprocess.run(
                [
                    FFPROBE_PATH,
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    path,
                ],
                capture_output=True,
                text=True,
                timeout=20,
                **_subprocess_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.log_signal.emit(
                self.username,
                f"⚠ Could not verify recording duration with ffprobe: {exc}",
            )
            return None
        if result.returncode != 0:
            self.log_signal.emit(
                self.username,
                f"⚠ ffprobe could not read the recording: "
                f"{format_error_message(result.stderr, result.stdout)}",
            )
            return None
        try:
            duration = float(result.stdout.strip())
        except ValueError:
            self.log_signal.emit(
                self.username,
                f"⚠ ffprobe returned an invalid duration: {result.stdout.strip()!r}",
            )
            return None
        return duration if duration > 0 else None

    def _post_recording_split(self, source_path: str) -> None:
        duration = self._probe_file_duration(source_path)
        if duration is None:
            self.log_signal.emit(
                self.username,
                "⚠ Could not split recording; keeping the original file",
            )
            return
        target_bytes = self.segment_size_mb * 1024 * 1024
        try:
            source_bytes = os.path.getsize(source_path)
        except OSError as exc:
            self.log_signal.emit(
                self.username,
                f"⚠ Could not inspect recording size: {exc}; keeping the original file",
            )
            return
        if source_bytes <= target_bytes:
            self.log_signal.emit(
                self.username,
                f"📦 Recording is within the {self.segment_size_mb} MB size limit; "
                "keeping one file",
            )
            return
        if not FFMPEG_PATH:
            self.log_signal.emit(
                self.username,
                "❌ FFmpeg is unavailable; keeping the original recording",
            )
            return

        source_stem, source_extension = os.path.splitext(source_path)
        container = source_extension.lstrip(".").lower()
        if container in ("m4v", "m4a"):
            container = "mp4"
        if container not in ("mp4", "mkv", "webm", "mov", "ts"):
            container = "mp4"
        output_pattern = f"{source_stem} segment %03d.{container}"
        self.log_signal.emit(
            self.username,
            f"📦 Splitting finished recording into {self.segment_size_mb} MB "
            "size-limited clips with FFmpeg…",
        )
        self.remuxing_signal.emit(self.stream_url, True)
        source_duration = duration
        offset = 0.0
        segment_number = 1
        split_error = ""
        try:
            while offset < source_duration - 0.25:
                output_path = output_pattern.replace("%03d", f"{segment_number:03d}")
                ffmpeg_size_limit = target_bytes
                part_size = 0
                for attempt in range(8):
                    result = subprocess.run(
                        [
                            FFMPEG_PATH,
                            "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                            "-ss", f"{offset:.3f}",
                            "-i", source_path,
                            "-map", "0:v?", "-map", "0:a?",
                            "-c", "copy",
                            "-fs", str(ffmpeg_size_limit),
                            output_path,
                        ],
                        capture_output=True,
                        text=True,
                        **_subprocess_kwargs(),
                    )
                    if result.returncode != 0:
                        split_error = format_error_message(result.stderr, result.stdout)
                        break

                    part_size = (
                        os.path.getsize(output_path)
                        if os.path.exists(output_path)
                        else 0
                    )
                    if 0 < part_size <= target_bytes:
                        break

                    if part_size == 0:
                        split_error = "FFmpeg produced an empty clip"
                        break
                    if attempt == 7:
                        split_error = (
                            f"Could not keep clip under the {self.segment_size_mb} MB limit"
                        )
                        break

                    overshoot = part_size - target_bytes
                    reduction = max(1, target_bytes // 100)
                    ffmpeg_size_limit = max(
                        1,
                        min(
                            ffmpeg_size_limit - 1,
                            ffmpeg_size_limit - overshoot - reduction,
                        ),
                    )
                if split_error:
                    break

                part_duration = (
                    self._probe_file_duration(output_path) if part_size > 0 else None
                )
                if part_duration is None:
                    split_error = "FFmpeg produced an empty or unreadable clip"
                    break

                segment_number += 1
                next_offset = offset + part_duration
                if next_offset <= offset + 0.05:
                    split_error = "FFmpeg produced a clip with no forward progress"
                    break
                offset = next_offset
        except OSError as exc:
            split_error = f"Could not run FFmpeg: {exc}"
        finally:
            self.remuxing_signal.emit(self.stream_url, False)

        output_prefix = f"{source_stem} segment "
        output_directory = os.path.dirname(source_path)
        try:
            parts = sorted(
                os.path.join(output_directory, name)
                for name in os.listdir(output_directory)
                if name.startswith(os.path.basename(output_prefix))
                and name.lower().endswith(f".{container}")
            )
        except OSError as exc:
            self.log_signal.emit(
                self.username,
                f"❌ Could not inspect split output: {exc}; keeping the original recording",
            )
            return
        valid_parts = [
            path for path in parts
            if os.path.isfile(path)
            and os.path.getsize(path) > 0
            and os.path.getsize(path) <= target_bytes
            and self._probe_file_duration(path) is not None
        ]
        if split_error or len(valid_parts) < 2 or len(valid_parts) != len(parts):
            for path in parts:
                try:
                    os.remove(path)
                except OSError as exc:
                    self.log_signal.emit(
                        self.username,
                        f"⚠ Could not remove incomplete split output {os.path.basename(path)}: {exc}",
                    )
            error = split_error or "FFmpeg did not produce at least two valid segments"
            self.log_signal.emit(
                self.username,
                f"⚠ Post-recording split failed ({error}); keeping the original recording",
            )
            return

        try:
            os.remove(source_path)
        except OSError as exc:
            self.log_signal.emit(
                self.username,
                f"⚠ Split parts were saved, but the original could not be removed: {exc}",
            )
            return
        self.log_signal.emit(
            self.username,
            f"✓ Split finished recording into {len(valid_parts)} files",
        )

    def _force_stop_process(self, proc: subprocess.Popen):
        try:
            parent = psutil.Process(proc.pid)
            children = parent.children(recursive=True)
            for process in children + [parent]:
                try:
                    process.terminate()
                except psutil.NoSuchProcess:
                    pass
            alive = psutil.wait_procs(children + [parent], timeout=3)[1]
            for process in alive:
                try:
                    process.kill()
                except psutil.NoSuchProcess:
                    pass
        except (psutil.NoSuchProcess, ProcessLookupError):
            pass
        except (psutil.AccessDenied, OSError) as exc:
            self.log_signal.emit(self.username, f"❌ Stop error: {str(exc)[:80]}")


    def stop(self):
        """Signal the process tree to stop. Safe to call multiple times."""
        if not self.isRunning() or self._run_finished:
            return

        self.manual_stop = True

        # Signal Stripchat downloader to stop. Do not set self.is_running to
        # False immediately here: the Stripchat native thread needs to finish its
        # final remux from the partial capture, and exiting the worker too early
        # would leave the .temp file behind.
        self._stripchat_stop = True
        self._xlovecam_stop = True

        # Handle yt-dlp process if it exists
        proc = self.process
        self.log_signal.emit(self.username, "⏹ Stopping download…")
        if not proc:
            return
        self._force_stop_process(proc)


# ─────────────────────────────────────────────
#  Stream checker
# ─────────────────────────────────────────────

class SiteCheckWorker(QThread):
    """Serially check the tracked streams for one site."""

    status_signal = Signal(str, object, str)

    CHECK_INTERVAL = 90
    BACKOFF_STEP = 30
    MAX_CHECK_INTERVAL = 300
    UNKNOWN_RETRY_INTERVAL = 5

    def __init__(
        self,
        site_key: str,
        ytdlp_path: str = YTDLP_PATH,
        start_delay: float = 0,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.setObjectName(f"SiteCheckWorker-{site_key}")
        self.site_key = site_key
        self._ytdlp = ytdlp_path
        self._start_delay = start_delay
        self._running = True
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._rate_limiter = RateLimiter(2.0)
        self._lock = threading.Lock()
        self._tracked: dict[str, float] = {}
        self._check_intervals: dict[str, int] = {}

    def add_stream(self, url: str, force: bool = False):
        with self._lock:
            if force or url not in self._tracked:
                self._tracked[url] = 0.0
                self._check_intervals[url] = self.CHECK_INTERVAL
        self._wake_event.set()

    def remove_stream(self, url: str):
        with self._lock:
            self._tracked.pop(url, None)
            self._check_intervals.pop(url, None)
        self._wake_event.set()

    def force_check(self, url: str):
        """Schedule a tracked URL for the next available site check."""
        with self._lock:
            if url in self._tracked:
                self._tracked[url] = 0.0
                self._check_intervals[url] = self.CHECK_INTERVAL
        self._wake_event.set()

    def stop(self):
        self._running = False
        self._stop_event.set()
        self._wake_event.set()

    def run(self):
        if self._stop_event.wait(self._start_delay):
            return

        while self._running:
            self._wake_event.clear()
            now = time.monotonic()
            with self._lock:
                due = [
                    url for url, last_check in self._tracked.items()
                    if now - last_check >= self._check_intervals.get(
                        url, self.CHECK_INTERVAL
                    )
                ]

            if due:
                for url in due:
                    if not self._running:
                        break
                    try:
                        status, message = self._check(url)
                    except Exception as exc:
                        print(
                            f"[Debug] {self.site_key} status check failed for "
                            f"{url}: {exc!r}"
                        )
                        status, message = StreamStatus.ERROR, "❌ Site check failed"

                    checked_at = time.monotonic()
                    with self._lock:
                        if url not in self._tracked:
                            continue
                        interval = self._check_intervals.get(
                            url, self.CHECK_INTERVAL
                        )
                        if status is None:
                            # Retry transient unknowns sooner than a full poll,
                            # without letting them spin at the worker's idle tick.
                            self._tracked[url] = checked_at - max(
                                0, interval - self.UNKNOWN_RETRY_INTERVAL
                            )
                        else:
                            self._tracked[url] = checked_at
                        if status == StreamStatus.ONLINE:
                            self._check_intervals[url] = self.CHECK_INTERVAL
                        elif status == StreamStatus.OFFLINE:
                            self._check_intervals[url] = min(
                                interval + self.BACKOFF_STEP,
                                self.MAX_CHECK_INTERVAL,
                            )
                    if status is not None:
                        self.status_signal.emit(url, status, message or "")
                continue

            with self._lock:
                next_due = min(
                    (
                        self._check_intervals.get(url, self.CHECK_INTERVAL)
                        - (now - last_check)
                        for url, last_check in self._tracked.items()
                    ),
                    default=1.0,
                )
            self._wake_event.wait(max(0.1, min(next_due, 1.0)))

    def _check(self, url: str) -> tuple[Optional[StreamStatus], Optional[str]]:
        self._rate_limiter.wait_if_needed()

        if STRIPCHAT_ADAPTER and STRIPCHAT_ADAPTER.matches(url):
            try:
                result = STRIPCHAT_ADAPTER.check(url, user_agent=get_user_agent())
                if result.value == "unknown":
                    print(f"[Debug] Skipping status update for {url}: {result.message}")
                    return None, result.message

                status_map = {
                    "online": StreamStatus.ONLINE,
                    "offline": StreamStatus.OFFLINE,
                    "private": StreamStatus.PRIVATE,
                    "away": StreamStatus.AWAY,
                }
                return status_map.get(result.value, StreamStatus.ERROR), result.message
            except Exception as exc:
                message = str(exc).lower()
                if "ticket" in message or "group" in message:
                    return StreamStatus.PRIVATE, "🎫 Ticket show"
                if "private" in message:
                    return StreamStatus.PRIVATE, "🔒 Private show"
                if "p2p" in message:
                    return StreamStatus.PRIVATE, "💰 P2P show"
                if "away" in message:
                    return StreamStatus.AWAY, "🌙 Away"
                if "not currently live" in message or "off" in message:
                    return StreamStatus.OFFLINE, "💤 Offline"
                print(f"[Debug] Stripchat live check failed for {url}: {exc!r}")
                return StreamStatus.ERROR, "❌ Stripchat check failed"

        if requires_authentication(url, BROWSER):
            return StreamStatus.ERROR, f"⚠ {BROWSER_REQUIRED_MSG}"

        context = SiteCheckContext(
            ytdlp_path=self._ytdlp,
            browser=BROWSER,
            cookie_args=get_cookie_args(),
            user_agent=get_user_agent(),
            subprocess_kwargs=_subprocess_kwargs(),
            format_error=format_error_message,
        )
        result = check_status_for_url(url, context)
        if result.value == "unknown":
            print(f"[Debug] Skipping status update for {url}: {result.message}")
            return None, result.message
        status_map = {
            "online": StreamStatus.ONLINE,
            "offline": StreamStatus.OFFLINE,
            "private": StreamStatus.PRIVATE,
            "away": StreamStatus.AWAY,
        }
        return status_map.get(result.value, StreamStatus.ERROR), result.message


class StreamChecker(QObject):
    """Coordinate independent, staggered check workers per site."""

    status_signal = Signal(str, object, str)
    START_STAGGER_SECONDS = 1.0
    SITE_ORDER = (
        "chaturbate.com",
        "stripchat.com",
        "bongacams.com",
        "camsoda.com",
        "xlovecam.com",
    )

    def __init__(self, ytdlp_path: str = YTDLP_PATH):
        super().__init__()
        self._ytdlp = ytdlp_path
        self._lock = threading.Lock()
        self._workers: dict[str, SiteCheckWorker] = {}
        self._stream_sites: dict[str, str] = {}
        self._running = False
        for site_index, site_key in enumerate(self.SITE_ORDER):
            self._create_worker(site_key, site_index)

    def _create_worker(self, site_key: str, site_index: int) -> SiteCheckWorker:
        worker = SiteCheckWorker(
            site_key=site_key,
            ytdlp_path=self._ytdlp,
            start_delay=site_index * self.START_STAGGER_SECONDS,
            parent=self,
        )
        worker.status_signal.connect(self.status_signal)
        self._workers[site_key] = worker
        if self._running:
            worker.start()
        return worker

    @staticmethod
    def _site_key_for_url(url: str) -> str:
        hostname = hostname_from_url(url)
        if hostname == "stripchat.com" or hostname.endswith(".stripchat.com"):
            return "stripchat.com"
        policy = get_site_policy(url)
        return policy.key if policy else (hostname or "other")

    def start(self):
        with self._lock:
            if self._running:
                return
            self._running = True
            workers = list(self._workers.values())
        for worker in workers:
            worker.start()

    def add_stream(self, url: str, force: bool = False):
        site_key = self._site_key_for_url(url)
        with self._lock:
            worker = self._workers.get(site_key)
            if worker is None:
                worker = self._create_worker(site_key, len(self._workers))
            self._stream_sites[url] = site_key
        worker.add_stream(url, force=force)

    def remove_stream(self, url: str):
        with self._lock:
            site_key = self._stream_sites.pop(url, None)
            worker = self._workers.get(site_key) if site_key else None
        if worker:
            worker.remove_stream(url)

    def force_check(self, url: str):
        with self._lock:
            site_key = self._stream_sites.get(url)
            worker = self._workers.get(site_key) if site_key else None
        if worker:
            worker.force_check(url)

    def stop(self):
        with self._lock:
            self._running = False
            workers = list(self._workers.values())
        for worker in workers:
            worker.stop()

    def terminate(self):
        """Last-resort shutdown for any site worker that did not stop in time."""
        with self._lock:
            workers = list(self._workers.values())
        for worker in workers:
            if worker.isRunning():
                print(f"[Debug] Terminating unresponsive {worker.objectName()}")
                worker.terminate()

    def isRunning(self) -> bool:
        with self._lock:
            workers = list(self._workers.values())
        return any(worker.isRunning() for worker in workers)

    def wait(self, timeout: int = 0) -> bool:
        """Wait for every site worker, respecting one total timeout budget."""
        deadline = time.monotonic() + timeout / 1000 if timeout else None
        with self._lock:
            workers = list(self._workers.values())
        for worker in workers:
            if not worker.isRunning():
                continue
            remaining = (
                max(0, int((deadline - time.monotonic()) * 1000))
                if deadline is not None
                else 0
            )
            if not worker.wait(remaining):
                return False
        return not any(worker.isRunning() for worker in workers)


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
            "secondary_download_enabled": SECONDARY_DOWNLOAD_ENABLED,
            "secondary_output_folder": SECONDARY_DOWNLOAD_OUTPUT,
            "minimum_free_space_gb": MINIMUM_FREE_SPACE_GB,
            "filename_template": FILENAME_TEMPLATE,
            "max_resolution": MAX_DOWNLOAD_RESOLUTION,
            "max_fps": MAX_DOWNLOAD_FPS,
            "split_by_filesize": SPLIT_BY_FILESIZE_ENABLED,
            "split_filesize_mb": SPLIT_FILESIZE_MB,
            "split_filesize_mode": SPLIT_FILESIZE_MODE,
            "auto_timeout_seconds": AUTO_DOWNLOAD_DISABLE_SECONDS,
            "auto_reenable_cooldown_seconds": AUTO_REENABLE_COOLDOWN_SECONDS,
            "browser": BROWSER,
            "browser_display": BROWSER_DISPLAY,
            "cookies_file": COOKIES_FILE,
            "download_bandwidth_limit_enabled": DOWNLOAD_BANDWIDTH_LIMIT_ENABLED,
            "download_bandwidth_estimated_mb_s": DOWNLOAD_BANDWIDTH_ESTIMATED_MB_S,
            "download_bandwidth_safe_limit_mb_s": DOWNLOAD_BANDWIDTH_SAFE_LIMIT_MB_S,
            "download_bandwidth_percent": DOWNLOAD_BANDWIDTH_PERCENT,
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
_BROWSER_TOP_CHOICES = ["Pick a browser", "Firefox-based"] + [_BROWSER_DISPLAY_NAMES[c] for c in _BROWSER_CHOICES if c != "firefox"]
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
        self.setMinimumWidth(620)
        self.resize(760, 680)
        self._cookie_probe_cache: dict[str, bool] = {}  # real_browser -> accessible
        self._cookie_probe_workers: Dict[str, CookieProbeWorker] = {}  # real_browser -> in-flight worker

        outer_layout = QVBoxLayout(self)
        tabs = QTabWidget()
        outer_layout.addWidget(tabs)

        general_page = QWidget()
        browser_page = QWidget()
        recording_page = QWidget()
        general_form = QFormLayout(general_page)
        browser_form = QFormLayout(browser_page)
        recording_form = QFormLayout(recording_page)

        for title, page in (
            ("General", general_page),
            ("Browser - Cookies", browser_page),
            ("Recording", recording_page),
        ):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setWidget(page)
            tabs.addTab(scroll, title)

        # ── Download output ──
        self.out_input = QLineEdit(DOWNLOAD_OUTPUT)
        self.out_input.setToolTip("Folder where downloads will be saved")
        out_browse = QPushButton("Browse…")
        out_browse.clicked.connect(self._browse_output)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_input)
        out_row.addWidget(out_browse)
        general_form.addRow("Primary download folder:", out_row)

        self.secondary_download_check = QCheckBox("Enable secondary download location")
        general_form.addRow("", self.secondary_download_check)
        self.secondary_download_hint = QLabel(
            "When enabled, new recordings use this folder if the primary drive "
            "falls below the minimum free-space threshold. Existing recordings "
            "stay where they started; each new recording or split segment checks "
            "available space again. A download will not start if neither location "
            "has enough free space."
        )
        self.secondary_download_hint.setWordWrap(True)
        self.secondary_download_hint.setStyleSheet("color: #888; font-size: 11px;")
        general_form.addRow("", self.secondary_download_hint)

        self.secondary_out_input = QLineEdit(SECONDARY_DOWNLOAD_OUTPUT)
        self.secondary_out_input.setToolTip(
            "New downloads use this folder when the primary drive is below the free-space threshold"
        )
        self.secondary_out_browse = QPushButton("Browse…")
        self.secondary_out_browse.clicked.connect(self._browse_secondary_output)
        secondary_out_row = QHBoxLayout()
        secondary_out_row.addWidget(self.secondary_out_input)
        secondary_out_row.addWidget(self.secondary_out_browse)
        self.secondary_out_label = QLabel("Secondary download folder:")
        general_form.addRow(self.secondary_out_label, secondary_out_row)

        self.minimum_free_space_slider = QSlider(Qt.Horizontal)
        self.minimum_free_space_slider.setRange(1, 20)
        self.minimum_free_space_slider.setTickInterval(1)
        self.minimum_free_space_slider.setTickPosition(QSlider.TicksBelow)
        self.minimum_free_space_label = QLabel()
        minimum_space_row = QHBoxLayout()
        minimum_space_row.addWidget(self.minimum_free_space_slider, 1)
        minimum_space_row.addWidget(self.minimum_free_space_label)
        self.minimum_free_space_form_label = QLabel("Minimum free space:")
        general_form.addRow(self.minimum_free_space_form_label, minimum_space_row)

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
        recording_form.addRow("Video quality:", quality_row)

        self.filename_template_input = QLineEdit(FILENAME_TEMPLATE)
        self.filename_template_input.setPlaceholderText(
            "{username}/{username} {date} {time}"
        )
        recording_form.addRow("Filename template:", self.filename_template_input)
        filename_tokens = QLabel(
            "Available: {site}, {username}, {date}, {time}, {resolution}, {fps}. "
            "Use / to create subfolders.\n"
            "Examples: {username} {date} {time}, {username}/{username} {date} {time}, "
            "{site}/{username}/{username} {date} {time}"
        )
        filename_tokens.setWordWrap(True)
        filename_tokens.setStyleSheet("color: #888; font-size: 11px;")
        recording_form.addRow("", filename_tokens)
        self.filename_preview_label = QLabel()
        self.filename_preview_label.setWordWrap(True)
        recording_form.addRow("Preview:", self.filename_preview_label)

        self.split_enabled_check = QCheckBox("Split recordings by file size")
        self.split_mode_combo = QComboBox()
        self.split_mode_combo.addItems(list(_SPLIT_FILESIZE_MODES))
        self.split_size_combo = QComboBox()
        self.split_size_combo.addItems(list(_SEGMENT_SIZE_CHOICES))
        split_row = QHBoxLayout()
        split_row.addWidget(self.split_enabled_check)
        split_row.addWidget(self.split_mode_combo)
        split_row.addWidget(self.split_size_combo)
        split_row.addStretch(1)
        recording_form.addRow("Split by Filesize:", split_row)

        split_hint = QLabel(
            "Each segment's duration is estimated from the detected resolution and FPS. "
            "Actual file sizes may vary. File-size splitting requires FFmpeg; post-recording "
            "splitting also requires extra temporary disk space while FFmpeg creates segments."
        )
        split_hint.setWordWrap(True)
        split_hint.setStyleSheet("color: #888; font-size: 11px;")
        recording_form.addRow("", split_hint)

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
        general_form.addRow("Disable auto-restart if shorter than:", timeout_row)

        timeout_hint = QLabel(
            "If a stream's auto-restarted recording ends before this much time has\n"
            "passed, auto-restart is temporarily disabled for it (protects against a\n"
            "restart-loop on flaky streams). Check \"Off\" to disable this protection entirely."
        )
        timeout_hint.setStyleSheet("color: #888; font-size: 11px;")
        general_form.addRow("", timeout_hint)

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
        general_form.addRow("Auto re-enable after:", cooldown_row)

        cooldown_hint = QLabel(
            "Once auto-restart gets disabled for a flaky stream (above), automatically\n"
            "re-enable it after this much time — gives the stream a chance to settle\n"
            "before retrying. \"Off\" (default) means re-checking Auto yourself instead."
        )
        cooldown_hint.setStyleSheet("color: #888; font-size: 11px;")
        general_form.addRow("", cooldown_hint)

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
        browser_form.addRow("Cookie source:", cookie_row)

        self.fork_detect_warning = QLabel(
            "⚠ Couldn't auto-detect this browser's profile on this system — use Browse to point at it manually."
        )
        self.fork_detect_warning.setStyleSheet("color: #FF9800; font-size: 11px;")
        self.fork_detect_warning.setVisible(False)
        browser_form.addRow("", self.fork_detect_warning)

        cookie_hint = QLabel(
            "Cookies are always used — most streams need them now. Pick your browser —\n"
            "for Firefox-based, Floorp/Zen/LibreWolf auto-detect their profile with no\n"
            "path needed; pick \"Other\" there (or Browse for any other browser) to\n"
            "point at a profile folder directly. Leave on Browse for the default profile."
        )
        cookie_hint.setStyleSheet("color: #888; font-size: 11px;")
        browser_form.addRow("", cookie_hint)

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
        browser_form.addRow(self._cookies_file_label, cookies_file_row)

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
        browser_form.addRow("", self._cookies_file_hint)

        # ── User-Agent ──
        self.user_agent_input = QLineEdit()
        self.user_agent_input.setPlaceholderText("e.g. Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...")
        browser_form.addRow("User-Agent:", self.user_agent_input)

        ua_hint = QLabel(
            "Often needed to get past Cloudflare's Turnstile check. Find yours by searching\n"
            "\"what is my user agent\", or visit whatsmyua.info — use the same browser your\n"
            "cookies came from."
        )
        ua_hint.setStyleSheet("color: #888; font-size: 11px;")
        browser_form.addRow("", ua_hint)

        # ── Download Bandwidth ──
        self.download_speed_test_btn = QPushButton("Test Download Speed")
        self.download_speed_test_btn.clicked.connect(self._run_download_speed_test)
        self.download_speed_result_label = QLabel("Speed test not run yet")
        self.download_speed_result_label.setStyleSheet("color: #888; font-size: 11px;")
        speed_row = QHBoxLayout()
        speed_row.addWidget(self.download_speed_test_btn)
        speed_row.addWidget(self.download_speed_result_label, 1)
        general_form.addRow("Download Bandwidth:", speed_row)

        self.download_limit_check = QCheckBox("Limit to")
        self.download_limit_check.setToolTip("Limit downloads based on estimated connection speed")
        self.download_limit_check.toggled.connect(self._on_download_bandwidth_toggled)
        self.download_percent_combo = QComboBox()
        self.download_percent_combo.addItems(["50%", "60%", "70%", "80%", "90%", "95%", "100%"])
        self.download_percent_combo.setCurrentText("80%")
        self.download_percent_combo.currentTextChanged.connect(self._on_bandwidth_percent_changed)
        limit_row = QHBoxLayout()
        limit_row.addWidget(self.download_limit_check)
        limit_row.addWidget(self.download_percent_combo)
        limit_row.addWidget(QLabel("of measured speed"))
        limit_row.addStretch(1)
        general_form.addRow("", limit_row)

        self.download_limit_label = QLabel("Run a speed test above to enable bandwidth limiting")
        self.download_limit_label.setStyleSheet("color: #8fb3ff; font-size: 12px;")
        general_form.addRow("", self.download_limit_label)

        bandwidth_hint = QLabel(
            "The speed test downloads 30 MB from Cloudflare to estimate your connection speed. "
            "The selected percentage is used as the safe bandwidth limit. Additional streams "
            "are queued when the limit is reached and start automatically as bandwidth becomes available."
        )
        bandwidth_hint.setWordWrap(True)
        bandwidth_hint.setStyleSheet("color: #888; font-size: 11px;")
        general_form.addRow("", bandwidth_hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.close)
        buttons.accepted.connect(self.close)
        outer_layout.addWidget(buttons)

        self._load_from_globals()

        # Live-apply on change, same pattern the rest of the app already uses
        self.out_input.textChanged.connect(self._on_output_changed)
        self.secondary_download_check.toggled.connect(self._on_secondary_download_toggled)
        self.secondary_out_input.textChanged.connect(self._on_secondary_output_changed)
        self.minimum_free_space_slider.valueChanged.connect(self._on_minimum_free_space_changed)
        self.filename_template_input.textChanged.connect(self._on_filename_template_changed)
        self.resolution_combo.currentTextChanged.connect(self._on_resolution_changed)
        self.fps_combo.currentTextChanged.connect(self._on_fps_changed)
        self.split_enabled_check.toggled.connect(self._on_split_enabled_changed)
        self.split_mode_combo.currentTextChanged.connect(self._on_split_mode_changed)
        self.split_size_combo.currentTextChanged.connect(self._on_split_size_changed)
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

        self._update_bandwidth_labels()

    def _load_from_globals(self):
        self.out_input.setText(DOWNLOAD_OUTPUT)
        self.secondary_download_check.setChecked(SECONDARY_DOWNLOAD_ENABLED)
        self.secondary_out_input.setText(SECONDARY_DOWNLOAD_OUTPUT)
        self.minimum_free_space_slider.setValue(MINIMUM_FREE_SPACE_GB)
        self._update_secondary_download_controls()
        self.filename_template_input.setText(FILENAME_TEMPLATE)
        self._update_filename_template_preview()
        if MAX_DOWNLOAD_RESOLUTION in _RESOLUTION_CHOICES:
            self.resolution_combo.setCurrentText(MAX_DOWNLOAD_RESOLUTION)
        if str(MAX_DOWNLOAD_FPS) in _FPS_CHOICES:
            self.fps_combo.setCurrentText(str(MAX_DOWNLOAD_FPS))
        self.split_enabled_check.setChecked(SPLIT_BY_FILESIZE_ENABLED)
        mode_label = next(
            (label for label, mode in _SPLIT_FILESIZE_MODES.items() if mode == SPLIT_FILESIZE_MODE),
            "Split during recording",
        )
        self.split_mode_combo.setCurrentText(mode_label)
        size_label = next(
            (label for label, size_mb in _SEGMENT_SIZE_CHOICES.items() if size_mb == SPLIT_FILESIZE_MB),
            "500 MB",
        )
        self.split_mode_combo.setEnabled(SPLIT_BY_FILESIZE_ENABLED)
        self.split_size_combo.setCurrentText(size_label)
        self.split_size_combo.setEnabled(SPLIT_BY_FILESIZE_ENABLED)
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
        self.download_limit_check.setChecked(DOWNLOAD_BANDWIDTH_LIMIT_ENABLED)
        self._update_bandwidth_labels()

    def _on_output_changed(self, text: str):
        global DOWNLOAD_OUTPUT
        DOWNLOAD_OUTPUT = text.strip() or "Downloads"
        self.settings_changed.emit()

    def _update_secondary_download_controls(self):
        enabled = self.secondary_download_check.isChecked()
        self.secondary_out_input.setVisible(enabled)
        self.secondary_out_browse.setVisible(enabled)
        self.secondary_out_label.setVisible(enabled)
        self.minimum_free_space_slider.setVisible(enabled)
        self.minimum_free_space_label.setVisible(enabled)
        self.minimum_free_space_form_label.setVisible(enabled)
        self.minimum_free_space_label.setText(
            f"{self.minimum_free_space_slider.value()} GB"
        )

    def _on_secondary_download_toggled(self, enabled: bool):
        global SECONDARY_DOWNLOAD_ENABLED
        SECONDARY_DOWNLOAD_ENABLED = bool(enabled)
        self._update_secondary_download_controls()
        self.settings_changed.emit()

    def _on_secondary_output_changed(self, text: str):
        global SECONDARY_DOWNLOAD_OUTPUT
        SECONDARY_DOWNLOAD_OUTPUT = text.strip()
        self.settings_changed.emit()

    def _on_minimum_free_space_changed(self, value: int):
        global MINIMUM_FREE_SPACE_GB
        MINIMUM_FREE_SPACE_GB = int(value)
        self._update_secondary_download_controls()
        self.settings_changed.emit()

    def _browse_secondary_output(self):
        folder = QFileDialog.getExistingDirectory(
            None,
            "Choose secondary download folder",
            self.secondary_out_input.text() or ".",
        )
        if folder:
            self.secondary_out_input.setText(folder)

    def _on_filename_template_changed(self, template: str):
        global FILENAME_TEMPLATE
        self._update_filename_template_preview(template)
        _, error = _filename_template_preview(template)
        if error:
            return
        FILENAME_TEMPLATE = template
        self.settings_changed.emit()

    def _update_filename_template_preview(self, template: Optional[str] = None):
        preview, error = _filename_template_preview(
            self.filename_template_input.text() if template is None else template
        )
        if error:
            self.filename_preview_label.setText(f"Invalid filename template: {error}")
            self.filename_preview_label.setStyleSheet("color: #ff8a80; font-size: 11px;")
        else:
            self.filename_preview_label.setText(f"{preview}.mp4 — valid filename")
            self.filename_preview_label.setStyleSheet("color: #7ae8a6; font-size: 11px;")

    def _on_resolution_changed(self, text: str):
        global MAX_DOWNLOAD_RESOLUTION
        MAX_DOWNLOAD_RESOLUTION = text
        self.settings_changed.emit()

    def _on_fps_changed(self, text: str):
        global MAX_DOWNLOAD_FPS
        MAX_DOWNLOAD_FPS = int(text)
        self.settings_changed.emit()

    def _on_split_enabled_changed(self, enabled: bool):
        global SPLIT_BY_FILESIZE_ENABLED
        SPLIT_BY_FILESIZE_ENABLED = bool(enabled)
        self.split_mode_combo.setEnabled(SPLIT_BY_FILESIZE_ENABLED)
        self.split_size_combo.setEnabled(SPLIT_BY_FILESIZE_ENABLED)
        self.settings_changed.emit()

    def _on_split_mode_changed(self, label: str):
        global SPLIT_FILESIZE_MODE
        if label in _SPLIT_FILESIZE_MODES:
            SPLIT_FILESIZE_MODE = _SPLIT_FILESIZE_MODES[label]
            self.settings_changed.emit()

    def _on_split_size_changed(self, label: str):
        global SPLIT_FILESIZE_MB
        if label in _SEGMENT_SIZE_CHOICES:
            SPLIT_FILESIZE_MB = _SEGMENT_SIZE_CHOICES[label]
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

        if top == "Pick a browser":
            self.firefox_variant_combo.setVisible(False)
            self.fork_detect_warning.setVisible(False)
            self._cookie_path_btn.setVisible(False)
            self._set_cookies_file_row_visible(False)
            BROWSER_DISPLAY = ""
            BROWSER = ""
            self.settings_changed.emit()
            return

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

    def _on_download_bandwidth_toggled(self, checked: bool):
        global DOWNLOAD_BANDWIDTH_LIMIT_ENABLED
        DOWNLOAD_BANDWIDTH_LIMIT_ENABLED = bool(checked)
        self._update_bandwidth_labels()
        self.settings_changed.emit()

    def _update_bandwidth_labels(self):
        if DOWNLOAD_BANDWIDTH_ESTIMATED_MB_S > 0:
            safe_limit = DOWNLOAD_BANDWIDTH_SAFE_LIMIT_MB_S if DOWNLOAD_BANDWIDTH_SAFE_LIMIT_MB_S > 0 else (DOWNLOAD_BANDWIDTH_ESTIMATED_MB_S * (DOWNLOAD_BANDWIDTH_PERCENT / 100.0))
            self.download_limit_label.setText(
                f"{DOWNLOAD_BANDWIDTH_ESTIMATED_MB_S:.1f} MB/s measured → "
                f"limit {safe_limit:.1f} MB/s ({DOWNLOAD_BANDWIDTH_PERCENT}%)"
            )
        else:
            self.download_limit_label.setText("Run a speed test above to enable bandwidth limiting")

        self.download_limit_label.setVisible(DOWNLOAD_BANDWIDTH_LIMIT_ENABLED or DOWNLOAD_BANDWIDTH_ESTIMATED_MB_S <= 0)
        self.download_percent_combo.setEnabled(DOWNLOAD_BANDWIDTH_LIMIT_ENABLED)
        self.download_speed_test_btn.setEnabled(not bool(getattr(self.parent(), "download_workers", None)))

    def _run_download_speed_test(self):
        parent = self.parent()
        if parent and getattr(parent, "download_workers", None):
            self.download_speed_result_label.setText("Cannot test while downloads are active")
            self.download_speed_result_label.setStyleSheet("color: #ff8a80; font-size: 11px;")
            self.download_speed_result_label.setToolTip(
                "Stop all active downloads first for the most accurate result."
            )
            return

        self.download_speed_result_label.setText("Testing connection speed…")
        self.download_speed_result_label.setStyleSheet("color: #8fb3ff; font-size: 11px;")
        self.download_speed_result_label.setToolTip("")
        self.download_speed_test_btn.setEnabled(False)

        self._speed_test_worker = SpeedTestWorker()
        self._speed_test_worker.result_signal.connect(self._on_download_speed_test_result)
        self._speed_test_worker.error_signal.connect(self._on_download_speed_test_error)
        self._speed_test_worker.finished.connect(self._update_bandwidth_labels)
        self._speed_test_worker.finished.connect(self._speed_test_worker.deleteLater)
        self._speed_test_worker.start()

    def _on_download_speed_test_result(self, speed_mb_s: float):
        global DOWNLOAD_BANDWIDTH_ESTIMATED_MB_S, DOWNLOAD_BANDWIDTH_SAFE_LIMIT_MB_S
        DOWNLOAD_BANDWIDTH_ESTIMATED_MB_S = float(speed_mb_s)
        # Calculate safe limit based on the current percentage
        DOWNLOAD_BANDWIDTH_SAFE_LIMIT_MB_S = max(0.0, float(speed_mb_s) * (DOWNLOAD_BANDWIDTH_PERCENT / 100.0))
        self.download_speed_result_label.setText(
            f"Speed test completed: {DOWNLOAD_BANDWIDTH_ESTIMATED_MB_S:.1f} MB/s"
        )
        self.download_speed_result_label.setStyleSheet("color: #7ae8a6; font-size: 11px;")
        self._update_bandwidth_labels()
        self.settings_changed.emit()

    def _on_download_speed_test_error(self, message: str):
        self.download_speed_result_label.setText("Speed test failed — please try again")
        self.download_speed_result_label.setStyleSheet("color: #ff8a80; font-size: 11px;")
        self.download_speed_result_label.setToolTip(message)

    def _on_bandwidth_percent_changed(self, percent_text: str):
        """Handle bandwidth percentage dropdown change."""
        global DOWNLOAD_BANDWIDTH_PERCENT
        # Remove the '%' sign and convert to int
        percent = int(percent_text.replace("%", ""))
        DOWNLOAD_BANDWIDTH_PERCENT = percent

        # Recalculate the safe limit based on the new percentage
        if DOWNLOAD_BANDWIDTH_ESTIMATED_MB_S > 0:
            self._recalculate_safe_limit()

        self.settings_changed.emit()

    def _recalculate_safe_limit(self):
        """Recalculate the safe limit based on the current percentage."""
        global DOWNLOAD_BANDWIDTH_SAFE_LIMIT_MB_S
        if DOWNLOAD_BANDWIDTH_ESTIMATED_MB_S > 0:
            DOWNLOAD_BANDWIDTH_SAFE_LIMIT_MB_S = max(0.0, DOWNLOAD_BANDWIDTH_ESTIMATED_MB_S * (DOWNLOAD_BANDWIDTH_PERCENT / 100.0))
            self._update_bandwidth_labels()

class SpeedTestWorker(QThread):
    result_signal = Signal(float)
    error_signal = Signal(str)

    def run(self):
        try:
            result = measure_download_speed(test_size_mb=30)
            if result <= 0:
                raise RuntimeError("download speed test returned zero")
            self.result_signal.emit(float(result))
        except Exception as exc:
            self.error_signal.emit(str(exc))

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
        self.checker.destroyed.connect(lambda: print("[Debug] StreamChecker destroyed"))
        self.checker.start()

        self.download_workers: Dict[str, DownloadWorker] = {}
        self.download_timers: Dict[str, DownloadTimer]   = {}
        self.stream_items: Dict[str, StreamItem]         = {}
        self.split_sessions: dict[str, dict[str, int]] = {}
        self.probe_cache: dict[str, tuple[str, int]] = {}
        self._probe_cache_generation: dict[str, int] = {}
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
        self._settings_dialog.settings_changed.connect(self._update_browser_unset_banner)
        self._settings_dialog.settings_changed.connect(self._update_download_info)
        self._settings_dialog.settings_changed.connect(self._update_disk_space_info)
        self._settings_dialog.cookie_access_confirmed_broken.connect(self._maybe_show_chromium_cookie_dialog)

        # Convenience references so the rest of the class can keep reading
        # these the same way it always has, without caring that they now
        # live inside the settings dialog instead of the main toolbar.
        self._out_input = self._settings_dialog.out_input
        self._user_agent_input = self._settings_dialog.user_agent_input

        self._build_ui()
        self.setStyleSheet(DARK)
        self._disk_space_timer = QTimer(self)
        self._disk_space_timer.timeout.connect(self._update_disk_space_info)
        self._disk_space_timer.start(30000)
        self._load_saved_streams()
        self._update_download_info()
        self._update_browser_unset_banner()

        if FFMPEG_HAS_NATIVE_H264_DECODER:
            self._log_msg("FFmpeg H.264 decoder: available", "#7ae8a6")
        else:
            self._log_msg(
                "FFmpeg H.264 decoder: unavailable — Stripchat previews disabled",
                "#ffb74d",
            )

        QApplication.instance().aboutToQuit.connect(self._shutdown_workers)

    # ── Persistence ─────────────────────────────

    def _load_saved_streams(self):
        global SECONDARY_DOWNLOAD_ENABLED, SECONDARY_DOWNLOAD_OUTPUT
        global MINIMUM_FREE_SPACE_GB, FILENAME_TEMPLATE
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

        saved_secondary_enabled = settings.get("secondary_download_enabled")
        if isinstance(saved_secondary_enabled, bool):
            SECONDARY_DOWNLOAD_ENABLED = saved_secondary_enabled
            self._settings_dialog.secondary_download_check.setChecked(
                SECONDARY_DOWNLOAD_ENABLED
            )
        saved_secondary_output = settings.get("secondary_output_folder")
        if isinstance(saved_secondary_output, str):
            SECONDARY_DOWNLOAD_OUTPUT = saved_secondary_output
            self._settings_dialog.secondary_out_input.setText(
                SECONDARY_DOWNLOAD_OUTPUT
            )
        saved_minimum_free_space = settings.get("minimum_free_space_gb")
        try:
            saved_minimum_free_space = int(saved_minimum_free_space)
        except (TypeError, ValueError):
            saved_minimum_free_space = MINIMUM_FREE_SPACE_GB
        if 1 <= saved_minimum_free_space <= 20:
            MINIMUM_FREE_SPACE_GB = saved_minimum_free_space
            self._settings_dialog.minimum_free_space_slider.setValue(
                MINIMUM_FREE_SPACE_GB
            )
            self._settings_dialog._update_secondary_download_controls()

        saved_filename_template = settings.get("filename_template")
        migrated_filename_template = (
            saved_filename_template == _LEGACY_DEFAULT_FILENAME_TEMPLATE
        )
        if migrated_filename_template:
            saved_filename_template = FILENAME_TEMPLATE
        if isinstance(saved_filename_template, str):
            _, template_error = _filename_template_preview(saved_filename_template)
            if not template_error:
                FILENAME_TEMPLATE = saved_filename_template
                self._settings_dialog.filename_template_input.setText(FILENAME_TEMPLATE)

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

        saved_split_enabled = settings.get("split_by_filesize")
        if isinstance(saved_split_enabled, bool):
            global SPLIT_BY_FILESIZE_ENABLED
            SPLIT_BY_FILESIZE_ENABLED = saved_split_enabled
            self._settings_dialog.split_enabled_check.setChecked(saved_split_enabled)

        saved_split_size = settings.get("split_filesize_mb")
        try:
            saved_split_size = int(saved_split_size)
        except (TypeError, ValueError):
            saved_split_size = None
        if saved_split_size in _SEGMENT_SIZE_CHOICES.values():
            global SPLIT_FILESIZE_MB
            SPLIT_FILESIZE_MB = saved_split_size
            size_label = next(
                label for label, size_mb in _SEGMENT_SIZE_CHOICES.items()
                if size_mb == saved_split_size
            )
            self._settings_dialog.split_size_combo.setCurrentText(size_label)
            self._settings_dialog.split_size_combo.setEnabled(SPLIT_BY_FILESIZE_ENABLED)

        saved_split_mode = str(settings.get("split_filesize_mode", "during") or "during")
        if saved_split_mode in _SPLIT_FILESIZE_MODES.values():
            global SPLIT_FILESIZE_MODE
            SPLIT_FILESIZE_MODE = saved_split_mode
            mode_label = next(
                label for label, mode in _SPLIT_FILESIZE_MODES.items()
                if mode == saved_split_mode
            )
            self._settings_dialog.split_mode_combo.setCurrentText(mode_label)
            self._settings_dialog.split_mode_combo.setEnabled(SPLIT_BY_FILESIZE_ENABLED)

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
            if browser in _BROWSER_CHOICES and (not path or os.path.isdir(path)):
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

        global DOWNLOAD_BANDWIDTH_LIMIT_ENABLED, DOWNLOAD_BANDWIDTH_ESTIMATED_MB_S, DOWNLOAD_BANDWIDTH_SAFE_LIMIT_MB_S, DOWNLOAD_BANDWIDTH_PERCENT

        saved_bandwidth_enabled = settings.get("download_bandwidth_limit_enabled", False)
        try:
            DOWNLOAD_BANDWIDTH_LIMIT_ENABLED = bool(saved_bandwidth_enabled)
        except (TypeError, ValueError):
            DOWNLOAD_BANDWIDTH_LIMIT_ENABLED = False

        saved_bandwidth_estimate = settings.get("download_bandwidth_estimated_mb_s", 0)
        try:
            DOWNLOAD_BANDWIDTH_ESTIMATED_MB_S = float(saved_bandwidth_estimate)
        except (TypeError, ValueError):
            DOWNLOAD_BANDWIDTH_ESTIMATED_MB_S = 0.0

        saved_bandwidth_limit = settings.get("download_bandwidth_safe_limit_mb_s", 0)
        try:
            DOWNLOAD_BANDWIDTH_SAFE_LIMIT_MB_S = float(saved_bandwidth_limit)
        except (TypeError, ValueError):
            DOWNLOAD_BANDWIDTH_SAFE_LIMIT_MB_S = 0.0

        saved_percent = settings.get("download_bandwidth_percent", 80)
        try:
            DOWNLOAD_BANDWIDTH_PERCENT = int(saved_percent)
        except (TypeError, ValueError):
            DOWNLOAD_BANDWIDTH_PERCENT = 80

        self._settings_dialog.download_limit_check.setChecked(DOWNLOAD_BANDWIDTH_LIMIT_ENABLED)
        self._settings_dialog.download_percent_combo.setCurrentText(f"{DOWNLOAD_BANDWIDTH_PERCENT}%")
        self._settings_dialog._update_bandwidth_labels()

        if DOWNLOAD_BANDWIDTH_ESTIMATED_MB_S > 0:
            self._settings_dialog.download_speed_result_label.setText(
                f"Speed test completed: {DOWNLOAD_BANDWIDTH_ESTIMATED_MB_S:.1f} MB/s"
            )
            self._settings_dialog.download_speed_result_label.setStyleSheet("color: #7ae8a6; font-size: 11px;")

        for entry in saved.get("streams", []):
            url = entry["url"]
            auto = entry["auto_start"]
            self._add_stream(url=url, auto_start=auto, silent=True)

        if migrated_filename_template:
            self._save()

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

        # ── Browser-not-configured banner (hidden once a browser is set) ──
        self._browser_unset_banner = QLabel(
            "ℹ First time use: please open ⚙ Settings and set your Browser and User-Agent options."
        )
        self._browser_unset_banner.setWordWrap(True)
        self._browser_unset_banner.setStyleSheet(
            "background:#0d2a3a; color:#4FC3F7; font-size:18px; font-weight:bold;"
            "padding:9px 15px; border-radius:4px; border:1px solid #4FC3F7;"
        )
        self._browser_unset_banner.setVisible(False)
        vbox.addWidget(self._browser_unset_banner)

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
        hh.setSectionResizeMode(1, QHeaderView.Fixed)   # Site
        hh.setSectionResizeMode(2, QHeaderView.Stretch) # Username
        hh.setSectionResizeMode(3, QHeaderView.Fixed)   # Status
        hh.setSectionResizeMode(4, QHeaderView.Fixed)   # Duration
        hh.setSectionResizeMode(5, QHeaderView.Fixed)   # Res
        hh.setSectionResizeMode(6, QHeaderView.Fixed)   # FPS
        hh.setSectionResizeMode(7, QHeaderView.Fixed)   # Auto
        hh.setSectionResizeMode(8, QHeaderView.Fixed)   # DL
        hh.setSectionResizeMode(9, QHeaderView.Fixed)   # Start
        hh.setSectionResizeMode(10, QHeaderView.Fixed)  # Stop
        hh.setSectionResizeMode(11, QHeaderView.Fixed)  # ✕

        self._table.setColumnWidth(0, 112)   # Preview
        self._table.setColumnWidth(1, 90)    # Site
        self._table.setColumnWidth(3, 90)    # Status
        self._table.setColumnWidth(4, 90)    # Duration
        self._table.setColumnWidth(5, 80)    # Res
        self._table.setColumnWidth(6, 48)    # FPS
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
        self._table.viewport().setContextMenuPolicy(Qt.DefaultContextMenu)
        self._table.viewport().installEventFilter(self)
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

        download_info_row = QHBoxLayout()
        self._download_info_label = QLabel("Download info: idle")
        self._download_info_label.setObjectName("sectionLabel")
        self._download_info_label.setStyleSheet(
            "font-size:11px; color:#8fb3ff; padding:4px 0 0 0;"
        )
        download_info_row.addWidget(self._download_info_label)
        download_info_row.addStretch(1)
        self._disk_space_label = QLabel("—")
        self._disk_space_label.setObjectName("sectionLabel")
        self._disk_space_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._disk_space_label.setStyleSheet(
            "font-size:11px; color:#8fb3ff; padding:4px 0 0 0;"
        )
        download_info_row.addWidget(self._disk_space_label)
        vbox.addLayout(download_info_row)

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Ready")
        self._update_download_info()

        # Tooltips
        self._url_input.setToolTip("Enter a stream URL\nExample: https://chaturbate.com/username/")
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
                QMessageBox.warning(self, "Warning", f"'{extract_stream_username(url)}' is already in the list")
            return

        username = extract_stream_username(url)
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

        # Col 1 — site
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

        # Col 6 — FPS
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

        for widget in (
            thumb, site_lbl, status_lbl, dur, res_lbl, fps_lbl,
            auto_cb, cb_wrap, dl_lbl, start_btn, stop_btn, rm_btn,
        ):
            self._set_stream_context_menu(widget, url)

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

    def _set_stream_context_menu(self, widget: QWidget, url: str):
        pending = [widget]
        while pending:
            target = pending.pop()
            target.setProperty("streamContextUrl", url)
            target.setContextMenuPolicy(Qt.DefaultContextMenu)
            target.installEventFilter(self)
            pending.extend(
                target.findChildren(
                    QWidget,
                    options=Qt.FindChildOption.FindDirectChildrenOnly,
                )
            )

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.ContextMenu:
            if watched is self._table.viewport():
                row = self._table.rowAt(event.pos().y())
                for item in self.stream_items.values():
                    if item.row == row:
                        self._show_stream_context_menu(item.url, event.globalPos())
                        return True
                return row >= 0
            target = watched if isinstance(watched, QWidget) else None
            while target:
                url = target.property("streamContextUrl")
                if url:
                    self._show_stream_context_menu(url, event.globalPos())
                    return True
                target = target.parentWidget()
        return super().eventFilter(watched, event)

    def _show_stream_context_menu(self, url: str, global_position):
        item = self.stream_items.get(url)
        if not item:
            return
        menu = QMenu(self)
        menu.addAction("Copy username", lambda: QApplication.clipboard().setText(item.username))
        menu.addAction("Copy URL", lambda: QApplication.clipboard().setText(url))
        menu.addAction("Open in browser", lambda: self._open_stream_in_browser(url))
        menu.addSeparator()
        worker = self.download_workers.get(url)
        is_downloading = item.download_active or bool(worker and worker.isRunning())
        if is_downloading:
            menu.addAction("Stop download", lambda: self._stop_download(url))
        else:
            menu.addAction("Start download", lambda: self._manual_start(url))
        menu.addAction("Remove stream", lambda: self._remove_stream(url))
        menu.exec(global_position)

    def _open_stream_in_browser(self, url: str):
        if not QDesktopServices.openUrl(QUrl(url)):
            QMessageBox.warning(self, "Unable to Open URL", f"Could not open this URL:\n{url}")

    def _manual_start(self, url: str, show_space_warning: bool = True):
        worker = self.download_workers.get(url)
        if worker and worker.isRunning():
            self._log_msg(f"⚠ Already downloading {self.stream_items[url].username}", "#FF9800")
            return
        item = self.stream_items.get(url)
        if not item:
            return

        if item.current_status != StreamStatus.ONLINE:
            self._log_msg(
                f"⚠ Can't start {item.username}: stream is not currently online",
                "#FF9800"
            )
            return

        if DOWNLOAD_BANDWIDTH_LIMIT_ENABLED and DOWNLOAD_BANDWIDTH_SAFE_LIMIT_MB_S > 0:
            current_need = self._active_download_need_mb_s()
            stream_need = self._stream_need_mb_s(item)
            queue_limit = DOWNLOAD_BANDWIDTH_SAFE_LIMIT_MB_S + DOWNLOAD_BANDWIDTH_HEADROOM_MB_S
            if current_need + stream_need > queue_limit:
                item.queued = True
                self._log_msg(
                    f"⏳ Queued {item.username}: needs {stream_need:.1f} MB/s, limit is {DOWNLOAD_BANDWIDTH_SAFE_LIMIT_MB_S:.1f} MB/s",
                    "#FF9800"
                )
                self._update_download_info()
                return
            item.queued = False

        output_folder, space_error = select_download_output(
            self._out_input.text().strip() or "Downloads",
            SECONDARY_DOWNLOAD_ENABLED,
            SECONDARY_DOWNLOAD_OUTPUT,
            MINIMUM_FREE_SPACE_GB,
        )
        if space_error:
            self._log_msg(f"⚠ Download not started for {item.username}: {space_error}", "#F44336")
            self._status_bar.showMessage(space_error, 15000)
            if show_space_warning:
                QMessageBox.warning(self, "Insufficient Download Space", space_error)
            return
        if output_folder != os.path.abspath(
            os.path.expanduser(self._out_input.text().strip() or "Downloads")
        ):
            self._log_msg(
                f"💾 Primary drive is below {MINIMUM_FREE_SPACE_GB} GB free; "
                f"using secondary location for {item.username}",
                "#FF9800",
            )

        segment_number = None
        during_split_enabled = (
            SPLIT_BY_FILESIZE_ENABLED and SPLIT_FILESIZE_MODE == "during"
        )
        if during_split_enabled:
            split_session = self.split_sessions.setdefault(url, {"next_segment": 1})
            segment_number = split_session["next_segment"]
        else:
            self.split_sessions.pop(url, None)

        cached_probe = self.probe_cache.get(url) if during_split_enabled else None
        worker = DownloadWorker(
            url,
            item.username,
            output_folder,
            segment_number=segment_number,
            cached_probe=cached_probe,
            probe_cache_generation=self._probe_cache_generation.get(url, 0),
        )
        worker.setParent(self)
        worker.log_signal.connect(self._on_dl_log)
        worker.finished_signal.connect(self._on_dl_finished)
        worker.finished.connect(worker.deleteLater)
        worker.progress_signal.connect(self._on_dl_progress)
        worker.resolution_signal.connect(self._on_resolution)
        worker.auto_download_disabled_signal.connect(self._on_auto_download_disabled)
        worker.short_download_signal.connect(self._on_short_download)
        worker.remuxing_signal.connect(self._on_remuxing)
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

    def _clear_probe_cache(self, url: str):
        self.probe_cache.pop(url, None)
        self._probe_cache_generation[url] = self._probe_cache_generation.get(url, 0) + 1

    def _stop_download(self, url: str):
        self.split_sessions.pop(url, None)
        self._clear_probe_cache(url)
        worker = self.download_workers.get(url)
        if worker:
            worker.stop()
            if not worker.isRunning():
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

        for si in self.stream_items.values():
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
        if status != old:
            self._clear_probe_cache(url)

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
            self._manual_start(url, show_space_warning=False)
        elif status in (StreamStatus.OFFLINE, StreamStatus.PRIVATE, StreamStatus.AWAY):
            self.split_sessions.pop(url, None)
            if item.queued:
                item.queued = False
                self._log_msg(f"🧹 Cleared queued state for {item.username}: stream went {status.value}", "#777")
            if item.download_active:
                self._log_msg(f"🛑 Force stop: {item.username} ({status.value})", "#F44336")
                self._stop_download(url)

    def _on_resolution(self, url: str, res: str, fps: int):
        item = self.stream_items.get(url)
        if not item:
            return
        item.resolution = res
        item.fps = fps or 30
        worker = self.download_workers.get(url)
        if (
            res
            and worker
            and worker.split_enabled
            and worker.split_mode == "during"
            and worker.probe_cache_generation == self._probe_cache_generation.get(url, 0)
        ):
            self.probe_cache[url] = (res, fps or 30)

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

        # Set the FPS column (col 6)
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

    def _update_browser_unset_banner(self):
        self._browser_unset_banner.setVisible(not BROWSER)

    def _on_dl_finished(self, url: str):
        worker = self.download_workers.get(url)
        item = self.stream_items.get(url)
        if item:
            self._log_msg(f"✓ Finished: {item.username}", "#4CAF50")
        should_continue_split = bool(
            worker
            and worker.automatic_split
            and not worker.manual_stop
            and SPLIT_BY_FILESIZE_ENABLED
            and SPLIT_FILESIZE_MODE == "during"
            and worker.split_mode == "during"
            and url in self.split_sessions
            and item
            and item.current_status == StreamStatus.ONLINE
        )
        if should_continue_split and worker:
            self.split_sessions[url]["next_segment"] = (worker.segment_number or 0) + 1
        else:
            self.split_sessions.pop(url, None)
            self._clear_probe_cache(url)

        self._cleanup_download(url)
        if should_continue_split:
            if worker and not worker.isRunning():
                self._log_msg(
                    f"📦 Starting segment {self.split_sessions[url]['next_segment']} "
                    f"for {item.username}",
                    "#4CAF50",
                )
                self._manual_start(url, show_space_warning=False)
            else:
                self.split_sessions.pop(url, None)
                self._log_msg(
                    f"⚠ Previous segment did not release its worker; stopping split recording",
                    "#FF9800",
                )
        # Schedule a status re-check a few seconds after the download ends.
        # This avoids hammering the site immediately while still updating much
        # faster than waiting for the next full CHECK_INTERVAL cycle.
        QTimer.singleShot(5000, lambda: self._post_download_check(url))

    def _post_download_check(self, url: str):
        """Trigger one status check after a download finishes, if still tracked."""
        if url in self.stream_items:
            self.checker.force_check(url)

    def _on_remuxing(self, url: str, is_remuxing: bool):
        """Update the GUI to show 'Remuxing…' while Stripchat finishes a
        partial capture. This is the step that makes Stop All *appear* to
        hang for a few seconds -- the ffmpeg remux is still running."""
        item = self.stream_items.get(url)
        if not item:
            return
        if is_remuxing:
            lbl = self._table.cellWidget(item.row, 3)
            if lbl:
                lbl.setText("🎬 Remuxing")
                lbl.setStyleSheet(
                    "font-size:11px; color:#9df; background:#1e3a4a;"
                    "border-radius:4px; padding:2px 6px;"
                )
            self._log_msg(f"🎬 Remuxing partial capture: {item.username}", "#5b9bd5")
        else:
            lbl = self._table.cellWidget(item.row, 3)
            if lbl:
                text, fg, bg = STATUS_STYLE.get(
                    item.current_status, ("?", "#888", "#1a1a1a")
                )
                lbl.setText(text)
                lbl.setStyleSheet(
                    f"font-size:11px; color:{fg}; background:{bg};"
                    "border-radius:4px; padding:2px 6px;"
                )

    # ── Helpers ─────────────────────────────────

    def _cleanup_download(self, url: str):
        worker = self.download_workers.get(url)
        if worker:
            worker.stop()
            # Give Stripchat downloads longer to finish their remux
            timeout = 30000 if (STRIPCHAT_ADAPTER and STRIPCHAT_ADAPTER.matches(url)) else 5000
            if not worker.wait(timeout):
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
        self._process_queued_streams()
        self._settings_dialog._update_bandwidth_labels()

    def _format_size(self, bytes_value: float) -> str:
        if bytes_value >= 1024 * 1024 * 1024:
            return f"{bytes_value / (1024 * 1024 * 1024):.2f} GB"
        if bytes_value >= 1024 * 1024:
            return f"{bytes_value / (1024 * 1024):.2f} MB"
        if bytes_value >= 1024:
            return f"{bytes_value / 1024:.2f} KB"
        return f"{bytes_value:.0f} B"

    def _stream_need_mb_s(self, item: Optional[StreamItem]) -> float:
        if not item:
            return 0.0
        return estimate_bandwidth_need_mb_s(item.resolution, item.fps)

    def _active_download_need_mb_s(self) -> float:
        total = 0.0
        for item in self.stream_items.values():
            if item.download_active:
                total += self._stream_need_mb_s(item)
        return total

    def _queued_stream_count(self) -> int:
        return sum(1 for item in self.stream_items.values() if item.queued and not item.download_active)

    def _process_queued_streams(self):
        if not DOWNLOAD_BANDWIDTH_LIMIT_ENABLED or DOWNLOAD_BANDWIDTH_SAFE_LIMIT_MB_S <= 0:
            return
        for url, item in list(self.stream_items.items()):
            if item.queued and not item.download_active and self._stream_need_mb_s(item) > 0:
                current_need = self._active_download_need_mb_s()
                queue_limit = DOWNLOAD_BANDWIDTH_SAFE_LIMIT_MB_S + DOWNLOAD_BANDWIDTH_HEADROOM_MB_S
                if current_need + self._stream_need_mb_s(item) <= queue_limit:
                    item.queued = False
                    self._log_msg(f"▶ Resuming queued stream: {item.username}", "#4CAF50")
                    self._manual_start(url, show_space_warning=False)
                    return

    def _estimate_download_info(self) -> tuple[str, float, float]:
        active_items = [item for item in self.stream_items.values() if item.download_active]
        if not active_items:
            return "No active downloads", 0.0, 0.0

        total_mbps = 0.0
        for item in active_items:
            total_mbps += self._stream_need_mb_s(item) * 8.0

        total_per_hour_mb = total_mbps * 3600 / 8.0
        total_label = format_hours_value(total_per_hour_mb)

        required_speed_mb_s = total_mbps / 8.0
        speed_label = format_speed_mb_s(required_speed_mb_s)

        if DOWNLOAD_BANDWIDTH_LIMIT_ENABLED and DOWNLOAD_BANDWIDTH_SAFE_LIMIT_MB_S > 0:
            capacity_label = format_speed_mb_s(DOWNLOAD_BANDWIDTH_SAFE_LIMIT_MB_S)
            summary = f"{len(active_items)} active • Total {total_label} • Using {speed_label} / {capacity_label}"
            queued_count = self._queued_stream_count()
            if queued_count > 0:
                summary += f" • {queued_count} queued"
            return summary, total_per_hour_mb, required_speed_mb_s

        summary = f"{len(active_items)} active • Total {total_label} • {speed_label}"
        return summary, total_per_hour_mb, required_speed_mb_s

    def _update_download_info(self):
        self._process_queued_streams()
        summary, _, _ = self._estimate_download_info()
        if self._download_info_label:
            self._download_info_label.setText(f"Download info: {summary}")
        self._update_disk_space_info()

    @staticmethod
    def _folder_disk_usage(folder: str):
        """Read disk usage for a folder, including not-yet-created output folders."""
        path = os.path.abspath(os.path.expanduser(folder.strip() or "Downloads"))
        probe_path = path
        while not os.path.exists(probe_path):
            parent = os.path.dirname(probe_path)
            if parent == probe_path:
                raise OSError(f"No existing parent directory for {path}")
            probe_path = parent
        if not os.path.isdir(probe_path):
            raise NotADirectoryError(probe_path)
        return path, shutil.disk_usage(probe_path)

    def _update_disk_space_info(self):
        label = getattr(self, "_disk_space_label", None)
        if label is None:
            return

        primary_folder = self._out_input.text().strip() or "Downloads"
        try:
            selected_path, usage = self._folder_disk_usage(primary_folder)
        except OSError as exc:
            label.setText("Disk space unavailable")
            label.setToolTip(f"Cannot read disk space for {primary_folder}: {exc}")
            return

        threshold_bytes = max(1, MINIMUM_FREE_SPACE_GB) * 1024 ** 3
        secondary_error = None
        if (
            usage.free < threshold_bytes
            and SECONDARY_DOWNLOAD_ENABLED
            and SECONDARY_DOWNLOAD_OUTPUT.strip()
        ):
            try:
                secondary_path, secondary_usage = self._folder_disk_usage(
                    SECONDARY_DOWNLOAD_OUTPUT
                )
            except OSError as exc:
                secondary_error = str(exc)
            else:
                if secondary_usage.free >= threshold_bytes:
                    selected_path, usage = secondary_path, secondary_usage

        free_gb = usage.free / 1024 ** 3
        total_gb = usage.total / 1024 ** 3
        label.setText(f"{free_gb:.0f} GB / {total_gb:.0f} GB")
        tooltip = f"Free / total space on {selected_path}"
        if secondary_error:
            tooltip += f"\nSecondary location unavailable: {secondary_error}"
        label.setToolTip(tooltip)

    def _update_dl_ui(self, url: str, active: bool):
        item = self.stream_items.get(url)
        if not item:
            return
        row = item.row

        dl_lbl = self._table.cellWidget(row, 8)
        if dl_lbl:
            dl_lbl.setText("●" if active else "—")
            dl_lbl.setStyleSheet(
                f"color:{'#4fc' if active else '#444'}; font-size:{'16' if active else '14'}px;"
            )

        for col, enabled in ((9, not active), (10, active)):
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
            # The worker owns normal subprocess completion: it still needs to
            # drain yt-dlp output and validate/remux a duration-limited segment
            # before emitting finished_signal. Cleaning up as soon as poll()
            # sees an exited child can mark a successful split as a manual stop.
            if worker.process.poll() is not None:
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
            if item and item.download_active and time.time() - item.last_check_time > 60:
                self.checker.force_check(url)
                item.last_check_time = time.time()

    def _log_msg(self, message: str, color: str = "#ccc"):
        ts = datetime.now().strftime("%H:%M:%S")
        self._log.append(f'<span style="color:{color};">[{ts}] {message}</span>')
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())
        self._status_bar.showMessage(message, 3000)

    def _wait_for_stop(self, worker: QThread | StreamChecker) -> bool:
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
