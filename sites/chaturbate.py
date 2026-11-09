"""Chaturbate site policy."""

from .base import SiteCheckContext, SitePolicy, SiteStatus, run_ytdlp_status_check


POLICY = SitePolicy(
    key="chaturbate.com",
    display_name="Chaturbate",
    hosts=("chaturbate.com",),
    referer="https://chaturbate.com/",
)


def check_status(url: str, context: SiteCheckContext, check_args: list[str]) -> SiteStatus:
    def handle_error(stderr: str, _stdout: str) -> SiteStatus | None:
        if "room is password protected" in stderr:
            return SiteStatus("private", "🔒 Password protected")
        return None

    return run_ytdlp_status_check(url, context, check_args, handle_error)
