"""
Platform knowledge (spec §C.1, §B.5, §C.3) — data, not judgment.

Everything here comes from the V&I spec and the research notes it cites
(`research_notes/Verification and Integrity department/platform_apis_and_fraud.md`).
UNVERIFIED facts are labelled so in comments and in ADR 0007.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlsplit

PLATFORMS = ("youtube", "tiktok", "instagram", "x", "snapchat", "twitch")
CERTIFIABLE = ("youtube", "tiktok", "instagram", "x")        # an adapter exists (x only behind its flag)
NOT_PAYABLE = ("snapchat", "twitch")                         # §0.2 D2, §0.3 R1

# §C.1 scopes: a connection must be granted EXACTLY these (a broader grant is refused: least access).
SCOPES: dict[str, tuple[str, ...]] = {
    "youtube": ("https://www.googleapis.com/auth/youtube.readonly",
                "https://www.googleapis.com/auth/yt-analytics.readonly"),
    "tiktok": ("user.info.basic", "video.list"),
    "instagram": ("instagram_business_basic", "instagram_business_manage_insights"),
    # X user-context scopes are not in the research (UNVERIFIED); X stays off by default.
    "x": ("tweet.read", "users.read"),
}

# The payable metric (B.3 metric_name) and its snapshot metric name.
PAYABLE_METRIC = {"youtube": "views", "tiktok": "views", "instagram": "views", "x": "impressions"}
METRIC_NAME = {"youtube": "views", "tiktok": "views", "instagram": "views", "x": "impressions"}

METRICS = ("views", "likes", "comments", "shares", "impressions", "engaged_views", "est_minutes_watched",
           "avg_view_duration_s", "avg_view_percentage", "reels_avg_watch_time_ms", "country_views")

# What each adapter can return (research §C.1). Anything else is "not_applicable" for that platform.
AVAILABLE_METRICS: dict[str, tuple[str, ...]] = {
    "youtube": ("views", "likes", "comments", "engaged_views", "est_minutes_watched", "avg_view_duration_s",
                "avg_view_percentage", "country_views"),
    "tiktok": ("views", "likes", "comments", "shares"),
    "instagram": ("views", "likes", "comments", "shares", "reels_avg_watch_time_ms"),
    "x": ("impressions", "likes"),
}

SOURCE_ENDPOINTS = (
    "youtube_data_v3_videos_list", "youtube_analytics_reports_query", "tiktok_display_v2_video_list",
    "instagram_graph_media_insights", "x_v2_tweets_lookup", "tiktok_oembed",
)
# The endpoint a platform's metric fetch must name (N16-8: an answer naming another or no endpoint, or carrying
# no 64-hex response hash, is not evidence and never becomes a snapshot).
FETCH_ENDPOINTS = {"youtube": ("youtube_data_v3_videos_list", "youtube_analytics_reports_query"),
                   "tiktok": ("tiktok_display_v2_video_list",), "instagram": ("instagram_graph_media_insights",),
                   "x": ("x_v2_tweets_lookup",)}

# Same-clip evidence per platform (§C.3): only TikTok can have a perceptual (cover PDQ) check.
PERCEPTUAL_CAPABLE = ("tiktok",)

# §B.5 retention: which rule governs the raw (non-statistics) platform fields of each platform.
RAW_RETENTION_RULE = {"youtube": "VI-15b", "tiktok": "VI-15c", "instagram": "VI-15d", "x": "VI-15e"}
STATS_RETENTION_RULE = {"youtube": "VI-15a", "tiktok": "VI-15c", "instagram": "VI-15d", "x": "VI-15e"}
RAW_REFRESH_DAYS = 30

# YouTube counts a view "the moment a video starts to play" from Aug 24, 2026 (YouTube Help 2991785):
# anomaly baselines never mix clips on both sides of this instant.
YT_VIEW_DEFINITION_CHANGE = datetime(2026, 8, 24, tzinfo=timezone.utc)

# Instagram eligibility (§C.1): professional account and >= 100 followers.
IG_MIN_FOLLOWERS = 100

# YouTube quota cost of one list read ("usually costs 1 unit"; invalid requests cost at least 1).
YT_UNITS_PER_READ = 1


# --- one canonical post identity (bug sweep C, C-2) -------------------------------------------------------------
# The same post must never be registered (and paid) under two submissions. A platform URL has many spellings
# (www./m. hosts, query strings such as ?lang= or ?is_from_webapp=, fragments, a trailing slash, a different @handle
# in front of the same TikTok video id, upper-case schemes and hosts), so a submission is keyed by the platform's own
# post id when the reference names one, else by the normalized URL. A reference that names no id (a short link such
# as vm.tiktok.com/...) is caught after the first fetch instead, by the platform's video id (service: video_owner).
_POST_ID = {
    "tiktok": (re.compile(r"/(?:@[^/]+/)?(?:video|photo)/([0-9A-Za-z_-]{1,64})(?:/|$)"),
               re.compile(r"^/v/([0-9]{1,30})(?:\.html)?/?$")),
    "youtube": (re.compile(r"^/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{6,20})(?:/|$)"),),
    "instagram": (re.compile(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]{1,64})(?:/|$)"),),
    "x": (re.compile(r"/status(?:es)?/([0-9]{1,30})(?:/|$)"),),
}
_HOST_PREFIXES = ("www.", "m.", "mobile.")
_BARE_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def post_key(platform: str, post_ref: str) -> str:
    """``<platform>:id:<post id>`` when ``post_ref`` names the platform's post id, else ``<platform>:url:<host><path>``
    (NFKC, scheme, case of the host, ``www.``/``m.``/``mobile.``, query, fragment and trailing slash ignored). Pure."""
    raw = unicodedata.normalize("NFKC", post_ref or "").strip()
    if platform == "youtube" and _BARE_YT_ID.fullmatch(raw):
        return f"youtube:id:{raw}"
    text = raw if "://" in raw else "https://" + raw
    try:
        u = urlsplit(text)
        host = (u.hostname or "").lower().rstrip(".")
    except ValueError:
        return f"{platform}:raw:{raw}"
    for pre in _HOST_PREFIXES:
        if host.startswith(pre):
            host = host[len(pre):]
    path = unquote(u.path or "/")
    if platform == "youtube":
        if host == "youtu.be":
            m = re.match(r"^/([A-Za-z0-9_-]{6,20})(?:/|$)", path)
            if m:
                return f"youtube:id:{m.group(1)}"
        v = parse_qs(u.query).get("v")
        if v and re.fullmatch(r"[A-Za-z0-9_-]{6,20}", v[0]):
            return f"youtube:id:{v[0]}"
    if platform == "tiktok":
        item = parse_qs(u.query).get("item_id")
        if item and re.fullmatch(r"[0-9]{1,30}", item[0]):
            return f"tiktok:id:{item[0]}"
    if platform == "x" and path.startswith("/i/web/status/"):
        path = path[len("/i/web"):]
    for rx in _POST_ID.get(platform, ()):
        m = rx.search(path)
        if m:
            return f"{platform}:id:{m.group(1)}"
    return f"{platform}:url:{host}{path.rstrip('/') or '/'}"
