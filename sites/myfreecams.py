"""Native MyFreeCams status, preview, and LL-HLS recording support."""

import os
import queue
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import quote, urljoin, urlsplit

import requests

from .base import (
    SiteCheckContext,
    SitePolicy,
    SiteStatus,
    debug_exception,
    debug_log,
)


API_BASE = "https://api-edge.myfreecams.com/usernameLookup/"
REFERER = "https://www.myfreecams.com/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:159.0) "
    "Gecko/20100101 Firefox/159.0"
)
_STREAM_FORMAT_RE = re.compile(
    r"Video:.*?(\d+)x(\d+)[^,]*,\s*([\d.]+)\s*fps",
    re.IGNORECASE,
)

POLICY = SitePolicy(
    key="myfreecams.com",
    display_name="MyFreeCams",
    hosts=("myfreecams.com",),
    referer=REFERER,
)

_VSTATE_STATUSES = {
    0: SiteStatus("online", "🟢 LIVE"),
    2: SiteStatus("offline", "💤 Not broadcasting"),
    12: SiteStatus("private", "🔒 Private show"),
    13: SiteStatus("private", "🔒 Group show"),
    14: SiteStatus("private", "🔒 Club show"),
    90: SiteStatus("away", "🌙 Away"),
}


@dataclass(frozen=True)
class MyFreeCamsDownloadResult:
    status: str
    message: str = ""
    split_requested: bool = False
    output_file: Optional[str] = None
    resolution: str = ""
    fps: int = 0


class MyFreeCamsAPIError(RuntimeError):
    """Raised when the MyFreeCams lookup response cannot identify a model."""


def username_from_url(url: str) -> str:
    """Extract a MyFreeCams username from its fragment-style room URL."""
    value = (url or "").strip()
    if "://" not in value and "/" not in value:
        username = value.lstrip("#")
    else:
        if "://" not in value:
            value = "https://" + value
        try:
            parsed = urlsplit(value)
        except ValueError:
            return ""
        username = parsed.fragment or parsed.path.rstrip("/").rsplit("/", 1)[-1]
    username = username.strip()
    return username if re.fullmatch(r"[A-Za-z0-9_]+", username) else ""


def status_from_vstate(vstate: object) -> SiteStatus:
    """Translate the API's VState code into the manager's neutral status."""
    try:
        code = int(vstate)
    except (TypeError, ValueError):
        return SiteStatus("unknown", f"❓ Invalid MyFreeCams VState: {vstate!r}")
    return _VSTATE_STATUSES.get(
        code, SiteStatus("unknown", f"❓ Unknown MyFreeCams VState: {code}")
    )


def _request_headers(user_agent: Optional[str]) -> dict[str, str]:
    return {
        "Referer": REFERER,
        "Origin": "https://www.myfreecams.com",
        "User-Agent": user_agent or DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _lookup_user(username: str, user_agent: Optional[str]) -> dict:
    response = requests.get(
        API_BASE + quote(username, safe=""),
        headers=_request_headers(user_agent),
        timeout=20,
    )
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as exc:
        raise MyFreeCamsAPIError("MyFreeCams API returned invalid JSON") from exc

    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict) or not result.get("success"):
        message = result.get("message") if isinstance(result, dict) else None
        detail = f": {message}" if message else ""
        raise MyFreeCamsAPIError(f"MyFreeCams lookup failed{detail}")
    user = result.get("user")
    if not isinstance(user, dict):
        raise MyFreeCamsAPIError("MyFreeCams API response has no user object")
    return user


def _model_session(user: dict) -> Optional[dict]:
    sessions = user.get("sessions")
    if not isinstance(sessions, list):
        raise MyFreeCamsAPIError("MyFreeCams API response has no session list")
    candidates = [
        session for session in sessions
        if isinstance(session, dict) and "vstate" in session
    ]
    return next(
        (session for session in candidates if session.get("server_name")),
        candidates[0] if candidates else None,
    )


