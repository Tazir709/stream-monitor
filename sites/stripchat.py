# STRIPCHAT INTEGRATION MODULE

# This module provides native Stripchat support for the Stream Monitor GUI.
# It handles:
#    • Stream status detection (public/private/ticket/offline)
#    • HLS playlist parsing and resolution selection
#    • Mouflon URL decryption (Stripchat's custom encryption)
#    • Live stream downloading with segment reassembly
#    • Preview frame extraction for thumbnails

#  DESIGN PHILOSOPHY:

#  All heavy lifting (downloading, remuxing) runs in QThreads to keep the
#  GUI responsive. The module is designed to be imported by
#  stream_manager.py and provides clean dataclass interfaces.

#  PERFORMANCE NOTES:

#  • SHA256 digests are cached per pdkey to avoid redundant hashing
#  • Compiled regex patterns are reused across all operations
#  • requests.Session is used for connection reuse
#  • ffmpeg console windows are hidden on Windows (CREATE_NO_WINDOW)

#  DEPENDENCIES:

#  • Python 3.8+
#  • requests (HTTP client)
#  • PySide6 (QThread for GUI integration)
#  • ffmpeg (must be in PATH for remuxing and preview extraction, must have native H.264 decoder for preview)

#  USAGE IN STREAM_MANAGER:

#  1. Status checking: extract_stripchat_data(username) → status dict
#  2. Preview: get_preview_frame(url) → PreviewFrame with RGB image
#  3. Download: StripchatDownloader(DownloadRequest) → QThread


import base64
import hashlib
import re
import requests
import time
import subprocess
import os
import json
import tempfile
import sys
import functools
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, urljoin
from datetime import datetime
from PySide6.QtCore import QThread, Signal, Qt
from .base import SiteStatus
from .registry import hostname_from_url

IS_WINDOWS = sys.platform == "win32"

# ─────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────

#  Stripchat uses "Mouflon" to obfuscate segment URLs. Each pkey maps to a
#  pdkey. The pdkey is used to decrypt segment URLs via XOR with SHA256(pdkey).
#  Keys are sourced from the stripchat_mouflon project (MIT License).
#  See: https://github.com/kesamom/stripchat_mouflon
KEYS = {
    "Zokee2OhPh9kugh4": "Quean4cai9boJa5a",
    "Zeechoej4aleeshi": "ubahjae7goPoodi6",
    "Ook7quaiNgiyuhai": "EQueeGh2kaewa3ch",
    "Fq6m2TO2ZeBkRPm9": "xb6di1NF9EFXHUwb",
    "GrRncsoByZmsiT6L": "NigHYyOD9l4rvAEb",
    "1Dzcc6OjP73LKbtI": "Y64UVwX5RrIWnOLp",
    "N2oLovTIXb0o28Uj": "ABE7Sj8jh3oPM2ae",
    "NTK9aqcLmNFMWrpQ": "tOcYOap4Ty1l9Jzb",
    "7uUnbD0jMCB9GH32": "lzCQ6QBTnLpB0zMF",
    "Ohi7eTRBpkAuML0l": "kExe29N2sLFrHGqu",
    "OLzu7QlySkG2fVRn": "CsovScFH9VirSJ4Z"
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) "
        "Gecko/20100101 Firefox/154.0"
    ),
    "Referer": "https://stripchat.com/",
    "Origin": "https://stripchat.com",
}

PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) "
        "Gecko/20100101 Firefox/154.0"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "identity",
    "Referer": "https://stripchat.com/",
}


