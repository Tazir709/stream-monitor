"""Site policy registry and hostname-based dispatch."""

from urllib.parse import urlsplit

from .base import SiteCheckContext, SitePolicy, SiteStatus
from .bongacams import POLICY as BONGACAMS_POLICY
from .bongacams import check_status as check_bongacams_status
from .camsoda import POLICY as CAMSODA_POLICY
from .camsoda import check_status as check_camsoda_status
from .chaturbate import POLICY as CHATURBATE_POLICY
from .chaturbate import check_status as check_chaturbate_status


SITE_POLICIES = (CHATURBATE_POLICY, CAMSODA_POLICY, BONGACAMS_POLICY)
SITE_CHECKERS = {
    CHATURBATE_POLICY.key: check_chaturbate_status,
    CAMSODA_POLICY.key: check_camsoda_status,
    BONGACAMS_POLICY.key: check_bongacams_status,
}


def hostname_from_url(url: str) -> str:
    """Return a normalized hostname, or an empty string for malformed input."""
    value = (url or "").strip()
    if "://" not in value:
        value = "https://" + value
    try:
        return (urlsplit(value).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def get_site_policy(url: str) -> SitePolicy | None:
    hostname = hostname_from_url(url)
    return next((policy for policy in SITE_POLICIES if policy.matches_host(hostname)), None)


def display_name_for_url(url: str) -> str:
    policy = get_site_policy(url)
    if policy:
        return policy.display_name
    hostname = hostname_from_url(url)
    label = hostname.split(".")[0] if hostname else ""
    return label.capitalize() if label else "Unknown"


def impersonate_args(policy: SitePolicy | None, browser: str) -> list[str]:
    if not policy or not policy.impersonate:
        return []
    target = browser.split(":", 1)[0].strip() if policy.impersonate is True else str(policy.impersonate)
    return ["--impersonate", target] if target else []


def check_args_for_url(url: str, browser: str) -> list[str]:
    return impersonate_args(get_site_policy(url), browser)


def download_args_for_url(url: str, browser: str) -> list[str]:
    policy = get_site_policy(url)
    return impersonate_args(policy, browser) + (list(policy.download_args) if policy else [])


def referer_for_url(url: str) -> str:
    policy = get_site_policy(url)
    return policy.referer if policy else ""


def ffmpeg_args_for_url(url: str) -> list[str]:
    policy = get_site_policy(url)
    return list(policy.ffmpeg_args) if policy else []


def requires_authentication(url: str, browser: str) -> bool:
    policy = get_site_policy(url)
    return bool(policy and policy.impersonate is True and not browser)


def check_status_for_url(url: str, context: SiteCheckContext) -> SiteStatus:
    policy = get_site_policy(url)
    checker = SITE_CHECKERS.get(policy.key) if policy else None
    if not checker:
        from .base import run_ytdlp_status_check
        return run_ytdlp_status_check(url, context, check_args_for_url(url, context.browser))
    return checker(url, context, check_args_for_url(url, context.browser))
