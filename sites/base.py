"""Shared site adapter types.

This module intentionally has no Qt or application imports so site discovery can
be used by tests and by the manager's non-GUI helpers.
"""

from dataclasses import dataclass, field
from http.cookiejar import CookieJar
import subprocess
import time
from typing import Callable, Optional
from urllib.parse import urlsplit

_debug_logger: Optional[Callable[[str, str], None]] = None
_debug_exception_logger: Optional[
    Callable[[str, str, Optional[BaseException]], None]
] = None


def configure_debug_logging(
    logger: Callable[[str, str], None],
    exception_logger: Callable[[str, str, Optional[BaseException]], None],
) -> None:
    """Register the application's debug logger for site modules."""
    global _debug_logger, _debug_exception_logger
    _debug_logger = logger
    _debug_exception_logger = exception_logger


def debug_log(component: str, message: str) -> None:
    if _debug_logger is not None:
        _debug_logger(component, message)


def debug_exception(
    component: str,
    message: str,
    exc: Optional[BaseException] = None,
) -> None:
    if _debug_exception_logger is not None:
        _debug_exception_logger(component, message, exc)


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
    cookies: CookieJar | dict[str, str] | None = None


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
    started_at = time.monotonic()
    debug_log("status", f"yt-dlp check started for {url}")
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
            debug_log(
                "status",
                f"yt-dlp check exited {result.returncode} for {url}; "
                f"stderr={result.stderr.strip()[:1200]!r}",
            )
            if error_handler:
                handled = error_handler(stderr, stdout)
                if handled:
                    debug_log(
                        "status",
                        f"site error handler mapped {url} to {handled.value}: "
                        f"{handled.message}",
                    )
                    return handled
            common_status = status_from_error(stderr)
            if common_status:
                debug_log(
                    "status",
                    f"common error mapping classified {url} as "
                    f"{common_status.value}: {common_status.message}",
                )
                return common_status
            error_hint = context.format_error(result.stderr, result.stdout)
            debug_log("status", f"check failed for {url}: {error_hint}")
            return SiteStatus("error", error_hint)

        if stdout == "is_live":
            debug_log(
                "status",
                f"{url} is online (check took {time.monotonic() - started_at:.2f}s)",
            )
            return SiteStatus("online", "🟢 LIVE")
        if stdout in ("was_live", "not_live", "post_live"):
            debug_log(
                "status",
                f"{url} is offline (yt-dlp={stdout}, "
                f"check took {time.monotonic() - started_at:.2f}s)",
            )
            return SiteStatus("offline", "💤 Offline")
        if not stdout:
            debug_log(
                "status",
                f"empty yt-dlp status for {url}; stderr={result.stderr.strip()[:1200]!r}",
            )
            return SiteStatus("error", "❓ Unknown (empty response)")
        debug_log("status", f"unrecognized yt-dlp status for {url}: {stdout!r}")
        return SiteStatus("offline", f"💤 Unknown ({stdout})")
    except subprocess.TimeoutExpired as exc:
        debug_exception("status", f"yt-dlp check timed out for {url}", exc)
        return SiteStatus("error", "⏰ Timeout")
    except subprocess.SubprocessError as exc:
        debug_exception("status", f"yt-dlp process error for {url}", exc)
        return SiteStatus("error", "❌ Process error")
    except Exception as exc:
        debug_exception("status", f"unexpected status-check error for {url}", exc)
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
