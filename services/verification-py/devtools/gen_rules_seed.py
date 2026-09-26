"""
Generate seed/vi_rules_seed.json from the V&I spec §B.6 rule table (rev 1, Sep 26, 2026).

The seed is DATA; its SHA-256 is pinned in config.PINNED_SEED_SHA256 and in ADR 0007, and the
service refuses to start on any other file (unless VI_ALLOW_UNPINNED_SEED=1, non-production).
Source URLs come only from the spec / the research notes it cites. Status ``unverified`` marks a
rule whose basis is UNVERIFIED in the research or that Andre must still rule on (VI-22); an
unverified rule never supports a positive ruling (RULE_NOT_IN_FORCE), it still supports negatives.

usage: python3 devtools/gen_rules_seed.py   (writes seed/vi_rules_seed.json and prints its SHA-256)
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

CR = "https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/terms"
VYRO = "https://vyro.com/clipper-terms"
VYRO_CONTENT = "https://vyro.com/content-requirements"
YT_POL = "https://developers.google.com/youtube/terms/developer-policies"
YT_2991785 = "https://support.google.com/youtube/answer/2991785"
YT_3399767 = "https://support.google.com/youtube/answer/3399767"
TT_CG = "https://www.tiktok.com/community-guidelines/en/integrity-authenticity"
SNAP = "https://www.snap.com/terms/spotlight-terms"
TX = "https://github.com/facebook/ThreatExchange/blob/main/README.md"
WHOP_TOS = "https://whop.com/content-rewards-terms-of-service/"
MRC = "https://mediaratingcouncil.org/sites/default/files/Standards/IVT%20Addendum%20Update%20062520.pdf"
OFCOM = ("https://www.ofcom.org.uk/siteassets/resources/documents/consultations/category-1-10-weeks/"
         "statement-age-assurance-and-childrens-access/part-3-guidance-on-highly-effective-age-assurance.pdf?v=395680")
COPPA = "https://www.federalregister.gov/documents/2025/04/22/2025-05904/childrens-online-privacy-protection-rule"
TT_VIDEO = "https://developers.tiktok.com/doc/tiktok-api-v2-video-object"
IG_INS = "https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights/"
IG_OVERVIEW = "https://developers.facebook.com/docs/instagram-platform/insights/"
X_DICT = "https://docs.x.com/x-api/fundamentals/data-dictionary"
SNAP_KIT = "https://developers.snap.com/snap-kit/creative-kit/overview"
TWITCH = "https://dev.twitch.tv/docs/api/videos/"


def row(rule_id, title, statement, kind, source_urls=(), basis=(), status="verified", parameters=None):
    return {"rule_id": rule_id, "title": title, "statement": statement, "source_urls": list(source_urls),
            "basis_obligation_ids": list(basis), "kind": kind, "parameters": parameters or {}, "status": status}


ROWS = [
    row("VI-00", "No ruling without an Andre-approved rule version",
        "Until Andre approves a V&I rule version, every ruling is negative with RULES_NOT_IN_FORCE.", "founder"),
    row("VI-01", "Official-API counts through the clipper's own OAuth connection only",
        "Counts come only from official platform APIs through an account the clipper connected with OAuth; no other "
        "count source is accepted.", "lead_default", [CR, VYRO]),
    row("VI-02", "No scraping, no download of posted media",
        "V&I never scrapes platform pages and never downloads posted media.", "lead_default"),
    row("VI-03", "Certified count is the platform-returned value at settlement",
        "The certified count is the platform-returned value fetched in the settlement window, never substituted, "
        "smoothed, interpolated or computed.", "research", [YT_POL]),
    row("VI-04", "Settle at create_time + max(HR-13 lag, rulebook minimum live days)",
        "A clip settles at the platform create time plus the larger of Compliance HR-13 settlement_lag_days and "
        "the rulebook's minimum live days; the lag is read from Compliance.", "lead_default", [CR, YT_2991785],
        ["HR-13"]),
    row("VI-05", "Downward platform revision before revision_watch_end: revision and clawback record",
        "Any downward platform revision of a certified count before revision_watch_end records a revision and a "
        "clawback record (counts only); Finance decides recovery.", "lead_default", [YT_2991785, TT_CG, SNAP],
        ["HR-13", "CQ-11"]),
    row("VI-06", "Live every day through the rulebook's minimum live period",
        "The clip must be evidenced live on every day from posting through the rulebook minimum live period.",
        "founder", [], ["HR-13", "CQ-11"]),
    row("VI-07", "Posted clip is the approved clip",
        "Same video id, same author and the per-platform fingerprint taken at approval.", "research", [TX]),
    row("VI-08", "Caption unchanged since approval",
        "The caption/description hash must equal the one taken at approval (disclosure lives in the caption).",
        "spec_choice", [WHOP_TOS]),
    row("VI-09", "Anomaly screen may hold; never decides pay",
        "The engagement anomaly screen may open a hold for a human; it never decides payment and never releases "
        "its own hold.", "research", [MRC]),
    row("VI-10", "Bought or botted engagement: S3 strike, clip voided",
        "Upheld bought/botted engagement or platform stripping at or above the threshold is an S3 strike and voids "
        "the clip.", "research", [CR, VYRO], ["US-FTC-465-08"]),
    row("VI-11", "18+ via neutral DOB plus one highly effective method; no guardian path",
        "Clippers are 18+: neutral DOB field plus one highly effective age-assurance method; self-declaration "
        "never passes; there is no guardian path.", "founder", [OFCOM, COPPA], ["HR-02", "AGE-01"]),
    row("VI-12", "One identity: one email, one payout identity",
        "A clipper has exactly one identity: one email and one payout identity.", "lead_default", [VYRO]),
    row("VI-13", "A social account belongs to exactly one identity",
        "A connected social account belongs to exactly one clipper identity; sharing or renting is forbidden.",
        "lead_default", [VYRO, CR]),
    row("VI-14", "Match to another clipper's clip or a campaign seed: hold to a human",
        "A submitted clip matching another clipper's clip or a campaign seed clip is held for a human decision.",
        "research", [VYRO_CONTENT]),
    row("VI-15", "Platform-data retention per platform; YouTube derived and aggregated use off",
        "Platform data is kept only as §B.5 allows (VI-15a..e); YouTube derived signals and cross-clipper "
        "aggregation stay off pending counsel question VI-CQ-01.", "research", [YT_POL]),
    row("VI-15a", "YouTube statistics and Analytics data: kept with the certification record",
        "YouTube statistics and YouTube Analytics API data may be stored as long as the certification record is "
        "kept (VI_EVIDENCE_RETENTION_DAYS).", "research", [YT_POL]),
    row("VI-15b", "YouTube other Authorized Data: 30-day refresh-or-delete; revoked connection purged in 24 h",
        "Other YouTube Authorized Data is refreshed or deleted within 30 calendar days of fetch, kept only as "
        "SHA-256 after revision_watch_end, and deleted within 24 hours of a revoked connection.", "research",
        [YT_POL, VYRO]),
    row("VI-15c", "TikTok: counts kept; ids 30-day refresh-or-delete; cover image never stored",
        "TikTok counts kept as YouTube statistics; id, create_time, share_url, duration and description 30-day "
        "refresh-or-delete; cover_image_url never stored. Retention terms UNVERIFIED (VI-CQ-02).", "research",
        [TT_VIDEO], status="unverified"),
    row("VI-15d", "Instagram: insights kept; media id, permalink and caption 30-day refresh-or-delete",
        "Instagram media insights kept as YouTube statistics; media id, permalink and caption 30-day "
        "refresh-or-delete. Retention terms UNVERIFIED (VI-CQ-02).", "research", [IG_INS], status="unverified"),
    row("VI-15e", "X: metric values kept as statistics",
        "X public/non-public metric values kept as YouTube statistics. Retention terms UNVERIFIED (VI-CQ-02).",
        "research", [X_DICT], status="unverified"),
    row("VI-16", "Tokens only in the vault; never output",
        "OAuth tokens, refresh tokens, authorization codes, client secrets and PKCE verifiers exist only inside "
        "the vault port and never appear in any output.", "lead_default"),
    row("VI-17", "Not connected: not payable",
        "A clipper who has not connected the posting account through OAuth cannot be certified.", "lead_default",
        [VYRO]),
    row("VI-18", "Snapchat and Twitch not payable; X only when enabled",
        "Snapchat and Twitch are not payable; X is payable only when VI_PLATFORM_X_ENABLED=1; Instagram requires a "
        "professional account with at least 100 followers.", "lead_default", [SNAP_KIT, TWITCH, IG_OVERVIEW]),
    row("VI-19", "Instagram Collab co-posts not certifiable unless the campaign permits",
        "An Instagram Collab co-post (or a post whose collab status is unknown) is not certifiable unless the "
        "campaign permits collabs.", "research", [CR]),
    row("VI-20", "Only registered submissions whose facts match are attested",
        "V&I attests only submissions registered by Creative whose post_ref, platform, posted_at and ids match the "
        "registration.", "spec_choice"),
    row("VI-21", "A cited Compliance row or a dependency not in force or unavailable: negative",
        "When a Compliance row a V&I rule cites is not verified/in force, or a dependency is unavailable or a "
        "stand-in, the ruling is negative.", "founder", [], ["HR-03"]),
    row("VI-22", "copyright_strike answered false only on the §C.4.3 basis",
        "copyright_strike is false only when the clip was live every day to min_live_end, Legal 37's takedown "
        "intake answered with none, and no rights restriction came back; Andre must rule whether this basis "
        "suffices (until then this rule is unverified and the answer is true).", "spec_choice", status="unverified"),
]


def main() -> None:
    out = Path(__file__).resolve().parents[1] / "seed" / "vi_rules_seed.json"
    doc = {"spec": "Verification and Integrity locked build spec rev 1 (Sep 26, 2026) section B.6",
           "generated_by": "devtools/gen_rules_seed.py", "rows": ROWS}
    data = (json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
    out.write_bytes(data)
    print(out, hashlib.sha256(data).hexdigest(), len(ROWS), "rows")


if __name__ == "__main__":
    main()