def _hidden_console_kwargs() -> dict:
    """Hide console windows for child processes on Windows."""
    kwargs = {}
    if IS_WINDOWS:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def _ffmpeg_supports_native_h264() -> bool:
    """Return True when ffmpeg exposes a native H.264 decoder."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-decoders"],
            capture_output=True,
            text=True,
            timeout=10,
            **_hidden_console_kwargs(),
        )

        for line in (result.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].lower() == "h264":
                return True

        return False

    except Exception:
        return False


FFMPEG_HAS_NATIVE_H264_DECODER = _ffmpeg_supports_native_h264()


INIT_SEGMENT_RE = re.compile(r'#EXT-X-MAP:URI="([^"]+)"')
MOUFLON_URL_RE = re.compile(r"#EXT-X-MOUFLON:URI:(https?://[^\r\n]+)")
PART_SEGMENT_RE = re.compile(r"_part\d+\.mp4$")
SEGMENT_SEQUENCE_RE = re.compile(r"_(\d+)\.mp4$")
MOUFLON_SEGMENT_RE = re.compile(r"_([^_]+)_(\d+(?:_part\d+)?)\.mp4")
STRIPCHAT_MEDIA_SEGMENT_SECONDS = 2.0
STRIPCHAT_CALIBRATION_SIZE_FACTOR = 0.90


def calculate_segment_count_for_size(target_size_mb: int, segment_bytes: int) -> int:
    """Estimate the number of two-second media segments for a target file size."""
    if target_size_mb <= 0 or segment_bytes <= 0:
        return 0
    target_bytes = target_size_mb * 1024 * 1024
    return max(
        1,
        int(target_bytes * STRIPCHAT_CALIBRATION_SIZE_FACTOR / segment_bytes),
    )


@functools.lru_cache(maxsize=512)
def get_pdkey_digest(pdkey: str) -> bytes:
    """Return a cached SHA256 digest for a Mouflon pdkey."""
    return hashlib.sha256(pdkey.encode("utf-8")).digest()

# Status constants for Stripchat streams
STREAM_STATUS_PUBLIC = "public"
STREAM_STATUS_GROUP_SHOW = "groupShow"
STREAM_STATUS_PRIVATE = "private"
STREAM_STATUS_P2P = "p2p"
STREAM_STATUS_AWAY = "away"
STREAM_STATUS_IDLE = "idle"
STREAM_STATUS_OFF = "off"
STREAM_STATUS_VIRTUAL_PRIVATE = "virtualPrivate"

# Statuses that are inaccessible for downloading
INACCESSIBLE_STATUSES = {
    STREAM_STATUS_GROUP_SHOW,
    STREAM_STATUS_PRIVATE,
    STREAM_STATUS_P2P,
    STREAM_STATUS_VIRTUAL_PRIVATE,
}

def get_user_agent_headers(user_agent: Optional[str] = None):
    """Return request headers with the configured GUI user-agent applied."""
    request_headers = HEADERS.copy()
    request_page_headers = PAGE_HEADERS.copy()
    if user_agent:
        request_headers["User-Agent"] = user_agent
        request_page_headers["User-Agent"] = user_agent
    return request_headers, request_page_headers

@dataclass
class StreamInfo:
    """Information about a Stripchat stream for use by the downloader."""
    url: str
    username: str
    model_id: int
    is_live: bool
    is_accessible: bool
    status: str
    playlist_url: str
    width: int
    height: int
    fps: Optional[float]
    pdkey: str

@dataclass
class PreviewFrame:
    """A decoded frame extracted from a Stripchat stream."""
    image: bytes      # Raw RGB24 image data (W x H x 3)
    width: int
    height: int

@dataclass
class DownloadRequest:
    """Request parameters for a Stripchat download."""
    stream_url: str
    output_file: Optional[str] = None
    target_height: int = 1080
    remux_on_finish: bool = True
    user_agent: Optional[str] = None
    ffmpeg_path: str = "ffmpeg"
    max_duration_seconds: int = 0
    target_size_mb: int = 0


@dataclass(frozen=True)
class StripchatDownloadResult:
    """Terminal result returned by the native download session."""

    status: str
    message: str = ""
    split_requested: bool = False
    output_file: Optional[str] = None

@dataclass
class DownloadProgress:
    """Progress update during a Stripchat download."""
    segment_count: int
    bytes_downloaded: int
    total_segments: Optional[int] = None
    current_segment: Optional[int] = None
    status: str = "downloading"  # "downloading", "remuxing", "finished", "error"


#  Main download worker. Runs in a background thread to keep GUI responsive.
#  Emits signals for progress, logs, and completion.
#
#  Lifecycle:
#  1. __init__() - Setup request and state
#  2. run() - Main download loop (called when thread starts)
#  3. stop() - Called by GUI to gracefully stop downloading
#  4. Signals emitted back to GUI thread
#
#  The download loop:
#  1. Fetch playlist (HLS master)
#  2. Download init segment (codec/container header)
#  3. Loop: fetch playlist, download new segments, decrypt URLs
#  4. If no new segments for 8 seconds, assume stream ended
#  5. Remux with ffmpeg to create playable MP4
class StripchatDownloader(QThread):
    """A QThread-based downloader for Stripchat streams."""

    progress_signal = Signal(DownloadProgress)
    finished_signal = Signal(str)  # output_file path
    error_signal = Signal(str)     # error message
    log_signal = Signal(str)       # log message

    def __init__(self, request: DownloadRequest):
        super().__init__()
        self.request = request
        self._running = True
        self._temp_file: Optional[str] = None
        self._ffmpeg_process: Optional[subprocess.Popen] = None
        self.duration_reached = False

    def stop(self):
        """Stop the download gracefully.

        For Stripchat, we keep the partial capture and allow the final remux to
        complete when it is already viable, because the raw HLS container is not
        watchable without remuxing.
        """
        self._running = False

    def run(self):
        """Main download loop running in a separate thread."""
        # Use the user_agent from the request if provided
        headers = HEADERS.copy()
        if self.request.user_agent:
            headers["User-Agent"] = self.request.user_agent

        page_headers = PAGE_HEADERS.copy()
        if self.request.user_agent:
            page_headers["User-Agent"] = self.request.user_agent

        session = requests.Session()
        session.headers.update(headers)

        try:
            # Get stream info
            info = get_stream_info(
                self.request.stream_url,
                self.request.target_height,
                user_agent=self.request.user_agent,
            )

            # Check if stream is accessible
            if not info.is_accessible:
                status_msg = {
                    STREAM_STATUS_GROUP_SHOW: "Ticket/Group show - requires purchase",
                    STREAM_STATUS_PRIVATE: "Private show - not accessible",
                    STREAM_STATUS_P2P: "P2P show - requires payment",
                    STREAM_STATUS_VIRTUAL_PRIVATE: "Virtual private show - not accessible",
                }.get(info.status, f"Stream status: {info.status}")
                self.error_signal.emit(f"Stream not accessible: {status_msg}")
                return

            if not info.is_live:
                self.error_signal.emit("Stream is not currently live")
                return

            # Generate output filename if not provided
            if self.request.output_file is None:
                timestamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
                output_stem = f"{info.username} {timestamp}"
                output_file = f"{output_stem}.mp4"
            else:
                output_file = self.request.output_file

            temp_file = f"{output_file}.temp"
            self._temp_file = temp_file

            self.log_signal.emit(f"Stream: {info.username} ({info.width}x{info.height})")

            # Fetch the initial playlist
            response = session.get(info.playlist_url, headers=headers, timeout=10)
            response.raise_for_status()
            playlist = response.text

            # Download init segment
            init_match = INIT_SEGMENT_RE.search(playlist)
            if not init_match:
                self.error_signal.emit("No HLS init segment found")
                return

            init_url = urljoin(info.playlist_url, init_match.group(1))
            init_response = session.get(init_url, headers=page_headers, timeout=10)
            init_response.raise_for_status()

            downloaded_sequences = set()
            segment_count = 0
            bytes_downloaded = 0
            last_new_segment_at = time.monotonic()
            playlist_failures = 0
            max_playlist_failures = 3

            self.log_signal.emit("Collecting segments...")

            with open(temp_file, "wb") as container:
                container.write(init_response.content)
                bytes_downloaded += len(init_response.content)
                download_started_at = time.monotonic()
                target_segment_count: Optional[int] = None

                while self._running:
                    duration_limit_reached = (
                        target_segment_count is not None
                        and segment_count >= target_segment_count
                    )
                    fallback_duration_reached = (
                        target_segment_count is None
                        and self.request.max_duration_seconds > 0
                        and time.monotonic() - download_started_at
                        >= self.request.max_duration_seconds
                    )
                    if duration_limit_reached or fallback_duration_reached:
                        self.duration_reached = True
                        target_duration = (
                            target_segment_count * STRIPCHAT_MEDIA_SEGMENT_SECONDS
                            if target_segment_count is not None
                            else self.request.max_duration_seconds
                        )
                        self.log_signal.emit(
                            f"Segment duration reached ({target_duration:g}s); "
                            "finalizing segment."
                        )
                        break
                    try:
                        response = session.get(info.playlist_url, headers=headers, timeout=10)
                        response.raise_for_status()
                        playlist = response.text
                        playlist_failures = 0
                    except requests.RequestException as e:
                        if isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code == 403:
                            self.log_signal.emit("Playlist returned 403; stream ended.")
                            break

                        playlist_failures += 1
                        if playlist_failures >= max_playlist_failures:
                            self.log_signal.emit("No playlist response after multiple attempts; stream ended.")
                            break

                        time.sleep(2)
                        continue

                    mouflon_urls = MOUFLON_URL_RE.findall(playlist)
                    complete_urls = [u for u in mouflon_urls if not PART_SEGMENT_RE.search(u)]
                    downloaded_segment = False

                    for encrypted_url in complete_urls:
                        if not self._running:
                            break

                        match = SEGMENT_SEQUENCE_RE.search(encrypted_url)
                        if not match:
                            continue

                        sequence = int(match.group(1))
                        if sequence in downloaded_sequences:
                            continue

                        decrypted_url = decrypt_segment_url(encrypted_url, info.pdkey)

                        try:
                            response = session.get(decrypted_url, headers=headers, timeout=10, stream=True)
                            response.raise_for_status()

                            byte_count = 0
                            for chunk in response.iter_content(chunk_size=1024 * 1024):
                                if chunk:
                                    container.write(chunk)
                                    byte_count += len(chunk)

                            downloaded_sequences.add(sequence)
                            segment_count += 1
                            bytes_downloaded += byte_count
                            downloaded_segment = True
                            container.flush()

                            if (
                                self.request.target_size_mb > 0
                                and target_segment_count is None
                                and byte_count > 0
                            ):
                                target_segment_count = calculate_segment_count_for_size(
                                    self.request.target_size_mb,
                                    byte_count,
                                )
                                measured_mbps = (
                                    byte_count
                                    * 8
                                    / STRIPCHAT_MEDIA_SEGMENT_SECONDS
                                    / 1_000_000
                                )
                                calibrated_duration = (
                                    target_segment_count
                                    * STRIPCHAT_MEDIA_SEGMENT_SECONDS
                                )
                                self.log_signal.emit(
                                    f"Calibrated from first "
                                    f"{STRIPCHAT_MEDIA_SEGMENT_SECONDS:g}s segment "
                                    f"({byte_count:,} bytes, {measured_mbps:.2f} Mbps): "
                                    f"targeting {target_segment_count} segments "
                                    f"({calibrated_duration:g}s) for "
                                    f"{self.request.target_size_mb} MB."
                                )

                            self.progress_signal.emit(DownloadProgress(
                                segment_count=segment_count,
                                bytes_downloaded=bytes_downloaded,
                                current_segment=sequence,
                            ))

                            if (
                                target_segment_count is not None
                                and segment_count >= target_segment_count
                            ):
                                self.duration_reached = True
                                self.log_signal.emit(
                                    f"Calibrated segment count reached "
                                    f"({segment_count}/{target_segment_count}); "
                                    "finalizing segment."
                                )
                                break

                        except Exception as e:
                            self.log_signal.emit(f"Failed to download segment {sequence}: {e}")

                    if self.duration_reached:
                        break

                    if downloaded_segment:
                        last_new_segment_at = time.monotonic()
                    elif time.monotonic() - last_new_segment_at >= 8:
                        self.log_signal.emit("No new segments for 8 seconds; stream ended.")
                        break

                    time.sleep(2)

            if segment_count == 0:
                if os.path.exists(temp_file):
                    os.unlink(temp_file)
                self.error_signal.emit("No segments were downloaded")
                return

            self.log_signal.emit(f"Downloaded {segment_count} segments ({bytes_downloaded:,} bytes)")


            # video remuxing with ffmpeg is done in a separate step to ensure the final output is a playable MP4 file
            # instead of a raw container. This is important for compatibility with most media players.
            # also, remuxing ensures that the timestamps are properly generated and the file is seekable.
            if self.request.remux_on_finish:
                if not self._running:
                    self.log_signal.emit("Stop requested; finishing remux from the partial capture.")
                self.log_signal.emit("Remuxing with FFmpeg...")
                self.progress_signal.emit(DownloadProgress(
                    segment_count=segment_count,
                    bytes_downloaded=bytes_downloaded,
                    status="remuxing"
                ))

                cmd = [
                    self.request.ffmpeg_path,
                    "-hide_banner",
                    "-loglevel", "error",
                    "-y",
                    "-i", temp_file,
                    "-c", "copy",
                    "-fflags", "+genpts",
                    output_file
                ]

                self._ffmpeg_process = subprocess.Popen(
                    cmd,
                    **_hidden_console_kwargs(),
                )
                try:
                    self._ffmpeg_process.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    self._ffmpeg_process.kill()
                    self._ffmpeg_process.wait(timeout=5)
                    raise
                if self._ffmpeg_process.returncode != 0:
                    raise RuntimeError(
                        f"ffmpeg exited with code {self._ffmpeg_process.returncode}"
                    )
                if not os.path.isfile(output_file) or os.path.getsize(output_file) == 0:
                    if os.path.exists(output_file):
                        os.unlink(output_file)
                    raise RuntimeError("ffmpeg produced no usable output file")
                self.log_signal.emit(f"Remuxed to: {output_file}")

                # Clean up temp file
                if os.path.exists(temp_file):
                    os.unlink(temp_file)
                self._ffmpeg_process = None

            self.progress_signal.emit(DownloadProgress(
                segment_count=segment_count,
                bytes_downloaded=bytes_downloaded,
                status="finished"
            ))

            self.finished_signal.emit(output_file)

        except Exception as e:
            self.error_signal.emit(str(e))

            # Keep the raw capture on error instead of deleting it -- a failed
            # remux (bad ffmpeg path, crash, timeout, non-zero exit) shouldn't
            # also cost the viewer everything that was actually downloaded.
            if self._temp_file and os.path.exists(self._temp_file):
                # output_file is always assigned before self._temp_file is set
                # (see above), so it's guaranteed bound here.
                self.log_signal.emit(
                    f"Raw capture preserved at: {self._temp_file} "
                    "(remux failed -- remux it manually with ffmpeg, e.g. "
                    f'ffmpeg -i "{self._temp_file}" -c copy -fflags +genpts "{output_file}")'
                )

        finally:
            session.close()

# ─────────────────────────────────────────────
#  Core Functions
# ─────────────────────────────────────────────

def extract_preloaded_state(username: str, user_agent: Optional[str] = None) -> dict:
    """Fetch and return the full PRELOADED_STATE object from a model page."""
    _, page_headers = get_user_agent_headers(user_agent)
    url = f"https://stripchat.com/{username}"
    response = requests.get(url, headers=page_headers, timeout=8)
    response.raise_for_status()

    match = re.search(
        r'<script\b[^>]*>\s*window\.__PRELOADED_STATE__\s*=\s*({.*?})\s*</script>',
        response.text,
        re.DOTALL,
    )
    if not match:
        raise RuntimeError("PRELOADED_STATE not found in page")
    return json.loads(match.group(1))


def extract_stripchat_data(username: str, user_agent: Optional[str] = None) -> dict:
    """
    Extract model ID, stream status, and HLS master playlist from Stripchat.
    Returns a dict with modelId, isLive, status, and hlsPlaylist.
    """
    data = extract_preloaded_state(username, user_agent=user_agent)
    view_cam = data.get("viewCam", {})
    model = view_cam.get("model", {})

    model_id = model.get("id")
    if not model_id:
        raise RuntimeError("Model ID not found in PRELOADED_STATE")

    # Get the stream status from the model
    status = model.get("status", STREAM_STATUS_OFF)

    # Determine if the stream is actually live
    # "idle" means the model is online but not broadcasting, so treat it
    # the same as offline/away for our purposes.
    is_live = status not in [STREAM_STATUS_OFF, STREAM_STATUS_AWAY, STREAM_STATUS_IDLE]

    # Determine if the stream is accessible for downloading
    # Only "public" streams are accessible
    is_accessible = status == STREAM_STATUS_PUBLIC

    # Find HLS host
    hls_host = None
    if view_cam.get("hlsStreamHost"):
        hls_host = view_cam["hlsStreamHost"]
    elif view_cam.get("hlsConfig"):
        hls_host = view_cam["hlsConfig"].get("host")
    elif data.get("config", {}).get("data", {}).get("hlsStreamHost"):
        hls_host = data["config"]["data"]["hlsStreamHost"]
    elif data.get("configV3", {}).get("initialCommon", {}).get("hlsStreamHost"):
        hls_host = data["configV3"]["initialCommon"]["hlsStreamHost"]
    else:
        static = data.get("configV3", {}).get("static", {})
        features = static.get("featureSettings", {})
        fallback_domains = features.get("hlsFallback", {}).get("fallbackDomains", [])
        if fallback_domains:
            hls_host = fallback_domains[0]

    if not hls_host:
        raise RuntimeError("Could not find HLS host in page data")

    # Normalize host
    hls_host = hls_host.rstrip("/")
    for prefix in ("https://", "http://"):
        if hls_host.startswith(prefix):
            hls_host = hls_host[len(prefix):]

    if not hls_host.startswith("edge-hls."):
        hls_host = f"edge-hls.{hls_host}"

    hls_playlist = (
        f"https://{hls_host}/hls/{model_id}/master/{model_id}_auto.m3u8"
    )

    return {
        "modelId": model_id,
        "isLive": is_live,
        "isAccessible": is_accessible,
        "status": status,
        "hlsPlaylist": hls_playlist,
    }


def get_stream_info(stream_url: str, target_height: int = 1080, user_agent: Optional[str] = None) -> StreamInfo:
    """
    Get all stream information needed for downloading a Stripchat stream.
    Returns a StreamInfo dataclass with URL, resolution, keys, etc.
    """
    _, page_headers = get_user_agent_headers(user_agent)
    username = stream_url.rstrip("/").split("/")[-1]
    data = extract_stripchat_data(username, user_agent=user_agent)

    # Get status and accessibility
    status = data.get("status", STREAM_STATUS_OFF)
    is_accessible = data.get("isAccessible", False)

    # If not accessible, raise appropriate error
    if not is_accessible:
        if status == STREAM_STATUS_GROUP_SHOW:
            raise RuntimeError(f"{username} is in a group/ticket show - requires ticket purchase")
        elif status == STREAM_STATUS_PRIVATE:
            raise RuntimeError(f"{username} is in a private show - not accessible")
        elif status == STREAM_STATUS_P2P:
            raise RuntimeError(f"{username} is in a P2P show - requires payment")
        elif status == STREAM_STATUS_VIRTUAL_PRIVATE:
            raise RuntimeError(f"{username} is in a virtual private show - not accessible")
        elif status == STREAM_STATUS_AWAY:
            raise RuntimeError(f"{username} is currently away")
        elif status == STREAM_STATUS_OFF:
            raise RuntimeError(f"{username} is not currently live")
        else:
            raise RuntimeError(f"{username} has unknown status: {status}")

    if not data.get("isLive"):
        raise RuntimeError(f"{username} is not currently live")

    master_url = data.get("hlsPlaylist")
    if not master_url:
        raise RuntimeError("No HLS playlist found")

    # Fix known domain variation
    master_url = master_url.replace(
        "edge-hls.img.doppiocdn.org",
        "edge-hls.doppiocdn.org"
    )

    # Fetch master playlist
    response = requests.get(master_url, headers=page_headers, timeout=10)
    response.raise_for_status()
    master = response.text

    # Extract pkey
    advertised_pkeys = re.findall(
        r"#EXT-X-MOUFLON:PSCH:v2:([A-Za-z0-9]+)",
        master,
    )
    pkey = next((k for k in advertised_pkeys if k in KEYS), None)
    if not pkey:
        raise RuntimeError("No known Mouflon pkey advertised")
    pdkey = KEYS[pkey]

    # Find available variants
    variants = []
    lines = master.splitlines()

    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue

        res_match = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
        if not res_match:
            continue

        # Extract FPS if advertised
        fps_match = re.search(r"FRAME-RATE=([\d.]+)", line)
        fps = float(fps_match.group(1)) if fps_match else None

        width = int(res_match.group(1))
        height = int(res_match.group(2))

        if i + 1 >= len(lines):
            continue

        url = lines[i + 1].strip()
        if url:
            variants.append({
                "width": width,
                "height": height,
                "url": urljoin(master_url, url),
                "fps": fps
            })

    if not variants:
        raise RuntimeError("No HLS variants found")

    # Select best variant at or below target height
    suitable = [v for v in variants if v["height"] <= target_height]
    if suitable:
        selected = max(suitable, key=lambda v: v["height"])
    else:
        selected = min(variants, key=lambda v: v["height"])

    # Add low-latency parameters and pkey
    parsed = urlsplit(selected["url"])
    query = dict(parse_qsl(parsed.query))
    query["playlistType"] = "lowLatency"
    query["psch"] = "v2"
    query["pkey"] = pkey

    final_url = urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urlencode(query),
        parsed.fragment,
    ))

    return StreamInfo(
        url=stream_url,
        username=username,
        model_id=data["modelId"],
        is_live=data["isLive"],
        is_accessible=data["isAccessible"],
        status=data["status"],
        playlist_url=final_url,
        width=selected["width"],
        height=selected["height"],
        fps=selected.get("fps"),
        pdkey=pdkey,
    )


def decrypt_segment_url(url: str, pdkey: str) -> str:
    """
    Decrypt a Mouflon-obfuscated segment URL using the pdkey.
    """
    match = MOUFLON_SEGMENT_RE.search(url)
    if not match:
        raise ValueError(f"Could not parse Mouflon URL: {url}")

    encrypted = match.group(1)

    # Reverse and pad the base64 string
    reversed_str = encrypted[::-1]
    reversed_str += "=" * (-len(reversed_str) % 4)
    encrypted_bytes = base64.b64decode(reversed_str)

    # XOR with SHA256(pdkey)
    key = get_pdkey_digest(pdkey)
    decrypted = bytes(b ^ key[i % len(key)] for i, b in enumerate(encrypted_bytes))

    return url.replace(encrypted, decrypted.decode("utf-8", errors="replace"))


def download_segment_data(url: str, user_agent: Optional[str] = None, session: Optional[requests.Session] = None) -> bytes:
    """Download a segment and return its data as bytes."""
    headers, _ = get_user_agent_headers(user_agent)
    request_session = session or requests.Session()
    response = request_session.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    return response.content


def extract_frame_from_segment(segment_data: bytes, init_data: bytes, width: int, height: int) -> Optional[bytes]:
    """
    Use ffmpeg to extract a single frame from the combined init + segment data.
    Returns raw RGB24 image data or None on failure.
    Uses memory piping instead of temp files for better cross-platform compatibility.
    """
    if not FFMPEG_HAS_NATIVE_H264_DECODER:
        print("[Debug] ffmpeg build does not expose a native H.264 decoder; skipping Stripchat preview frame.")
        return None

    try:
        # Build the combined data in memory
        combined_data = init_data + segment_data

        # Use ffmpeg with stdin piping
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-i", "pipe:0",
            "-frames:v", "1",
            "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                   f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "pipe:1",
        ]

        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_hidden_console_kwargs(),
        )

        try:
            stdout, stderr = process.communicate(input=combined_data, timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            print("[Debug] ffmpeg timed out")
            return None

        if process.returncode == 0 and len(stdout) >= width * height * 3:
            return stdout[:width * height * 3]

        if stderr:
            print(f"[Debug] ffmpeg stderr: {stderr.decode('utf-8', errors='replace')[:200]}")

        return None

    except Exception as e:
        print(f"[Debug] Failed to extract frame: {e!r}")
        return None


def get_preview_frame(stream_url: str, target_height: int = 720, user_agent: Optional[str] = None) -> Optional[PreviewFrame]:
    """
    Get a decoded frame from a Stripchat stream.
    Returns a PreviewFrame object with raw RGB24 data, or None if the stream is not live or can't be fetched.
    """
    try:
        info = get_stream_info(stream_url, target_height, user_agent=user_agent)

        if not info.is_accessible:
            print(f"[Debug] Stream not accessible: {info.status}")
            return None

        if not info.is_live:
            return None

        headers, _ = get_user_agent_headers(user_agent)
        session = requests.Session()
        response = session.get(info.playlist_url, headers=headers, timeout=10)
        response.raise_for_status()
        playlist = response.text

        # Get the init segment
        init_match = INIT_SEGMENT_RE.search(playlist)
        if not init_match:
            return None

        init_url = urljoin(info.playlist_url, init_match.group(1))
        init_data = download_segment_data(init_url, user_agent=user_agent, session=session)

        # Find complete segments
        mouflon_urls = MOUFLON_URL_RE.findall(playlist)
        complete_urls = [
            u for u in mouflon_urls
            if not PART_SEGMENT_RE.search(u)
        ]

        if not complete_urls:
            return None

        # Use the latest complete segment
        encrypted_url = complete_urls[-1]
        decrypted_url = decrypt_segment_url(encrypted_url, info.pdkey)
        segment_data = download_segment_data(decrypted_url, user_agent=user_agent, session=session)

        # Extract the frame
        frame_data = extract_frame_from_segment(segment_data, init_data, info.width, info.height)
        if frame_data is None:
            return None

        preview_width = 320
        preview_height = 180

        with tempfile.NamedTemporaryFile(delete=False, suffix=".raw") as tmp:
            tmp.write(frame_data)
            tmp_path = tmp.name

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-s", f"{info.width}x{info.height}",
            "-i", tmp_path,
            "-frames:v", "1",
            "-vf", f"scale={preview_width}:{preview_height}:force_original_aspect_ratio=decrease,"
                f"pad={preview_width}:{preview_height}:(ow-iw)/2:(oh-ih)/2",
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "pipe:1",
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=10,
            **_hidden_console_kwargs(),
        )
        os.unlink(tmp_path)

        if result.returncode == 0 and len(result.stdout) >= preview_width * preview_height * 3:
            return PreviewFrame(
                image=result.stdout[:preview_width * preview_height * 3],
                width=preview_width,
                height=preview_height,
            )

        return None

    except Exception as e:
        print(f"[Debug] Failed to get preview frame: {e!r}")
        return None


class StripchatSiteAdapter:
    """Application-facing boundary for Stripchat-specific operations."""

    key = "stripchat.com"
    display_name = "Stripchat"

    @staticmethod
    def matches(url: str) -> bool:
        hostname = hostname_from_url(url)
        return hostname == "stripchat.com" or hostname.endswith(".stripchat.com")

    def check(self, url: str, user_agent: Optional[str] = None) -> SiteStatus:
        username = url.rstrip("/").split("/")[-1]
        data = extract_stripchat_data(username, user_agent=user_agent)
        status = data.get("status", STREAM_STATUS_OFF)

        # Empty / None status: the page was scraped mid-transition before
        # the server filled in the status field. This is a scrape race, not
        # a real state -- return "unknown" so the manager can skip this
        # poll cycle instead of misclassifying the stream or killing an
        # active download.
        if not status:
            print(f"[Debug] Empty Stripchat status for {username}")
            return SiteStatus("unknown", "❓ Unknown")

        messages = {
            STREAM_STATUS_PUBLIC: ("online", "🟢 LIVE"),
            STREAM_STATUS_GROUP_SHOW: ("private", "🎫 Ticket show"),
            STREAM_STATUS_PRIVATE: ("private", "🔒 Private show"),
            STREAM_STATUS_P2P: ("private", "💰 P2P show"),
            STREAM_STATUS_VIRTUAL_PRIVATE: ("private", "🔒 Virtual private show"),
            STREAM_STATUS_AWAY: ("away", "🌙 Away"),
            STREAM_STATUS_IDLE: ("offline", "💤 Idle"),
            STREAM_STATUS_OFF: ("offline", "💤 Offline"),
        }
        if status in messages:
            value, message = messages[status]
            return SiteStatus(value, message)

        # Unknown status string (new value we haven't seen yet). Log it and
        # treat as "unknown" rather than crashing -- the next poll will
        # hopefully return something we recognise.
        print(f"[Debug] Unknown Stripchat status: {status!r}")
        return SiteStatus("unknown", f"❓ Unknown ({status})")

    @staticmethod
    def preview(url: str, user_agent: Optional[str] = None) -> Optional[PreviewFrame]:
        return get_preview_frame(url, target_height=720, user_agent=user_agent)

    @classmethod
    def preview_data(cls, url: str, user_agent: Optional[str] = None) -> Optional[tuple[bytes, int, int]]:
        frame = cls.preview(url, user_agent=user_agent)
        return (frame.image, frame.width, frame.height) if frame else None

    @staticmethod
    def supports_download() -> bool:
        return True

    def download(
        self,
        stream_url: str,
        username: str,
        output_directory: str,
        target_height: int,
        user_agent: Optional[str],
        output_file_factory: Optional[Callable[[str, int], str]] = None,
        ffmpeg_path: str = "ffmpeg",
        on_log: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[int], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        on_resolution: Optional[Callable[[str, int], Optional[int]]] = None,
        segment_number: Optional[int] = None,
        split_by_filesize: bool = False,
        max_duration_seconds: int = 0,
        target_size_mb: int = 0,
    ) -> StripchatDownloadResult:
        """Run one native download and wait for its complete lifecycle."""
        timestamp = datetime.now().strftime(
            "%Y-%m-%d %H_%M_%S_%f" if segment_number is not None or split_by_filesize
            else "%Y-%m-%d %H_%M_%S"
        )
        output_stem = f"{username} {timestamp}"
        if segment_number is not None:
            output_stem += f" segment {segment_number:03d}"
        output_file = os.path.join(output_directory, f"{output_stem}.mp4")
        output_resolution, output_fps = "unknown", 0
        effective_max_duration_seconds = max_duration_seconds
        try:
            info = get_stream_info(
                stream_url,
                target_height=target_height,
                user_agent=user_agent or None,
            )
            output_resolution = f"{info.width}x{info.height}"
            output_fps = int(round(info.fps or 0))
            if info and on_resolution:
                probed_duration = on_resolution(
                    output_resolution, output_fps
                )
                if probed_duration and probed_duration > 0:
                    effective_max_duration_seconds = probed_duration
        except Exception as exc:
            if on_log:
                on_log(f"Resolution probe failed: {exc}")
        if output_file_factory:
            output_file = output_file_factory(output_resolution, output_fps)

        request = DownloadRequest(
            stream_url=stream_url,
            output_file=output_file,
            target_height=target_height,
            user_agent=user_agent or None,
            ffmpeg_path=ffmpeg_path,
            remux_on_finish=True,
            max_duration_seconds=effective_max_duration_seconds,
            target_size_mb=target_size_mb,
        )

        outcome = {"status": "running", "message": "", "output_file": ""}

        def handle_error(message: str):
            outcome["status"] = "error"
            outcome["message"] = message
            if on_error:
                on_error(message)

        def handle_progress(progress: DownloadProgress):
            if on_progress and progress.total_segments:
                percent = int((progress.segment_count / progress.total_segments) * 100)
                on_progress(max(0, min(100, percent)))

        def handle_finished(path: str):
            outcome["output_file"] = path

        downloader = StripchatDownloader(request)
        if on_log:
            downloader.log_signal.connect(
                on_log, Qt.ConnectionType.DirectConnection
            )
        if on_progress:
            downloader.progress_signal.connect(
                handle_progress, Qt.ConnectionType.DirectConnection
            )
        downloader.error_signal.connect(
            handle_error, Qt.ConnectionType.DirectConnection
        )
        downloader.finished_signal.connect(
            handle_finished, Qt.ConnectionType.DirectConnection
        )

        if on_log:
            on_log("Using Stripchat native downloader...")
        downloader.start()

        while downloader.isRunning():
            if should_stop and should_stop():
                downloader.stop()
            time.sleep(0.1)

        downloader.wait(15000)
        if outcome["status"] == "error":
            return StripchatDownloadResult(
                "error", outcome["message"], output_file=outcome["output_file"] or None
            )
        if should_stop and should_stop():
            return StripchatDownloadResult(
                "stopped", "Stop requested", output_file=outcome["output_file"] or None
            )
        if outcome["status"] == "running":
            outcome["status"] = "success"
        else:
            outcome["status"] = "error"
            outcome["message"] = "Native downloader ended without a terminal result"

        return StripchatDownloadResult(
            outcome["status"],
            outcome["message"],
            split_requested=downloader.duration_reached,
            output_file=outcome["output_file"] or None,
        )

# ──────────────────────────────────────────────────────────────────────────────
#  COMMAND-LINE TESTING
# ──────────────────────────────────────────────────────────────────────────────
#  This section is for development and debugging only.

# usage: python -m sites.stripchat https://stripchat.com/username
# from main directory. It will print stream info and optionally save debug data.

if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(
    description="Stripchat integration testing and debug tool"
    )

    parser.add_argument(
        "url",
        help="Stripchat stream URL (e.g., https://stripchat.com/username)"
    )
    parser.add_argument(
        "height",
        nargs="?",
        type=int,
        default=1080,
        help="Target height (default: 1080)"
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show verbose debug output"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Save the complete PRELOADED_STATE object to a JSON file for debugging"
    )

    args = parser.parse_args()

    # Optional: Set logging level
    if args.verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    try:
        username = args.url.rstrip("/").split("/")[-1]
        if args.debug:
            data = extract_preloaded_state(username)
            debug_file = f"debug_{username}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(debug_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"🐛 Complete PRELOADED_STATE saved to: {os.path.abspath(debug_file)}")

        # Default: Show stream info
        print(f"Getting stream info for: {args.url}")
        print("=" * 60)

        info = get_stream_info(args.url, target_height=args.height)

        # Print all fields in a nice format
        print(f"Username:       {info.username}")
        print(f"Model ID:       {info.model_id}")
        print(f"Status:         {info.status}")
        print(f"Live:           {'Yes' if info.is_live else 'No'}")
        print(f"Accessible:     {'Yes' if info.is_accessible else 'No'}")

        # Add explanation for inaccessible streams
        if not info.is_accessible:
            status_explanations = {
                "groupShow": "🎫 Ticket/Group show - requires purchase",
                "private": "🔒 Private show - not accessible",
                "p2p": "💰 P2P show - requires payment",
            }
            if info.status in status_explanations:
                print(f"Note:           {status_explanations[info.status]}")

        print(f"Resolution:     {info.width}x{info.height}")
        print(f"FPS:            {info.fps or 'Unknown'}")
        print(f"Playlist URL:   {info.playlist_url[:80]}...")
        print("=" * 60)

    except Exception as e:
        print(f"\n❌ Error: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)