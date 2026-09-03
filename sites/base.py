"""Shared site adapter types.

This module intentionally has no Qt or application imports so site discovery can
be used by tests and by the manager's non-GUI helpers.
"""

from dataclasses import dataclass, field
import subprocess
from typing import Callable, Optional
from urllib.parse import urlsplit


@dataclass(frozen=True)
class SitePolicy:
    """Site-specific policy used by the generic yt-dlp path."""

    key: str
    display_name: str
    hosts: tuple[str, ...]
    referer: str = ""
    impersonate: bool | str = False
    check_args: tuple[str, ...] = ()
    download_args: tuple[str, ...] = ()
    ffmpeg_args: tuple[str, ...] = ()

    def matches_host(self, hostname: str) -> bool:
        hostname = (hostname or "").lower().rstrip(".")
        return any(hostname == host or hostname.endswith("." + host) for host in self.hosts)


@dataclass(frozen=True)
class SiteStatus:
    """Site-neutral status result; the manager maps this to its UI enum."""

    value: str
    message: str = ""


@dataclass
class SiteCheckContext:
    """Application services required by a site's yt-dlp status check."""

    ytdlp_path: str
    browser: str
    cookie_args: list[str]
    user_agent: str
    subprocess_kwargs: dict
    format_error: Callable[[str, str], str]


def extract_username(url: str) -> str:
    """Extract the first path segment used as a model username."""
    value = (url or "").strip()
    if "://" not in value:
        value = "https://" + value
    try:
        path_parts = [part for part in urlsplit(value).path.split("/") if part]
    except ValueError:
        path_parts = []
    return path_parts[0] if path_parts else (url or "")


def run_ytdlp_status_check(
    url: str,
    context: SiteCheckContext,
    check_args: list[str],
    error_handler: Optional[Callable[[str, str], Optional[SiteStatus]]] = None,
) -> SiteStatus:
    """Run the common yt-dlp live-status probe and apply site error rules."""
    try:
        command = [context.ytdlp_path, "--simulate", "--print", "%(live_status)s"]
        if context.user_agent:
            command.extend(["--user-agent", context.user_agent])
        command.extend(context.cookie_args)
        command.extend(check_args)
        command.append(url)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            **context.subprocess_kwargs,
        )
        stdout = result.stdout.strip().lower()
        stderr = result.stderr.lower()

        if result.returncode != 0:
            if error_handler:
                handled = error_handler(stderr, stdout)
                if handled:
                    return handled
            common_status = status_from_error(stderr)
            if common_status:
                return common_status
            error_hint = context.format_error(result.stderr, result.stdout)
            return SiteStatus("error", error_hint)

        if stdout == "is_live":
            return SiteStatus("online", "🟢 LIVE")
        if stdout in ("was_live", "not_live", "post_live"):
            return SiteStatus("offline", "💤 Offline")
        if not stdout:
            return SiteStatus("error", "❓ Unknown (empty response)")
        return SiteStatus("offline", f"💤 Unknown ({stdout})")
    except subprocess.TimeoutExpired:
        return SiteStatus("error", "⏰ Timeout")
    except subprocess.SubprocessError:
        return SiteStatus("error", "❌ Process error")
    except Exception:
        return SiteStatus("error", "❌ Error")


def status_from_error(stderr: str) -> Optional[SiteStatus]:
    """Preserve common yt-dlp error-to-status mappings."""
    if "currently away" in stderr:
        return SiteStatus("away", "🌙 Away")
    if "hidden session" in stderr:
        return SiteStatus("private", "🔒 Hidden session (private)")
    if "private" in stderr:
        return SiteStatus("private", "🔒 Private show")
    if "age restricted" in stderr or "age-restricted" in stderr:
        return SiteStatus("private", "🔒 Age restricted")
    if "offline" in stderr:
        return SiteStatus("offline", "💤 Offline")
    if "video unavailable" in stderr or "not found" in stderr:
        return SiteStatus("offline", "💤 Stream not found")
    return None


@dataclass
class SiteContext:
    """Runtime configuration supplied by the application to an adapter."""

    user_agent: str = ""
    cookies: list[str] = field(default_factory=list)
    ffmpeg_path: str = "ffmpeg"
    ytdlp_path: str = "yt-dlp"
    target_resolution: str = "1920x1080"
    target_fps: int = 30
    output_directory: Optional[str] = None
