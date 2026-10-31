"""Native XloveCam status, preview, and HLS recording support."""

import json
import os
import re
import queue
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import requests

from .base import SiteCheckContext, SitePolicy, SiteStatus


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:159.0) "
    "Gecko/20100101 Firefox/159.0"
)
REFERER = "https://www.xlovecam.com/"
PAGE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "identity",
    "Referer": REFERER,
}
PLAYLIST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    "Referer": REFERER,
    "Origin": "https://www.xlovecam.com",
}
_SCRIPT_START_RE = re.compile(
    r"<script[^>]*>\s*(?:(?:window\.__PRELOADED_STATE__|window\.__INITIAL_STATE__|"
    r"window\.__NUXT__|window\.__NEXT_DATA__)\s*=\s*)?(?=\{)",
    re.IGNORECASE,
)
_BANDWIDTH_RE = re.compile(r"(?:^|,)BANDWIDTH=(\d+)(?:,|$)")
_STREAM_FORMAT_RE = re.compile(
    r"Video:.*?(\d+)x(\d+)[^,]*,\s*([\d.]+)\s*fps",
    re.IGNORECASE,
)

POLICY = SitePolicy(
    key="xlovecam.com",
    display_name="XloveCam",
    hosts=("xlovecam.com",),
    referer=REFERER,
)


@dataclass(frozen=True)
class XloveCamDownloadResult:
    status: str
    message: str = ""
    split_requested: bool = False
    output_file: Optional[str] = None
    resolution: str = ""
    fps: int = 0


def username_from_url(url: str) -> str:
    value = (url or "").strip()
    if "://" not in value and "/" not in value:
        return value
    if "://" not in value:
        value = "https://" + value
    try:
        parts = [part for part in urlsplit(value).path.split("/") if part]
    except ValueError:
        return ""
    for index, part in enumerate(parts[:-1]):
        if part.lower() == "model":
            return parts[index + 1]
    return parts[-1] if parts else ""


def profile_url(username_or_url: str) -> str:
    username = username_from_url(username_or_url)
    if not username:
        raise ValueError("An XloveCam model username is required")
    return f"https://www.xlovecam.com/en/model/{quote(username, safe='')}/"


def _find_json_objects(html: str):
    """Yield balanced JSON objects from script tags, respecting quoted braces."""
    for match in _SCRIPT_START_RE.finditer(html):
        start = match.end()
        depth = 0
        in_string = False
        string_char = ""
        escape = False
        for index in range(start, len(html)):
            char = html[index]
            if escape:
                escape = False
                continue
            if in_string:
                if char == "\\":
                    escape = True
                elif char == string_char:
                    in_string = False
                continue
            if char in ("'", '"'):
                in_string = True
                string_char = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    yield html[start:index + 1]
                    break