def build_master_url(user_id: str, server_id: str, phase: str) -> str:
    """Construct the edge LL-HLS master playlist URL."""
    stream_name = f"mfc_{phase}1{user_id}.f4v_cmaf"
    return (
        "https://edgevideo.myfreecams.com/llhls/NxServer/"
        f"{server_id}/ngrp:{stream_name}/playlist_sfm4s.m3u8"
    )


def resolution_limit_dimensions(max_resolution: str) -> tuple[int, int]:
    """Convert a GUI resolution value such as ``1920x1080`` to width/height."""
    match = re.fullmatch(r"(\d+)x(\d+)", max_resolution.strip())
    if not match:
        raise ValueError(f"Invalid maximum resolution: {max_resolution!r}")
    width, height = (int(value) for value in match.groups())
    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid maximum resolution: {max_resolution!r}")
    return width, height


def pick_highest_quality(
    master_text: str,
    master_url: str,
    max_resolution: str = "3840x2160",
) -> str:
    """Pick the highest-bandwidth variant at the best resolution under the cap."""
    max_width, max_height = resolution_limit_dimensions(max_resolution)
    variants = []
    lines = master_text.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        resolution_match = re.search(r"(?:^|,)RESOLUTION=(\d+)x(\d+)(?:,|$)", line)
        bandwidth_match = re.search(r"BANDWIDTH=(\d+)", line)
        if not resolution_match:
            continue
        width, height = (int(value) for value in resolution_match.groups())
        bandwidth = int(bandwidth_match.group(1)) if bandwidth_match else 0
        uri = next(
            (
                following.strip()
                for following in lines[index + 1:]
                if following.strip() and not following.strip().startswith("#")
            ),
            "",
        )
        if uri and width <= max_width and height <= max_height:
            variants.append((height, width, bandwidth, uri))
    if not variants:
        raise MyFreeCamsAPIError(
            "MyFreeCams master playlist has no variants within "
            f"the {max_resolution} resolution limit"
        )
    uri = max(variants, key=lambda variant: variant[:3])[3]
    return urljoin(master_url, uri)


def check_status(
    url: str,
    context: SiteCheckContext,
    _: list[str],
) -> SiteStatus:
    """Look up a room and report its status using the API's VState value."""
    username = username_from_url(url)
    if not username:
        debug_log("MyFreeCams", f"invalid model URL received: {url}")
        return SiteStatus("error", "❌ Invalid MyFreeCams model URL")
    try:
        debug_log("MyFreeCams", f"looking up status for {username}")
        user = _lookup_user(username, context.user_agent)
        model_session = _model_session(user)
    except requests.RequestException as exc:
        debug_exception("MyFreeCams", f"status request failed for {username}", exc)
        return SiteStatus("error", f"❌ MyFreeCams request failed: {exc}")
    except MyFreeCamsAPIError as exc:
        debug_exception("MyFreeCams", f"status response invalid for {username}", exc)
        return SiteStatus("error", f"❌ {exc}")
    if model_session is None:
        # No active session means the model isn't broadcasting.
        # This is a normal offline state, not an API failure.
        debug_log("MyFreeCams", f"no active session for {username} (offline)")
        return SiteStatus("offline", "💤 Offline")
    debug_log(
        "MyFreeCams",
        f"status for {username}: VState={model_session['vstate']}",
    )
    return status_from_vstate(model_session["vstate"])


