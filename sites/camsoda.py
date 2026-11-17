"""Camsoda site policy."""

import threading
import time

from .base import SiteCheckContext, SitePolicy, SiteStatus, run_ytdlp_status_check


_request_lock = threading.Lock()
_last_request_time = 0.0
_MIN_REQUEST_INTERVAL = 2.0


def wait_for_request() -> None:
    """Wait until the shared CamSoda request interval has elapsed."""
    global _last_request_time
    with _request_lock:
        wait = _MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        _last_request_time = time.monotonic()


POLICY = SitePolicy(
    key="camsoda.com",
    display_name="Camsoda",
    hosts=("camsoda.com",),
    impersonate=True,
    download_args=("--downloader-args", "ffmpeg_i:-extension_picky 0"),
    ffmpeg_args=("-allowed_extensions", "ALL", "-extension_picky", "0"),
    referer="https://www.camsoda.com/",
)


def check_status(url: str, context: SiteCheckContext, check_args: list[str]) -> SiteStatus:
    def handle_error(stderr: str, _stdout: str) -> SiteStatus | None:
        if "no active streams found" in stderr:
            return SiteStatus("offline", "💤 Offline")
        return None

    wait_for_request()
    return run_ytdlp_status_check(url, context, check_args, handle_error)
