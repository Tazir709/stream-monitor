"""Chaturbate site policy."""

from .base import SiteCheckContext, SitePolicy, SiteStatus, run_ytdlp_status_check


POLICY = SitePolicy(
    key="chaturbate.com",
    display_name="Chaturbate",
    hosts=("chaturbate.com",),
    referer="https://chaturbate.com/",
)


def check_status(url: str, context: SiteCheckContext, check_args: list[str]) -> SiteStatus:
    return run_ytdlp_status_check(url, context, check_args)
