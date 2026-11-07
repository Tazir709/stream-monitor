"""Native RU4 BongaCams stream extraction."""

from dataclasses import dataclass
from http.cookiejar import CookieJar
import json
import re
from typing import Optional
from urllib.parse import quote, urlsplit

import requests

from .base import SiteCheckContext, SitePolicy, SiteStatus, debug_exception


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:159.0) "
    "Gecko/20100101 Firefox/159.0"
)
REFERER = "https://ru4.bongocams.com/"
PAGE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-GB,en;q=0.5",
    "Referer": REFERER,
}
_INITIAL_STATE_RE = re.compile(
    r"<script\b(?=[^>]*\bdata-type=[\"']initialState[\"'])[^>]*>"
    r"(.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)

POLICY = SitePolicy(
    key="ru4.bongocams.com",
    display_name="BongaCams RU4",
    hosts=("ru4.bongocams.com",),
    referer=REFERER,
    impersonate=True,
)


@dataclass(frozen=True)
class BongaCamsStreamInfo:
    username: str
    cdn_hostname: str
    is_available: bool

    @property
    def stream_url(self) -> str:
        return (
            f"https://{self.cdn_hostname}/hls/"
            f"stream_{quote(self.username, safe='')}/playlist.m3u8"
        )


def username_from_url(url: str) -> str:
    value = (url or "").strip()
    if "://" not in value:
        value = "https://" + value
    try:
        parts = [part for part in urlsplit(value).path.split("/") if part]
    except ValueError:
        return ""
    return parts[0] if parts else ""


def _cdn_hostname(frame_url: str) -> str:
    try:
        hostname = (urlsplit(frame_url).hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise ValueError("BongaCams returned an invalid initialVideoPoster.frame") from exc

    if hostname.startswith("live-"):
        cdn_hostname = hostname
    elif hostname.startswith("mobile-"):
        cdn_hostname = f"live-{hostname[len('mobile-'):]}"
    else:
        cdn_hostname = f"live-{hostname}"

    if not cdn_hostname.endswith(".bcvcdn.com"):
        raise ValueError("BongaCams returned an unrecognized CDN hostname")
    return cdn_hostname


def extract_stream_info(html: str) -> BongaCamsStreamInfo:
    raw_states = _INITIAL_STATE_RE.findall(html)
    if not raw_states:
        raise ValueError("BongaCams initialState scripts were not found")

    states = []
    malformed_states = 0
    for raw_state in raw_states:
        try:
            state = json.loads(raw_state)
        except ValueError:
            malformed_states += 1
            continue
        if isinstance(state, dict):
            states.append(state)
    if not states:
        raise ValueError("BongaCams initialState JSON could not be parsed")

    video_state = next(
        (state for state in states if "initialVideoPoster" in state), None
    )
    options_state = next(
        (state for state in states if "streamOptions" in state), None
    )
    if video_state is None or options_state is None:
        message = "BongaCams initialState is missing video or stream options"
        if malformed_states:
            message += f" ({malformed_states} malformed state block(s))"
        raise ValueError(message)

    poster = video_state["initialVideoPoster"]
    options = options_state["streamOptions"]
    if not isinstance(poster, dict) or not isinstance(options, dict):
        raise ValueError("BongaCams returned invalid video or stream options")

    username = poster.get("username")
    frame_url = poster.get("frame")
    if not isinstance(username, str) or not username.strip():
        raise ValueError("BongaCams initialVideoPoster has no broadcaster username")
    is_available = options.get("isAvailable")
    if not isinstance(is_available, bool):
        raise ValueError("BongaCams streamOptions has no valid isAvailable flag")
    if not is_available:
        return BongaCamsStreamInfo(
            username=username.strip(),
            cdn_hostname="",
            is_available=False,
        )
    if not isinstance(frame_url, str) or not frame_url:
        raise ValueError("BongaCams initialVideoPoster has no frame URL")

    return BongaCamsStreamInfo(
        username=username.strip(),
        cdn_hostname=_cdn_hostname(frame_url),
        is_available=True,
    )


def _fetch_page(
    url: str,
    user_agent: Optional[str],
    cookies: CookieJar | dict[str, str] | None = None,
) -> str:
    username = username_from_url(url)
    if not username:
        raise ValueError("A BongaCams broadcaster username is required")
    headers = dict(PAGE_HEADERS)
    headers["User-Agent"] = user_agent or USER_AGENT
    response = requests.get(
        f"https://ru4.bongocams.com/{quote(username, safe='')}",
        headers=headers,
        cookies=cookies,
        timeout=30,
    )
    response.raise_for_status()
    return response.text


class BongaCamsRu4SiteAdapter:
    """Application-facing RU4 operations using the page's embedded state."""

    @staticmethod
    def matches(url: str) -> bool:
        value = (url or "").strip()
        if "://" not in value:
            value = "https://" + value
        try:
            hostname = (urlsplit(value).hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        return (
            hostname == "ru4.bongocams.com"
            or hostname.endswith(".ru4.bongocams.com")
        )

    def check(
        self,
        url: str,
        user_agent: Optional[str] = None,
        cookies: CookieJar | dict[str, str] | None = None,
    ) -> SiteStatus:
        username = username_from_url(url)
        if not username:
            return SiteStatus("error", "❌ Invalid BongaCams RU4 broadcaster URL")
        try:
            html = _fetch_page(url, user_agent, cookies)
            if "verify you are 18" in html.lower():
                return SiteStatus("private", "🔒 Age verification required")
            info = extract_stream_info(html)
        except requests.RequestException as exc:
            debug_exception("BongaCams RU4", f"page request failed for {username}", exc)
            return SiteStatus("error", f"❌ BongaCams RU4 request failed: {exc}")
        except ValueError as exc:
            debug_exception("BongaCams RU4", f"page data invalid for {username}", exc)
            return SiteStatus("error", f"❌ BongaCams RU4: {exc}")
        if not info.is_available:
            return SiteStatus("offline", "💤 Offline")
        return SiteStatus("online", "🟢 LIVE")

    def resolve_stream_url(
        self,
        url: str,
        user_agent: Optional[str] = None,
        cookies: CookieJar | dict[str, str] | None = None,
    ) -> str:
        username = username_from_url(url)
        if not username:
            raise ValueError("A BongaCams broadcaster username is required")
        html = _fetch_page(url, user_agent, cookies)
        if "verify you are 18" in html.lower():
            raise RuntimeError("BongaCams requires age verification")
        info = extract_stream_info(html)
        if not info.is_available:
            raise RuntimeError("BongaCams broadcaster is not currently live")
        return info.stream_url


def check_status(
    url: str,
    context: SiteCheckContext,
    _check_args: list[str],
) -> SiteStatus:
    del _check_args
    return BongaCamsRu4SiteAdapter().check(
        url,
        user_agent=context.user_agent,
        cookies=context.cookies,
    )
