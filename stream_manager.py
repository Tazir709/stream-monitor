import subprocess
import time
import threading
import psutil
import re
import os
import sys
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
    QFormLayout, QDialogButtonBox
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QEvent, QSize
from PySide6.QtGui import QImage, QPixmap, QFont, QColor, QPalette


# ─────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────

USE_COOKIES = True
BROWSER = "firefox:/home/pie/.floorp/31e5uirm.default-default" # Floorp is Firefox-based; pointing yt-dlp's firefox cookie-reader at its profile dir
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:153.0) Gecko/20100101 Firefox/153.0" # Optional user-agent string for yt-dlp. Leave empty to use default.
DOWNLOAD_OUTPUT = "Downloads" # Folder downloads are saved to. Settable from the Settings dialog; persisted across sessions.
MAX_DOWNLOAD_RESOLUTION = "1920x1080" # 3840x2160, 1920x1080, 1280x720, 960x540, 640x360
MAX_DOWNLOAD_FPS = 30 # 60 or 30

# Prevents unstable streams from continuously rejoining the download queue
# when the system is under heavy load. Prioritizing fewer stable downloads
# is preferable to having many unstable downloads repeatedly stopping and restarting.
AUTO_DOWNLOAD_DISABLE_SECONDS = 300 # Disable auto-download if the stream is shorter than this many seconds. 0 to disable

DOWNLOAD_BITRATE_KBPS = { # Usage calculation based on 30 fps
    "640x360": 896,
    "960x540": 1696,
    "1280x720": 3096,
    "1920x1080": 5128,
    "3840x2160": 7192,
}


def get_download_format_selector() -> str:
    max_height = int(MAX_DOWNLOAD_RESOLUTION.split("x")[1])
    max_fps = MAX_DOWNLOAD_FPS
    
    # Build a format selector with multiple fallback levels
    return (
        # Level 1: Exact match (preferred resolution + FPS)
        f"bestvideo[height<={max_height}][fps<={max_fps}]+bestaudio/"
        # Level 2: Same resolution, any FPS (if FPS is too high, just take what's available)
        f"bestvideo[height<={max_height}]+bestaudio/"
        # Level 3: Lower resolution, same FPS
        f"bestvideo[fps<={max_fps}]+bestaudio/"
        # Level 4: Anything goes (best available)
        f"bestvideo+bestaudio/"
        # Level 5: Ultimate fallback (if everything else fails)
        f"best"
    )


def get_user_agent() -> str:
    return (USER_AGENT or "").strip()


def should_disable_auto_download(duration_seconds: int) -> bool:
    return duration_seconds < AUTO_DOWNLOAD_DISABLE_SECONDS


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


def log_exception(prefix: str) -> None:
    print(f"[Debug] {prefix}")
    traceback.print_exc()


