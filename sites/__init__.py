"""Site adapter and policy registry exports."""

from .base import SiteCheckContext, SiteContext, SitePolicy, SiteStatus, extract_username
from .bongacams_ru4 import BongaCamsRu4SiteAdapter
from .registry import (
	check_args_for_url,
	display_name_for_url,
	download_args_for_url,
	ffmpeg_args_for_url,
	get_site_policy,
	hostname_from_url,
	normalize_site_url,
	check_status_for_url,
	requires_authentication,
	referer_for_url,
)

__all__ = [
	"SiteContext",
	"SiteCheckContext",
	"SitePolicy",
	"SiteStatus",
	"BongaCamsRu4SiteAdapter",
	"check_args_for_url",
	"display_name_for_url",
	"download_args_for_url",
	"ffmpeg_args_for_url",
	"get_site_policy",
	"hostname_from_url",
	"normalize_site_url",
	"extract_username",
	"check_status_for_url",
	"requires_authentication",
	"referer_for_url",
]
