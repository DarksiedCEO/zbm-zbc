"""
Platform knowledge (spec §C.1, §B.5, §C.3) — data, not judgment.

Everything here comes from the V&I spec and the research notes it cites
(`research_notes/Verification and Integrity department/platform_apis_and_fraud.md`).
UNVERIFIED facts are labelled so in comments and in ADR 0007.
"""

from __future__ import annotations

from datetime import datetime, timezone

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
    "instagram_graph_media_insights", "x_v2_tweets_lookup", "tiktok_oembed", "fake",
)

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