def format_error_message(stderr: str, stdout: str = "") -> str:
    text = f"{stderr}\n{stdout}".lower()
    
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
            if USE_COOKIES:
                cmd.extend(["--cookies-from-browser", BROWSER])
            cmd.append(page_url)
            r = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=30
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

        host = page_url.lower()
        if "chaturbate" in host:
            referer = "https://chaturbate.com/"
        else:
            referer = ""

        W, H = 320, 180
        user_agent = get_user_agent() or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        cmd = [
            FFMPEG_PATH,
            "-user_agent", user_agent,
            *([ "-headers", f"Referer: {referer}\r\n"] if referer else []),
            "-timeout", "10000000",
            "-i", stream_url,
            "-frames:v", "1",
            "-vf", f"scale={W}:{H}",
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
    resolution_signal         = Signal(str, str)   # (url, "1920x1080")
    auto_download_disabled_signal = Signal(str)      # url

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

    def __init__(self, stream_url: str, username: str, output_path: str = "Downloads"):
        super().__init__()
        self.setObjectName(f"DownloadWorker-{username}")
        self.stream_url  = stream_url
        self.username    = username
        self.output_path = output_path
        self.process: Optional[subprocess.Popen] = None
        self.is_running  = False
        self._rate_limiter = RateLimiter(2.0)
        self._line_queue: Queue = Queue()
        self._started_at = 0.0
        os.makedirs(output_path, exist_ok=True)

    def _probe_resolution(self) -> str:
        """Ask yt-dlp for the selected format's resolution before starting the download."""
        try:
            cmd = [
                YTDLP_PATH,
                "--no-playlist",
                "--format", get_download_format_selector(),
                "--print", "%(width)sx%(height)s",
            ]
            user_agent = get_user_agent()
            if user_agent:
                cmd.extend(["--user-agent", user_agent])
            if USE_COOKIES:
                cmd.extend(["--cookies-from-browser", BROWSER])
            cmd.append(self.stream_url)
            r = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode == 0:
                res = r.stdout.strip().split("\n")[0]
                if re.match(r"^\d+x\d+$", res):
                    return res
            if r.returncode != 0:
                err = format_error_message(r.stderr, r.stdout)
                print(f"[Debug] DownloadWorker probe failed for {self.username}: {err}")
        except Exception:
            pass
        return ""

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
            res = self._probe_resolution()
            if res:
                self.log_signal.emit(self.username, f"📐 Resolution: {res}")
                self.resolution_signal.emit(self.stream_url, res)
            else:
                self.log_signal.emit(self.username, "📐 Resolution unknown")
                self.resolution_signal.emit(self.stream_url, "")

            self._started_at = time.time()

            current_time = datetime.now().strftime("%Y-%m-%d %H_%M")

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
            if USE_COOKIES:
                cmd.extend(["--cookies-from-browser", BROWSER])
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
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

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
                if should_disable_auto_download(elapsed):
                    self.auto_download_disabled_signal.emit(self.stream_url)

        except Exception as e:
            hint = format_error_message(str(e))
            self.log_signal.emit(self.username, f"❌ Error: {str(e)[:100]} | {hint}")
            print(f"[Debug] DownloadWorker run failed for {self.username}: {e!r}")
            traceback.print_exc()
        finally:
            self.is_running = False
            self.finished_signal.emit(self.stream_url)

    def stop(self):
        """Signal the process tree to stop. Does NOT touch self.process — run() owns it."""
        proc = self.process
        if not proc:
            return
        self.log_signal.emit(self.username, "⏹ Stopping download…")
        self.is_running = False
        try:
            parent = psutil.Process(proc.pid)
            children = parent.children(recursive=True)
            for p in children:
                try: p.terminate()
                except psutil.NoSuchProcess: pass
            parent.terminate()
            gone, alive = psutil.wait_procs(children + [parent], timeout=5)
            for p in alive:
                try: p.kill()
                except psutil.NoSuchProcess: pass
        except (psutil.NoSuchProcess, ProcessLookupError):
            pass
        except Exception as e:
            self.log_signal.emit(self.username, f"❌ Stop error: {str(e)[:80]}")
            print(f"[Debug] DownloadWorker stop failed for {self.username}: {e!r}")
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
            if USE_COOKIES:
                cmd.extend(["--cookies-from-browser", BROWSER])
            cmd.append(url)
            r = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=30,
            )
            stdout = r.stdout.strip().lower()
            stderr = r.stderr.lower()

            if r.returncode != 0:
                if "currently away" in stderr:
                    return StreamStatus.AWAY, "🌙 Away"
                if "hidden session" in stderr:
                    return StreamStatus.PRIVATE, "🔒 Hidden session (private)"
                if "private" in stderr:
                    return StreamStatus.PRIVATE, "🔒 Private show"
                if "age restricted" in stderr or "age-restricted" in stderr:
                    return StreamStatus.PRIVATE, "🔒 Age restricted"
                if "offline" in stderr:
                    return StreamStatus.OFFLINE, "💤 Offline"
                if "video unavailable" in stderr or "not found" in stderr:
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


