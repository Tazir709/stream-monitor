"""Camsoda site policy."""

from .base import SiteCheckContext, SitePolicy, SiteStatus, run_ytdlp_status_check


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

    return run_ytdlp_status_check(url, context, check_args, handle_error)
