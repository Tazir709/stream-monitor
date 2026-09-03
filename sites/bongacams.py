"""BongaCams site policy."""

from .base import SiteCheckContext, SitePolicy, SiteStatus, run_ytdlp_status_check


POLICY = SitePolicy(
    key="bongacams.com",
    display_name="BongaCams",
    hosts=("bongacams.com",),
    impersonate=True,
)


def check_status(url: str, context: SiteCheckContext, check_args: list[str]) -> SiteStatus:
    return run_ytdlp_status_check(url, context, check_args)