def load_saved_streams() -> list[dict]:
    try:
        with open(SAVE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [
                {
                    "url": e["url"],
                    "auto_start": bool(e.get("auto_start", False)),
                    "user_agent": e.get("user_agent", ""),
                    "output_folder": e.get("output_folder", ""),
                    "max_resolution": e.get("max_resolution", ""),
                    "max_fps": e.get("max_fps", ""),
                }
                for e in data
                if isinstance(e, dict) and e.get("url")
            ]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return []


def save_streams(stream_items: dict) -> None:
    payload = [
        {
            "url": item.url,
            "auto_start": item.auto_start,
            "user_agent": USER_AGENT,
            "output_folder": DOWNLOAD_OUTPUT,
            "max_resolution": MAX_DOWNLOAD_RESOLUTION,
            "max_fps": MAX_DOWNLOAD_FPS,
        }
        for item in stream_items.values()
    ]
    try:
        with open(SAVE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except OSError:
        pass


# ─────────────────────────────────────────────
#  Settings dialog
# ─────────────────────────────────────────────

_BROWSER_CHOICES = ["firefox", "chrome", "chromium", "edge", "brave", "opera", "safari"]
_RESOLUTION_CHOICES = ["640x360", "960x540", "1280x720", "1920x1080", "3840x2160"]
_FPS_CHOICES = ["30", "60"]


class SettingsDialog(QDialog):
    """Setup-once config — output folder, cookies, User-Agent — lives here
    instead of the main toolbar, so day-to-day use (paste a URL, go) stays
    uncluttered."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)

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

        # ── Cookies (always on — no toggle, just where to read them from) ──
        self.browser_combo = QComboBox()
        self.browser_combo.addItems(_BROWSER_CHOICES)
        self.cookie_path_input = QLineEdit()
        self.cookie_path_input.setPlaceholderText("Profile folder (leave blank for the default profile)")
        cookie_browse = QPushButton("Browse…")
        cookie_browse.clicked.connect(self._browse_cookies)
        cookie_row = QHBoxLayout()
        cookie_row.addWidget(self.browser_combo)
        cookie_row.addWidget(self.cookie_path_input, 1)
        cookie_row.addWidget(cookie_browse)
        form.addRow("Cookie source:", cookie_row)

        cookie_hint = QLabel(
            "Cookies are always used — most streams need them now. Pick which browser,\n"
            "and if it's a Firefox-based browser not in the list (Floorp, Zen, LibreWolf…),\n"
            "browse to its profile folder directly."
        )
        cookie_hint.setStyleSheet("color: #888; font-size: 11px;")
        form.addRow("", cookie_hint)

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
        self.browser_combo.currentTextChanged.connect(self._on_cookie_source_changed)
        self.cookie_path_input.textChanged.connect(self._on_cookie_source_changed)
        self.user_agent_input.textChanged.connect(self._on_user_agent_changed)

    def _load_from_globals(self):
        self.out_input.setText(DOWNLOAD_OUTPUT)
        if MAX_DOWNLOAD_RESOLUTION in _RESOLUTION_CHOICES:
            self.resolution_combo.setCurrentText(MAX_DOWNLOAD_RESOLUTION)
        if str(MAX_DOWNLOAD_FPS) in _FPS_CHOICES:
            self.fps_combo.setCurrentText(str(MAX_DOWNLOAD_FPS))
        self.user_agent_input.setText(USER_AGENT)
        browser, _, path = BROWSER.partition(":")
        if browser in _BROWSER_CHOICES:
            self.browser_combo.setCurrentText(browser)
        self.cookie_path_input.setText(path)

    def _on_output_changed(self, text: str):
        global DOWNLOAD_OUTPUT
        DOWNLOAD_OUTPUT = text.strip() or "Downloads"

    def _on_resolution_changed(self, text: str):
        global MAX_DOWNLOAD_RESOLUTION
        MAX_DOWNLOAD_RESOLUTION = text

    def _on_fps_changed(self, text: str):
        global MAX_DOWNLOAD_FPS
        MAX_DOWNLOAD_FPS = int(text)

    def _browse_output(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose download output folder", self.out_input.text() or "."
        )
        if folder:
            self.out_input.setText(folder)

    def _browse_cookies(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose browser profile folder",
            self.cookie_path_input.text() or os.path.expanduser("~"),
        )
        if folder:
            self.cookie_path_input.setText(folder)

    def _on_cookie_source_changed(self, _=None):
        global BROWSER
        path = self.cookie_path_input.text().strip()
        browser = self.browser_combo.currentText()
        BROWSER = f"{browser}:{path}" if path else browser

    def _on_user_agent_changed(self, text: str):
        global USER_AGENT
        USER_AGENT = text.strip()


class StreamDownloaderGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Stream Download Manager")
        self.setGeometry(100, 100, 920, 780)
        self.setMinimumSize(920, 600)

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

        # Process health check
        self._proc_timer = QTimer(self)
        self._proc_timer.timeout.connect(self._check_processes)
        self._proc_timer.start(5000)

        self._settings_dialog = SettingsDialog(self)
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
        saved_user_agent = saved[0].get("user_agent", "").strip()
        if saved_user_agent:
            # Restore the last-used UA across sessions — but only if there
            # actually was one saved, so an empty entry (e.g. from before
            # a UA was ever configured) doesn't clobber a real config value.
            global USER_AGENT
            USER_AGENT = saved_user_agent
            if self._user_agent_input:
                self._user_agent_input.setText(USER_AGENT)
        saved_output = saved[0].get("output_folder", "").strip()
        if saved_output:
            global DOWNLOAD_OUTPUT
            DOWNLOAD_OUTPUT = saved_output
            if self._out_input:
                self._out_input.setText(DOWNLOAD_OUTPUT)
        saved_resolution = str(saved[0].get("max_resolution", "")).strip()
        if saved_resolution in _RESOLUTION_CHOICES:
            global MAX_DOWNLOAD_RESOLUTION
            MAX_DOWNLOAD_RESOLUTION = saved_resolution
            self._settings_dialog.resolution_combo.setCurrentText(saved_resolution)
        saved_fps = str(saved[0].get("max_fps", "")).strip()
        if saved_fps in _FPS_CHOICES:
            global MAX_DOWNLOAD_FPS
            MAX_DOWNLOAD_FPS = int(saved_fps)
            self._settings_dialog.fps_combo.setCurrentText(saved_fps)
        for entry in saved:
            url = entry["url"]
            auto = entry["auto_start"]
            self._add_stream(url=url, auto_start=auto, silent=True)
        if saved:
            self._log_msg(f"📂 Restored {len(saved)} stream(s) from last session", "#888")

    def _save(self):
        save_streams(self.stream_items)

    def _open_settings(self):
        self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()

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

        # ── Splitter: table / log ──
        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)

        # Table
        self._table = QTableWidget()
        self._table.setColumnCount(10)
        self._table.setHorizontalHeaderLabels([
            "Preview", "Username", "Status", "Duration",
            "Res", "Auto", "DL", "Start", "Stop", "✕",
        ])
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Fixed)
        hh.setSectionResizeMode(3, QHeaderView.Fixed)
        hh.setSectionResizeMode(4, QHeaderView.Fixed)
        hh.setSectionResizeMode(5, QHeaderView.Fixed)
        hh.setSectionResizeMode(6, QHeaderView.Fixed)
        hh.setSectionResizeMode(7, QHeaderView.Fixed)
        hh.setSectionResizeMode(8, QHeaderView.Fixed)
        hh.setSectionResizeMode(9, QHeaderView.Fixed)
        self._table.setColumnWidth(0, 112)
        self._table.setColumnWidth(2, 90)
        self._table.setColumnWidth(3, 90)
        self._table.setColumnWidth(4, 80)
        self._table.setColumnWidth(5, 48)
        self._table.setColumnWidth(6, 36)
        self._table.setColumnWidth(7, 85)
        self._table.setColumnWidth(8, 85)
        self._table.setColumnWidth(9, 90)
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

        # Col 1 — username
        u_item = QTableWidgetItem(username)
        u_item.setToolTip(url)
        u_item.setForeground(QColor("#d0d0e8"))
        self._table.setItem(row, 1, u_item)

        # Col 2 — status
        status_lbl = QLabel("⏳ Checking")
        status_lbl.setAlignment(Qt.AlignCenter)
        status_lbl.setStyleSheet("font-size:11px; color:#666;")
        self._table.setCellWidget(row, 2, status_lbl)

        # Col 3 — duration
        dur = DownloadTimer()
        self._table.setCellWidget(row, 3, dur)
        self.download_timers[url] = dur

        # Col 4 — resolution
        res_lbl = QLabel("—")
        res_lbl.setAlignment(Qt.AlignCenter)
        res_lbl.setStyleSheet("font-size:11px; color:#555; font-family:'Courier New',monospace;")
        self._table.setCellWidget(row, 4, res_lbl)

        # Col 5 — auto-start
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
        self._table.setCellWidget(row, 5, cb_wrap)

        # Col 6 — DL indicator
        dl_lbl = QLabel("—")
        dl_lbl.setAlignment(Qt.AlignCenter)
        dl_lbl.setStyleSheet("color:#444; font-size:14px;")
        self._table.setCellWidget(row, 6, dl_lbl)

        # Col 7 — Start
        start_btn = QPushButton("▶ Start")
        start_btn.setObjectName("startBtn")
        start_btn.setToolTip("Manually start downloading this stream now")
        start_btn.clicked.connect(lambda _, u=url: self._manual_start(u))
        self._table.setCellWidget(row, 7, start_btn)

        # Col 8 — Stop
        stop_btn = QPushButton("⏹ Stop")
        stop_btn.setObjectName("stopBtn")
        stop_btn.setEnabled(False)
        stop_btn.setToolTip("Stop the current download for this stream")
        stop_btn.clicked.connect(lambda _, u=url: self._stop_download(u))
        self._table.setCellWidget(row, 8, stop_btn)

        # Col 9 — Remove
        rm_btn = QPushButton("Remove")
        rm_btn.setObjectName("removeBtn")
        rm_btn.setToolTip("Remove this stream from the list")
        rm_btn.clicked.connect(lambda _, u=url: self._remove_stream(u))
        self._table.setCellWidget(row, 9, rm_btn)

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
        if checked and item.current_status == StreamStatus.ONLINE and not item.download_active:
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
        lbl = self._table.cellWidget(item.row, 2)
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

    def _on_resolution(self, url: str, res: str):
        item = self.stream_items.get(url)
        if not item:
            return
        item.resolution = res
        res_lbl = self._table.cellWidget(item.row, 4)
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
        self._update_download_info()

    def _on_dl_log(self, username: str, message: str):
        self._log_msg(f"[{username}] {message}", "#5b9bd5")

    def _on_dl_progress(self, username: str, percent: int):
        self._status_bar.showMessage(f"{username}: {percent}%", 1500)

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
                f"⚠ Auto-start disabled for {item.username}: download stopped before 10 minutes",
                "#FF9800"
            )

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
            kbps = DOWNLOAD_BITRATE_KBPS.get(resolution, 0)
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
            details.append(f"{item.username}: {resolution} → {size_label}")

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

        dl_lbl = self._table.cellWidget(row, 6)
        if dl_lbl:
            dl_lbl.setText("●" if active else "—")
            dl_lbl.setStyleSheet(
                f"color:{'#4fc' if active else '#444'}; font-size:{'16' if active else '14'}px;"
            )

        for col, enabled in ((7, not active), (8, active)):
            btn = self._table.cellWidget(row, col)
            if btn:
                btn.setEnabled(enabled)

        if not active:
            res_lbl = self._table.cellWidget(row, 4)
            if res_lbl:
                res_lbl.setText("—")
                res_lbl.setStyleSheet(
                    "font-size:11px; color:#555; font-family:'Courier New',monospace;"
                )
            item.resolution = ""

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