def extract_state(html: str) -> dict:
    """Find the embedded state object containing performerInfo."""
    candidates = []
    for blob in _find_json_objects(html):
        try:
            obj = json.loads(blob)
        except (json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(obj, dict):
            continue
        chat = obj.get("chat")
        if isinstance(chat, dict) and isinstance(chat.get("performerInfo"), dict):
            candidates.append((len(obj), len(blob), obj))
    if not candidates:
        raise ValueError("Could not find XloveCam performer state in page")
    return max(candidates, key=lambda candidate: (candidate[0], candidate[1]))[2]


def _performer_info(state: dict) -> dict:
    chat = state.get("chat")
    if not isinstance(chat, dict):
        return {}
    performer_info = chat.get("performerInfo")
    return performer_info if isinstance(performer_info, dict) else {}


def _performer(state: dict) -> dict:
    performer = _performer_info(state).get("performer")
    return performer if isinstance(performer, dict) else {}


def _live_info(state: dict) -> dict:
    live_info = _performer(state).get("liveStreamInfo")
    return live_info if isinstance(live_info, dict) else {}


def resolve_playlist_url(state: dict) -> str:
    """Prefer the public HLS playlist and fall back to the safe playlist."""
    live_info = _live_info(state)
    return live_info.get("hlsPlaylist") or live_info.get("hlsPlaylistSafe") or ""


def resolve_highest_bandwidth_variant(
    playlist_url: str,
    user_agent: Optional[str] = None,
) -> str:
    """Return the highest-bandwidth HLS variant, or the input URL if none exist."""
    parsed_url = urlsplit(playlist_url)
    if parsed_url.path.endswith("/safe.m3u8"):
        master_path = parsed_url.path[:-len("safe.m3u8")] + "playlist.m3u8"
        playlist_url = urlunsplit(
            (parsed_url.scheme, parsed_url.netloc, master_path, parsed_url.query, parsed_url.fragment)
        )

    headers = dict(PLAYLIST_HEADERS)
    if user_agent:
        headers["User-Agent"] = user_agent
    response = requests.get(playlist_url, headers=headers, timeout=15)
    response.raise_for_status()

    lines = [line.strip() for line in response.text.splitlines()]
    variants = []
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        bandwidth_match = _BANDWIDTH_RE.search(line.partition(":")[2])
        if not bandwidth_match:
            continue
        variant_uri = next(
            (candidate for candidate in lines[index + 1:] if candidate and not candidate.startswith("#")),
            None,
        )
        if variant_uri:
            variants.append(
                (int(bandwidth_match.group(1)), urljoin(response.url, variant_uri))
            )
    return max(variants, key=lambda variant: variant[0])[1] if variants else playlist_url


def _fetch_page(username: str, user_agent: Optional[str] = None) -> str:
    headers = dict(PAGE_HEADERS)
    if user_agent:
        headers["User-Agent"] = user_agent
    response = requests.get(profile_url(username), headers=headers, timeout=15)
    response.raise_for_status()
    return response.text


def _status_for_page(html: str, username: str) -> tuple[SiteStatus, dict]:
    if "js-mode-show-offline" in html:
        return SiteStatus("offline", "💤 Offline"), {}
    try:
        state = extract_state(html)
    except ValueError:
        return SiteStatus("unknown", "❓ XloveCam status unavailable"), {}

    performer = _performer(state)
    live_info = _live_info(state)
    show_type = live_info.get("showType")
    if show_type == 1:
        if live_info.get("hlsPlaylist"):
            return SiteStatus("online", "🟢 LIVE"), state
        return SiteStatus("unknown", "❓ Public stream playlist is not ready"), state
    if show_type == 2:
        return SiteStatus("private", "🔒 Private show"), state
    if show_type == 0 or performer.get("online") == 0:
        return SiteStatus("offline", "💤 Offline"), state
    if live_info.get("hlsPlaylistSafe") and not live_info.get("hlsPlaylist"):
        return SiteStatus("unknown", "❓ Safe playlist only; public stream unavailable"), state
    return SiteStatus("unknown", f"❓ Unknown XloveCam status for {username}"), state


def check_status(url: str, context: SiteCheckContext, _check_args: list[str]) -> SiteStatus:
    username = username_from_url(url)
    if not username:
        return SiteStatus("error", "❌ Invalid XloveCam model URL")
    try:
        return _status_for_page(_fetch_page(username, context.user_agent), username)[0]
    except requests.RequestException as exc:
        return SiteStatus("error", f"❌ XloveCam request failed: {exc}")


class XloveCamSiteAdapter:
    """Application-facing XloveCam operations using its embedded page state."""

    @staticmethod
    def matches(url: str) -> bool:
        value = (url or "").strip()
        if "://" not in value:
            value = "https://" + value
        try:
            hostname = (urlsplit(value).hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        return hostname == "xlovecam.com" or hostname.endswith(".xlovecam.com")

    def check(self, url: str, user_agent: Optional[str] = None) -> SiteStatus:
        username = username_from_url(url)
        if not username:
            return SiteStatus("error", "❌ Invalid XloveCam model URL")
        try:
            page = _fetch_page(username, user_agent)
        except requests.RequestException as exc:
            return SiteStatus("error", f"❌ XloveCam request failed: {exc}")
        return _status_for_page(page, username)[0]

    @staticmethod
    def _live_playlist(url: str, user_agent: Optional[str]) -> str:
        username = username_from_url(url)
        if not username:
            raise ValueError("An XloveCam model username is required")
        page = _fetch_page(username, user_agent)
        status, state = _status_for_page(page, username)
        if status.value != "online":
            raise RuntimeError(status.message or "XloveCam stream is not publicly live")
        playlist_url = resolve_playlist_url(state)
        if not playlist_url:
            raise RuntimeError("No XloveCam HLS playlist is available")
        return resolve_highest_bandwidth_variant(playlist_url, user_agent)

    @classmethod
    def preview_data(
        cls,
        url: str,
        user_agent: Optional[str] = None,
        ffmpeg_path: str = "ffmpeg",
        subprocess_kwargs: Optional[dict] = None,
    ) -> Optional[tuple[bytes, int, int]]:
        playlist_url = cls._live_playlist(url, user_agent)
        width, height = 320, 180
        effective_user_agent = user_agent or USER_AGENT
        ffmpeg_headers = (
            f"User-Agent: {effective_user_agent}\r\n"
            f"Referer: {REFERER}\r\n"
            "Origin: https://www.xlovecam.com\r\n"
        )
        command = [
            ffmpeg_path, "-hide_banner", "-loglevel", "error",
            "-user_agent", effective_user_agent,
            "-headers", ffmpeg_headers,
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-i", playlist_url,
            "-frames:v", "1",
            "-vf", (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
            ),
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=15,
            **(subprocess_kwargs or {}),
        )
        expected = width * height * 3
        if result.returncode == 0 and len(result.stdout) >= expected:
            return result.stdout[:expected], width, height
        return None

    def download(
        self,
        stream_url: str,
        output_file: str,
        user_agent: Optional[str] = None,
        ffmpeg_path: str = "ffmpeg",
        on_log: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        max_duration_seconds: int = 0,
        subprocess_kwargs: Optional[dict] = None,
        on_resolution: Optional[Callable[[str, int], None]] = None,
    ) -> XloveCamDownloadResult:
        playlist_url = self._live_playlist(stream_url, user_agent)
        output_directory = os.path.dirname(os.path.abspath(output_file))
        os.makedirs(output_directory, exist_ok=True)
        capture_file = f"{output_file}.temp.ts"
        effective_user_agent = user_agent or USER_AGENT
        ffmpeg_headers = (
            f"User-Agent: {effective_user_agent}\r\n"
            f"Referer: {REFERER}\r\n"
            "Origin: https://www.xlovecam.com\r\n"
        )
        command = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel", "info",
            "-stats",
            "-user_agent", effective_user_agent,
            "-headers", ffmpeg_headers,
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-i", playlist_url,
        ]
        if max_duration_seconds > 0:
            command.extend(["-t", str(max_duration_seconds)])
        command.extend(["-c", "copy", "-y", capture_file])
        if on_log:
            on_log("Recording XloveCam HLS stream with FFmpeg...")

        stopped = False
        started_at = time.monotonic()
        split_requested = False
        log_lines: deque[str] = deque(maxlen=1000)
        detected_format: Optional[tuple[str, int]] = None
        log_queue: queue.Queue[Optional[str]] = queue.Queue()
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            **(subprocess_kwargs or {}),
        )
        if process.stderr is None:
            raise RuntimeError("FFmpeg stderr pipe was not created")

        def drain_stderr() -> None:
            try:
                for line in process.stderr:
                    log_queue.put(line.rstrip())
            finally:
                log_queue.put(None)

        reader = threading.Thread(target=drain_stderr, daemon=True)
        reader.start()
        reader_finished = False
        while process.poll() is None or not reader_finished:
            if (
                process.poll() is None
                and should_stop
                and should_stop()
            ):
                stopped = True
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                if process.poll() is None:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            try:
                line = log_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                reader_finished = True
                continue
            if line:
                log_lines.append(line)
                match = _STREAM_FORMAT_RE.search(line)
                if match:
                    resolution = f"{match.group(1)}x{match.group(2)}"
                    fps = round(float(match.group(3)))
                    stream_format = (resolution, fps)
                    if stream_format != detected_format:
                        detected_format = stream_format
                        if on_resolution:
                            on_resolution(*stream_format)

        reader.join()
        stderr = "\n".join(log_lines).strip()

        try:
            capture_size = os.path.getsize(capture_file)
        except OSError:
            capture_size = 0
        if capture_size <= 0:
            if stopped:
                return XloveCamDownloadResult(
                    "stopped",
                    "Stopped before FFmpeg produced a recording",
                    output_file=None,
                    resolution=detected_format[0] if detected_format else "",
                    fps=detected_format[1] if detected_format else 0,
                )
            if process.returncode != 0:
                return XloveCamDownloadResult(
                    "error",
                    stderr or f"FFmpeg exited with code {process.returncode}",
                    output_file=capture_file,
                    resolution=detected_format[0] if detected_format else "",
                    fps=detected_format[1] if detected_format else 0,
                )
            return XloveCamDownloadResult(
                "error",
                "FFmpeg completed without producing a recording",
                output_file=capture_file,
                resolution=detected_format[0] if detected_format else "",
                fps=detected_format[1] if detected_format else 0,
            )

        if process.returncode != 0 and not stopped:
            return XloveCamDownloadResult(
                "error",
                stderr or f"FFmpeg exited with code {process.returncode}",
                output_file=capture_file,
                resolution=detected_format[0] if detected_format else "",
                fps=detected_format[1] if detected_format else 0,
            )

        split_requested = (
            max_duration_seconds > 0
            and time.monotonic() - started_at >= max_duration_seconds - 2
        )
        if on_log:
            on_log("Remuxing with FFmpeg...")
        remux_command = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-fflags", "+genpts",
            "-i", capture_file,
            "-c", "copy",
            "-movflags", "+faststart",
            output_file,
        ]
        try:
            remux_result = subprocess.run(
                remux_command,
                capture_output=True,
                text=True,
                **(subprocess_kwargs or {}),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return XloveCamDownloadResult(
                "error",
                f"FFmpeg remux failed: {exc}. Raw capture preserved at {capture_file}",
                output_file=capture_file,
                resolution=detected_format[0] if detected_format else "",
                fps=detected_format[1] if detected_format else 0,
            )
        if remux_result.returncode != 0:
            remux_error = (remux_result.stderr or remux_result.stdout or "").strip()
            if on_log:
                on_log(
                    "AAC packet-copy remux failed; retrying with audio re-encoding."
                )
            fallback_command = [
                ffmpeg_path,
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-fflags", "+genpts",
                "-i", capture_file,
                "-map", "0:v?",
                "-map", "0:a?",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "96k",
                "-movflags", "+faststart",
                output_file,
            ]
            try:
                fallback_result = subprocess.run(
                    fallback_command,
                    capture_output=True,
                    text=True,
                    **(subprocess_kwargs or {}),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return XloveCamDownloadResult(
                    "error",
                    (
                        f"FFmpeg packet-copy remux failed"
                        + (f": {remux_error}" if remux_error else "")
                        + f"; AAC re-encode retry failed: {exc}. "
                        f"Raw capture preserved at {capture_file}"
                    ),
                    output_file=capture_file,
                    resolution=detected_format[0] if detected_format else "",
                    fps=detected_format[1] if detected_format else 0,
                )
            if fallback_result.returncode != 0:
                fallback_error = (
                    fallback_result.stderr or fallback_result.stdout or ""
                ).strip()
                return XloveCamDownloadResult(
                    "error",
                    (
                        f"FFmpeg packet-copy remux failed"
                        + (f": {remux_error}" if remux_error else "")
                        + f"; AAC re-encode retry exited with code "
                        f"{fallback_result.returncode}"
                        + (f": {fallback_error}" if fallback_error else "")
                        + f". Raw capture preserved at {capture_file}"
                    ),
                    output_file=capture_file,
                    resolution=detected_format[0] if detected_format else "",
                    fps=detected_format[1] if detected_format else 0,
                )
            if on_log:
                on_log("Remux succeeded with AAC audio re-encoded.")
        try:
            if os.path.getsize(output_file) <= 0:
                return XloveCamDownloadResult(
                    "error",
                    f"FFmpeg remux produced no usable MP4. Raw capture preserved at {capture_file}",
                    output_file=capture_file,
                    resolution=detected_format[0] if detected_format else "",
                    fps=detected_format[1] if detected_format else 0,
                )
            os.remove(capture_file)
        except OSError as exc:
            return XloveCamDownloadResult(
                "error",
                f"Could not finalize the MP4 recording: {exc}. Raw capture preserved at {capture_file}",
                output_file=capture_file,
                resolution=detected_format[0] if detected_format else "",
                fps=detected_format[1] if detected_format else 0,
            )
        if on_log:
            on_log(f"Remuxed to: {output_file}")

        if stopped:
            return XloveCamDownloadResult(
                "stopped",
                "Stop requested",
                output_file=output_file,
                resolution=detected_format[0] if detected_format else "",
                fps=detected_format[1] if detected_format else 0,
            )
        return XloveCamDownloadResult(
            "success",
            split_requested=split_requested,
            output_file=output_file,
            resolution=detected_format[0] if detected_format else "",
            fps=detected_format[1] if detected_format else 0,
        )