class MyFreeCamsSiteAdapter:
    """Application-facing MyFreeCams API and LL-HLS operations."""

    @staticmethod
    def matches(url: str) -> bool:
        value = (url or "").strip()
        if "://" not in value:
            value = "https://" + value
        try:
            hostname = (urlsplit(value).hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        return hostname == "myfreecams.com" or hostname.endswith(".myfreecams.com")

    def check(self, url: str, user_agent: Optional[str] = None) -> SiteStatus:
        context = SiteCheckContext(
            ytdlp_path="",
            browser="",
            cookie_args=[],
            user_agent=user_agent or "",
            subprocess_kwargs={},
            format_error=lambda stderr, stdout: stderr or stdout,
        )
        return check_status(url, context, [])

    @staticmethod
    def _live_playlist(
        url: str,
        user_agent: Optional[str],
        max_resolution: str = "3840x2160",
    ) -> str:
        username = username_from_url(url)
        if not username:
            raise ValueError("A MyFreeCams model username is required")
        user = _lookup_user(username, user_agent)
        model_session = _model_session(user)
        if model_session is None:
            raise RuntimeError("MyFreeCams returned no VState")
        status = status_from_vstate(model_session["vstate"])
        if status.value != "online":
            raise RuntimeError(status.message or "MyFreeCams model is not public")

        user_id = str(user.get("id", ""))
        server_name = str(model_session.get("server_name", ""))
        phase = model_session.get("phase")
        user_id_valid = user_id.isdigit()
        server_match = re.fullmatch(r"video(\d+)", server_name, re.IGNORECASE)
        phase_valid = isinstance(phase, str)
        if not user_id_valid or not server_match or not phase_valid:
            debug_log(
                "MyFreeCams",
                "live session validation failed: "
                f"user_id={user_id!r} (valid={user_id_valid}), "
                f"server_name={server_name!r} (valid={server_match is not None}), "
                f"phase={phase!r} (valid={phase_valid})",
            )
            invalid_fields = [
                name
                for name, valid in (
                    ("user_id", user_id_valid),
                    ("server_name", server_match is not None),
                    ("phase", phase_valid),
                )
                if not valid
            ]
            raise MyFreeCamsAPIError(
                "MyFreeCams returned invalid live session data "
                f"({', '.join(invalid_fields)})"
            )

        phase = phase or ""
        master_url = build_master_url(user_id, server_match.group(1), phase)
        response = requests.get(
            master_url,
            headers=_request_headers(user_agent),
            timeout=20,
        )
        response.raise_for_status()
        if not response.text.lstrip().startswith("#EXTM3U"):
            raise MyFreeCamsAPIError("MyFreeCams response is not an HLS playlist")
        return pick_highest_quality(response.text, master_url, max_resolution)

    @classmethod
    def preview_data(
        cls,
        url: str,
        user_agent: Optional[str] = None,
        ffmpeg_path: str = "ffmpeg",
        subprocess_kwargs: Optional[dict] = None,
        max_resolution: str = "3840x2160",
    ) -> Optional[tuple[bytes, int, int]]:
        playlist_url = cls._live_playlist(url, user_agent, max_resolution)
        effective_user_agent = user_agent or DEFAULT_USER_AGENT
        ffmpeg_headers = (
            f"User-Agent: {effective_user_agent}\r\n"
            f"Referer: {REFERER}\r\n"
            "Origin: https://www.myfreecams.com\r\n"
        )
        width, height = 320, 180
        result = subprocess.run(
            [
                ffmpeg_path,
                "-hide_banner",
                "-loglevel", "error",
                "-user_agent", effective_user_agent,
                "-headers", ffmpeg_headers,
                "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                "-extension_picky", "0",
                "-allowed_extensions", "ALL",
                "-i", playlist_url,
                "-frames:v", "1",
                "-vf", (
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
                ),
                "-f", "rawvideo",
                "-pix_fmt", "rgb24",
                "pipe:1",
            ],
            capture_output=True,
            timeout=15,
            **(subprocess_kwargs or {}),
        )
        expected = width * height * 3
        if result.returncode == 0 and len(result.stdout) >= expected:
            return result.stdout[:expected], width, height

        stderr = (
            result.stderr.decode("utf-8", errors="replace")
            if isinstance(result.stderr, bytes)
            else (result.stderr or "")
        ).strip()

        debug_log(
            "MyFreeCams",
            f"FFmpeg preview failed: returncode={result.returncode}, "
            f"output_bytes={len(result.stdout)}, expected_bytes={expected}",
        )

        if stderr:
            debug_log("MyFreeCams", f"FFmpeg preview stderr: {stderr[:2000]}")

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
        max_resolution: str = "3840x2160",
    ) -> MyFreeCamsDownloadResult:
        try:
            debug_log(
                "MyFreeCams",
                f"requesting live playlist for {username_from_url(stream_url)}",
            )
            playlist_url = self._live_playlist(
                stream_url, user_agent, max_resolution
            )
        except (requests.RequestException, MyFreeCamsAPIError, RuntimeError, ValueError) as exc:
            debug_exception("MyFreeCams", "could not prepare LL-HLS playlist", exc)
            return MyFreeCamsDownloadResult("error", str(exc))

        output_directory = os.path.dirname(os.path.abspath(output_file))
        os.makedirs(output_directory, exist_ok=True)
        capture_file = f"{output_file}.temp.ts"
        effective_user_agent = user_agent or DEFAULT_USER_AGENT
        ffmpeg_headers = (
            f"User-Agent: {effective_user_agent}\r\n"
            f"Referer: {REFERER}\r\n"
            "Origin: https://www.myfreecams.com\r\n"
        )
        command = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel", "info",
            "-nostats",
            "-user_agent", effective_user_agent,
            "-headers", ffmpeg_headers,
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-allowed_extensions", "ALL",
            "-extension_picky", "0",
            "-i", playlist_url,
        ]
        if max_duration_seconds > 0:
            command.extend(["-t", str(max_duration_seconds)])
        command.extend(["-c", "copy", "-y", capture_file])
        if on_log:
            on_log("Recording MyFreeCams LL-HLS stream with FFmpeg...")

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                **(subprocess_kwargs or {}),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            debug_exception("MyFreeCams", "FFmpeg capture could not start", exc)
            return MyFreeCamsDownloadResult("error", f"FFmpeg failed to start: {exc}")
        if process.stderr is None:
            process.terminate()
            process.wait()
            return MyFreeCamsDownloadResult("error", "FFmpeg stderr pipe was not created")

        started_at = time.monotonic()
        stopped = False
        log_lines: deque[str] = deque(maxlen=100)
        log_queue: queue.Queue[Optional[str]] = queue.Queue()

        def drain_stderr() -> None:
            try:
                for line in process.stderr:
                    log_queue.put(line.rstrip())
            finally:
                log_queue.put(None)

        reader = threading.Thread(target=drain_stderr, daemon=True)
        reader.start()
        reader_finished = False
        detected_format: Optional[tuple[str, int]] = None
        duration_limit_reached = False
        while process.poll() is None or not reader_finished:
            elapsed = time.monotonic() - started_at
            duration_reached = (
                max_duration_seconds > 0 and elapsed >= max_duration_seconds
            )
            stop_requested = bool(should_stop and should_stop())
            if process.poll() is None and (stop_requested or duration_reached):
                stopped = stop_requested
                duration_limit_reached = duration_reached and not stop_requested
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
            elif line:
                match = _STREAM_FORMAT_RE.search(line)

                if match:
                    stream_format = (
                        f"{match.group(1)}x{match.group(2)}",
                        round(float(match.group(3))),
                    )
                    if stream_format != detected_format:
                        detected_format = stream_format
                        if on_resolution:
                            on_resolution(*stream_format)

                # Keep useful diagnostics, but discard routine FFmpeg output.
                lower = line.lower()

                diagnostic_terms = (
                    "warning",
                    "error",
                    "failed",
                    "failure",
                    "assertion",
                    "retrying",
                    "skipping",
                    "timed out",
                    "timeout",
                    "404",
                    "403",
                    "503",
                    "end of file",
                    "connection reset",
                    "server returned",
                    "corrupt",
                    "invalid",
                    "not found",
                    "could not",
                    "unable",
                )

                is_diagnostic = any(
                    term in lower for term in diagnostic_terms
                )

                # Preserve the stream information used for resolution detection.
                if is_diagnostic or match:
                    # Avoid filling the log with repeated identical messages.
                    if line not in log_lines:
                        log_lines.append(line)

        reader.join()
        capture_elapsed = time.monotonic() - started_at
        stderr = "\n".join(log_lines).strip()
        try:
            capture_size = os.path.getsize(capture_file)
        except OSError:
            capture_size = 0
        result_format = detected_format or ("", 0)
        debug_log(
            "MyFreeCams",
            f"capture ended: ffmpeg_exit={process.returncode}, "
            f"elapsed={capture_elapsed:.1f}s, bytes={capture_size}, "
            f"stopped={stopped}, duration_limit={duration_limit_reached}",
        )
        if capture_size <= 0:
            if stopped:
                return MyFreeCamsDownloadResult(
                    "stopped",
                    "Stopped before FFmpeg produced a recording",
                    resolution=result_format[0],
                    fps=result_format[1],
                )
            return MyFreeCamsDownloadResult(
                "error",
                stderr or f"FFmpeg exited with code {process.returncode} without a recording",
                output_file=capture_file,
                resolution=result_format[0],
                fps=result_format[1],
            )
        capture_warning = ""
        if process.returncode != 0 and not (stopped or duration_limit_reached):
            capture_warning = (
                f"FFmpeg exited with code {process.returncode}; "
                "attempting to remux the partial capture"
            )
            if on_log:
                on_log(f"⚠ MyFreeCams {capture_warning}")
                if stderr:
                    on_log(f"MyFreeCams FFmpeg diagnostics:\n{stderr}")

        if on_log:
            on_log("Remuxing MyFreeCams capture with FFmpeg...")
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
                timeout=180,
                **(subprocess_kwargs or {}),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            debug_exception("MyFreeCams", "FFmpeg remux failed to run", exc)
            return MyFreeCamsDownloadResult(
                "error",
                f"FFmpeg remux failed: {exc}. Raw capture preserved at {capture_file}",
                output_file=capture_file,
                resolution=result_format[0],
                fps=result_format[1],
            )
        if remux_result.returncode != 0:
            debug_log(
                "MyFreeCams",
                f"primary remux failed with exit={remux_result.returncode}; "
                f"stderr={(remux_result.stderr or '').strip()[:1200]!r}",
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
                    timeout=120,
                    **(subprocess_kwargs or {}),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return MyFreeCamsDownloadResult(
                    "error",
                    f"FFmpeg audio fallback failed: {exc}. Raw capture preserved at {capture_file}",
                    output_file=capture_file,
                    resolution=result_format[0],
                    fps=result_format[1],
                )
            if fallback_result.returncode != 0:
                detail = (fallback_result.stderr or fallback_result.stdout or "").strip()
                return MyFreeCamsDownloadResult(
                    "error",
                    (
                        "FFmpeg could not remux the capture"
                        + (f": {detail}" if detail else "")
                        + f". Raw capture preserved at {capture_file}"
                    ),
                    output_file=capture_file,
                    resolution=result_format[0],
                    fps=result_format[1],
                )
            if on_log:
                on_log("Remux succeeded with audio re-encoded.")

        try:
            if os.path.getsize(output_file) <= 0:
                return MyFreeCamsDownloadResult(
                    "error",
                    f"FFmpeg remux produced no usable MP4. Raw capture preserved at {capture_file}",
                    output_file=capture_file,
                    resolution=result_format[0],
                    fps=result_format[1],
                )
            os.remove(capture_file)
        except OSError as exc:
            return MyFreeCamsDownloadResult(
                "error",
                f"Could not finalize the MP4 recording: {exc}. Raw capture preserved at {capture_file}",
                output_file=capture_file,
                resolution=result_format[0],
                fps=result_format[1],
            )

        split_requested = (
            duration_limit_reached
            or (
                max_duration_seconds > 0
                and capture_elapsed >= max_duration_seconds - 2
            )
        )
        if on_log:
            on_log(f"Remuxed to: {output_file}")
        return MyFreeCamsDownloadResult(
            "stopped" if stopped else "success",
            (
                f"Partial capture remuxed successfully after FFmpeg exit "
                f"code {process.returncode}"
                if capture_warning
                else "Recording completed"
            ),
            split_requested=split_requested,
            output_file=output_file,
            resolution=result_format[0],
            fps=result_format[1],
        )
