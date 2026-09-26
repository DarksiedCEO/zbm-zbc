"""
Real HTTP platform adapters (spec §C.1) — built and mock-tested, NOT wired in this build.

Endpoints are the documented ones in the research notes. Fields the research did not confirm are marked
UNVERIFIED here and in ADR 0007 choice 9; they must be checked against the live platform docs before the
vault is wired (Cybersecurity 22, AEGIS review). Each adapter:
- asks the vault for the token inside ``with_token`` and uses it only in that call's Authorization header;
- returns counts exactly as the platform returned them (``as_int``: ints or decimal strings; anything else
  → the metric is absent, never guessed);
- hashes the raw response body (``source_response_sha256``) and keeps nothing else of it;
- converts every failure into ``available=False`` / ``live_state="unknown"`` with no message text.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

from adapters.base import CallRefused, Http, TransportFailed, as_int
from ports import AccountAnswer, AdapterAnswer, VideoFacts

YT_DATA = "https://www.googleapis.com/youtube/v3/"
YT_ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
TT_API = "https://open.tiktokapis.com/v2/"
TT_OEMBED = "https://www.tiktok.com/oembed"
IG_API = "https://graph.instagram.com/v22.0/"     # host/version for "Instagram API with Instagram Login": UNVERIFIED
X_API = "https://api.x.com/2/"

_FAILED = AdapterAnswer(False, live_state="unknown")


def _combined_sha(*shas: Optional[str]) -> Optional[str]:
    parts = [s for s in shas if s]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else hashlib.sha256("|".join(parts).encode()).hexdigest()


def _epoch(rfc3339: object) -> Optional[int]:
    if not isinstance(rfc3339, str) or len(rfc3339) > 40:
        return None
    try:
        dt = datetime.fromisoformat(rfc3339.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return int(dt.astimezone(timezone.utc).timestamp())


def _guard(fn, failed):
    try:
        return fn()
    except (CallRefused, TransportFailed, ValueError, KeyError, TypeError, AttributeError, IndexError):
        return failed
    except Exception:  # noqa: BLE001 - VaultUnavailable and anything else: unavailable, message dropped
        return failed


class YouTubeAdapter:
    platform = "youtube"
    ALLOWED = (("GET", YT_DATA + "videos"), ("GET", YT_DATA + "channels"), ("GET", YT_ANALYTICS))
    _ID = re.compile(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{6,20})|^([A-Za-z0-9_-]{6,20})$")

    def __init__(self, transport: Optional[httpx.BaseTransport] = None):
        self.http = Http(self.ALLOWED, transport)

    def video_id(self, video_ref: str) -> Optional[str]:
        m = self._ID.search(video_ref or "")
        return (m.group(1) or m.group(2)) if m else None

    def account(self, vault, vault_ref):
        def run(token):
            # channels.list mine=true (a list read, 1 unit); `mine` parameter UNVERIFIED in the research
            r = self.http.call("GET", YT_DATA + "channels", token, params={"part": "id", "mine": "true"})
            items = (r.json() or {}).get("items") if r.status == 200 else None
            if not items or not isinstance(items[0].get("id"), str):
                return AccountAnswer(False, http_status=r.status)
            return AccountAnswer(True, items[0]["id"], http_status=r.status)
        return _guard(lambda: vault.with_token(vault_ref, "youtube_account", run), AccountAnswer(False))

    def fetch(self, vault, vault_ref, account_id, video_ref, metrics, hint_time):
        vid = self.video_id(video_ref)
        if not vid:
            return _FAILED

        def run(token):
            # videos.list part=statistics (viewCount/likeCount/commentCount: research) + snippet
            # (channelId, publishedAt, description: UNVERIFIED in the research, needed for author and create time)
            r = self.http.call("GET", YT_DATA + "videos", token, params={"part": "statistics,snippet", "id": vid})
            cost = 1
            if r.status == 429:
                return AdapterAnswer(False, source_endpoint="youtube_data_v3_videos_list", cost_units=cost,
                                     http_status=429, rate_limited=True)
            data = r.json() if r.status == 200 else None
            if not isinstance(data, dict):
                return AdapterAnswer(False, source_endpoint="youtube_data_v3_videos_list", cost_units=cost,
                                     http_status=r.status)
            items = data.get("items") or []
            if not items:
                return AdapterAnswer(True, live_state="gone", source_endpoint="youtube_data_v3_videos_list",
                                     source_response_sha256=r.sha256, cost_units=cost, http_status=r.status)
            it = items[0]
            st, sn = it.get("statistics") or {}, it.get("snippet") or {}
            values = {k: v for k, v in (("views", as_int(st.get("viewCount"))), ("likes", as_int(st.get("likeCount"))),
                                        ("comments", as_int(st.get("commentCount")))) if v is not None}
            created = _epoch(sn.get("publishedAt"))
            if it.get("id") != vid or not isinstance(sn.get("channelId"), str) or created is None:
                return AdapterAnswer(False, source_endpoint="youtube_data_v3_videos_list", cost_units=cost,
                                     http_status=r.status)
            video = VideoFacts(vid, sn["channelId"], created, None, sn.get("description"))
            shas = [r.sha256]
            country: dict = {}
            wanted = [m for m in ("engaged_views", "est_minutes_watched", "avg_view_duration_s", "avg_view_percentage")
                      if m in metrics]
            day = datetime.now(timezone.utc).date().isoformat()
            start = datetime.fromtimestamp(created, timezone.utc).date().isoformat()
            if wanted:
                names = {"engaged_views": "engagedViews", "est_minutes_watched": "estimatedMinutesWatched",
                         "avg_view_duration_s": "averageViewDuration", "avg_view_percentage": "averageViewPercentage"}
                a = self.http.call("GET", YT_ANALYTICS, token, params={
                    "ids": "channel==MINE", "metrics": ",".join(names[m] for m in wanted),
                    "filters": f"video=={vid}", "startDate": start, "endDate": day})
                cost += 1
                rows = ((a.json() or {}).get("rows") or [[]]) if a.status == 200 else [[]]
                for m, v in zip(wanted, rows[0] if rows else []):
                    iv = as_int(v) if not isinstance(v, float) else (int(v) if v >= 0 and v == int(v) else None)
                    if iv is not None:
                        values[m] = iv
                shas.append(a.sha256)
            if "country_views" in metrics:
                # lifetime-per-video country split only (video+day+country in one query is UNVERIFIED)
                a = self.http.call("GET", YT_ANALYTICS, token, params={
                    "ids": "channel==MINE", "metrics": "views", "dimensions": "country",
                    "filters": f"video=={vid}", "startDate": start, "endDate": day})
                cost += 1
                for row in ((a.json() or {}).get("rows") or []) if a.status == 200 else []:
                    if (isinstance(row, list) and len(row) == 2 and isinstance(row[0], str)
                            and re.fullmatch(r"[A-Z]{2}", row[0]) and as_int(row[1]) is not None):
                        country[row[0]] = as_int(row[1])
                shas.append(a.sha256)
            return AdapterAnswer(True, values, country, video, "live", "youtube_data_v3_videos_list",
                                 _combined_sha(*shas), cost, r.status)
        return _guard(lambda: vault.with_token(vault_ref, "youtube_fetch", run), _FAILED)


class TikTokAdapter:
    platform = "tiktok"
    ALLOWED = (("POST", TT_API + "video/list/"), ("GET", TT_API + "user/info/"))
    FIELDS = "id,create_time,share_url,duration,video_description,cover_image_url,view_count,like_count,comment_count,share_count"
    _ID = re.compile(r"/video/([0-9]{5,25})|^([0-9]{5,25})$")
    MAX_PAGES = 5

    def __init__(self, transport: Optional[httpx.BaseTransport] = None, cover_transport=None, fetch_cover: bool = False):
        self.http = Http(self.ALLOWED, transport)
        self.fetch_cover = fetch_cover          # VI_TT_COVER_PDQ (default 0, VI-CQ-05)
        self.cover_transport = cover_transport

    def video_id(self, video_ref: str) -> Optional[str]:
        m = self._ID.search(video_ref or "")
        return (m.group(1) or m.group(2)) if m else None

    def account(self, vault, vault_ref):
        def run(token):
            r = self.http.call("GET", TT_API + "user/info/", token, params={"fields": "open_id"})
            user = (((r.json() or {}).get("data") or {}).get("user") or {}) if r.status == 200 else {}
            if not isinstance(user.get("open_id"), str):
                return AccountAnswer(False, http_status=r.status)
            return AccountAnswer(True, user["open_id"], http_status=r.status)
        return _guard(lambda: vault.with_token(vault_ref, "tiktok_account", run), AccountAnswer(False))

    def fetch(self, vault, vault_ref, account_id, video_ref, metrics, hint_time):
        vid = self.video_id(video_ref)
        if not vid or hint_time is None:
            return _FAILED

        def run(token):
            # lookup (§C.1): cursor = (create_time + 1) * 1000, max_count 20, match id; page older while has_more
            cursor = (int(hint_time) + 1) * 1000
            shas = []
            for _ in range(self.MAX_PAGES):
                r = self.http.call("POST", TT_API + "video/list/", token, params={"fields": self.FIELDS},
                                   json_body={"cursor": cursor, "max_count": 20})
                if r.status == 429:
                    return AdapterAnswer(False, source_endpoint="tiktok_display_v2_video_list", http_status=429,
                                         rate_limited=True)
                data = (r.json() or {}).get("data") if r.status == 200 else None
                if not isinstance(data, dict):
                    return AdapterAnswer(False, source_endpoint="tiktok_display_v2_video_list", http_status=r.status)
                shas.append(r.sha256)
                for v in data.get("videos") or []:
                    if isinstance(v, dict) and str(v.get("id")) == vid:
                        values = {k: n for k, n in (("views", as_int(v.get("view_count"))),
                                                    ("likes", as_int(v.get("like_count"))),
                                                    ("comments", as_int(v.get("comment_count"))),
                                                    ("shares", as_int(v.get("share_count")))) if n is not None}
                        ct = as_int(v.get("create_time"))
                        if ct is None:
                            return AdapterAnswer(False, source_endpoint="tiktok_display_v2_video_list")
                        dur = as_int(v.get("duration"))
                        cover = self._cover(v.get("cover_image_url")) if self.fetch_cover else None
                        video = VideoFacts(vid, account_id, ct, dur * 1000 if dur is not None else None,
                                           v.get("video_description"), cover,
                                           v.get("share_url") if isinstance(v.get("share_url"), str) else None)
                        return AdapterAnswer(True, values, {}, video, "live", "tiktok_display_v2_video_list",
                                             _combined_sha(*shas), 0, r.status)
                nxt = as_int(data.get("cursor"))
                if not data.get("has_more") or nxt is None or nxt >= cursor:
                    break
                cursor = nxt
            # not in the owner's list at its cursor: gone (§C.4)
            return AdapterAnswer(True, {}, {}, None, "gone", "tiktok_display_v2_video_list", _combined_sha(*shas), 0, 200)
        return _guard(lambda: vault.with_token(vault_ref, "tiktok_fetch", run), _FAILED)

    def _cover(self, url) -> Optional[bytes]:
        # the cover image (not posted media) is read into memory only, within its 6-hour TTL; host UNVERIFIED
        if not isinstance(url, str) or not url.startswith("https://"):
            return None
        try:
            with httpx.Client(timeout=10.0, transport=self.cover_transport, follow_redirects=False) as c:
                r = c.get(url)
            return r.content[:5 * 1024 * 1024] if r.status_code == 200 else None
        except httpx.HTTPError:
            return None


class InstagramAdapter:
    platform = "instagram"
    ALLOWED = (("GET", IG_API),)
    _ID = re.compile(r"^([0-9]{5,25})$")
    INSIGHTS = {"views": "views", "likes": "likes", "comments": "comments", "shares": "shares",
                "reels_avg_watch_time_ms": "ig_reels_avg_watch_time"}

    def __init__(self, transport: Optional[httpx.BaseTransport] = None):
        self.http = Http(self.ALLOWED, transport)

    def account(self, vault, vault_ref):
        def run(token):
            # /me fields user_id, account_type, followers_count: UNVERIFIED in the research
            r = self.http.call("GET", IG_API + "me", token, params={"fields": "user_id,account_type,followers_count"})
            d = r.json() if r.status == 200 else None
            if not isinstance(d, dict) or not isinstance(d.get("user_id"), (str, int)):
                return AccountAnswer(False, http_status=r.status)
            return AccountAnswer(True, str(d["user_id"]), d.get("account_type") in ("BUSINESS", "MEDIA_CREATOR"),
                                 as_int(d.get("followers_count")), r.status)
        return _guard(lambda: vault.with_token(vault_ref, "instagram_account", run), AccountAnswer(False))

    def fetch(self, vault, vault_ref, account_id, video_ref, metrics, hint_time):
        m = self._ID.fullmatch(video_ref or "")
        if not m:
            return _FAILED       # only a numeric media id can be looked up (permalink resolution UNVERIFIED)
        mid = m.group(1)

        def run(token):
            r = self.http.call("GET", IG_API + mid, token, params={"fields": "id,timestamp,caption,owner"})
            if r.status == 429:
                return AdapterAnswer(False, source_endpoint="instagram_graph_media_insights", http_status=429,
                                     rate_limited=True)
            if r.status == 404:
                return AdapterAnswer(True, live_state="gone", source_endpoint="instagram_graph_media_insights",
                                     source_response_sha256=r.sha256, http_status=404)
            d = r.json() if r.status == 200 else None
            owner = ((d or {}).get("owner") or {}).get("id") if isinstance(d, dict) else None
            created = _epoch((d or {}).get("timestamp")) if isinstance(d, dict) else None
            if not isinstance(d, dict) or created is None or owner is None:
                return AdapterAnswer(False, source_endpoint="instagram_graph_media_insights", http_status=r.status)
            wanted = [k for k in self.INSIGHTS if k in metrics]
            values: dict = {}
            shas = [r.sha256]
            if wanted:
                a = self.http.call("GET", IG_API + mid + "/insights", token,
                                   params={"metric": ",".join(self.INSIGHTS[k] for k in wanted)})
                shas.append(a.sha256)
                for row in ((a.json() or {}).get("data") or []) if a.status == 200 else []:
                    name = row.get("name") if isinstance(row, dict) else None
                    vals = row.get("values") if isinstance(row, dict) else None
                    key = next((k for k, v in self.INSIGHTS.items() if v == name), None)
                    if key and isinstance(vals, list) and vals and isinstance(vals[0], dict):
                        iv = as_int(vals[0].get("value"))
                        if iv is not None:
                            values[key] = iv
            video = VideoFacts(mid, str(owner), created, None, d.get("caption") if isinstance(d.get("caption"), str) else None)
            return AdapterAnswer(True, values, {}, video, "live", "instagram_graph_media_insights",
                                 _combined_sha(*shas), 0, r.status)
        return _guard(lambda: vault.with_token(vault_ref, "instagram_fetch", run), _FAILED)


class XAdapter:
    platform = "x"
    ALLOWED = (("GET", X_API + "tweets/"), ("GET", X_API + "users/me"))
    _ID = re.compile(r"/status/([0-9]{5,25})|^([0-9]{5,25})$")

    def __init__(self, transport: Optional[httpx.BaseTransport] = None):
        self.http = Http(self.ALLOWED, transport)

    def account(self, vault, vault_ref):
        def run(token):
            r = self.http.call("GET", X_API + "users/me", token)
            d = ((r.json() or {}).get("data") or {}) if r.status == 200 else {}
            if not isinstance(d.get("id"), str):
                return AccountAnswer(False, http_status=r.status)
            return AccountAnswer(True, d["id"], http_status=r.status)
        return _guard(lambda: vault.with_token(vault_ref, "x_account", run), AccountAnswer(False))

    def fetch(self, vault, vault_ref, account_id, video_ref, metrics, hint_time):
        m = self._ID.search(video_ref or "")
        if not m:
            return _FAILED
        pid = m.group(1) or m.group(2)

        def run(token):
            r = self.http.call("GET", X_API + "tweets/" + pid, token,
                               params={"tweet.fields": "public_metrics,created_at,author_id,text"})
            if r.status == 429:
                return AdapterAnswer(False, source_endpoint="x_v2_tweets_lookup", http_status=429, rate_limited=True,
                                     cost_units=1)
            if r.status == 404:
                return AdapterAnswer(True, live_state="gone", source_endpoint="x_v2_tweets_lookup",
                                     source_response_sha256=r.sha256, http_status=404, cost_units=1)
            d = ((r.json() or {}).get("data") or {}) if r.status == 200 else {}
            pm = d.get("public_metrics") or {}
            created = _epoch(d.get("created_at"))
            if d.get("id") != pid or not isinstance(d.get("author_id"), str) or created is None:
                return AdapterAnswer(False, source_endpoint="x_v2_tweets_lookup", http_status=r.status, cost_units=1)
            values = {k: v for k, v in (("impressions", as_int(pm.get("impression_count"))),
                                        ("likes", as_int(pm.get("like_count")))) if v is not None}
            video = VideoFacts(pid, d["author_id"], created, None, d.get("text") if isinstance(d.get("text"), str) else None)
            return AdapterAnswer(True, values, {}, video, "live", "x_v2_tweets_lookup", r.sha256, 1, r.status)
        return _guard(lambda: vault.with_token(vault_ref, "x_fetch", run), _FAILED)


class TikTokOEmbed:
    """Public fallback (§C.4): GET https://www.tiktok.com/oembed?url=<share_url>; no auth, no cookies.
    200 → live; 400/404 → gone; anything else → unknown. Wired only with VI_OEMBED_ENABLED=1."""

    ALLOWED = (("GET", TT_OEMBED),)

    def __init__(self, transport: Optional[httpx.BaseTransport] = None):
        self.http = Http(self.ALLOWED, transport)

    def check(self, share_url: str) -> str:
        if not isinstance(share_url, str) or not share_url.startswith("https://www.tiktok.com/"):
            return "unknown"
        try:
            r = self.http.call("GET", TT_OEMBED, None, params={"url": share_url})
        except Exception:  # noqa: BLE001
            return "unknown"
        if r.status == 200 and isinstance(r.json(), dict):
            return "live"
        if r.status in (400, 404):
            return "gone"
        return "unknown"


ADAPTERS = {"youtube": YouTubeAdapter, "tiktok": TikTokAdapter, "instagram": InstagramAdapter, "x": XAdapter}
