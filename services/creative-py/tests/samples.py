"""Sample inputs shared by the tests (plain dicts, so they double as API bodies)."""

from __future__ import annotations

import copy
from datetime import date, timedelta

from conftest import NOW

TODAY = NOW.date()


# --- ZBM -----------------------------------------------------------------------------

def zbm_requirements(**over) -> dict:
    req = {
        "client_id": "client_acme",
        "objective": "Drive trial sign-ups for Acme oat milk among home baristas.",
        "audience": "Home coffee drinkers, 25-40, who make milk drinks at home.",
        "key_message": "Acme oat milk froths like dairy.",
        "deliverables": [{"deliverable_id": "d1", "platform": "youtube", "placement": "shorts",
                          "length_seconds": 30, "aspect_ratio": "9:16", "format": "mp4", "count": 1}],
        "mandatories": ["Acme logo end card"],
        "approvers": ["creative lead", "andre"],
        "distribution": ["YouTube Shorts paid placement"],
        "deadline": str(TODAY + timedelta(days=21)),
        "disclosure_requirements": ["Paid partnership"],
        "hook": "Watch this froth.",
        "success_in_numbers": [{"metric": "trial_signups", "comparator": ">=", "target": 500,
                                "unit": "signups", "measured_by": "client CRM export"}],
        "insight_candidates": [
            {"statement": "Home baristas quit oat milk because it won't foam.",
             "evidence": [{"source_id": "survey_2026_q2", "kind": "survey", "ref": "Q14 open answers"},
                          {"source_id": "interviews_aug", "kind": "interview", "ref": "8 of 12 interviews"}]},
            {"statement": "People like oat milk.",
             "evidence": [{"source_id": "blog_post", "kind": "research", "ref": "a blog"}]},
        ],
        "tone_of_voice": "Warm, dry wit.",
        "rights_and_permissions": [{"asset_id": "footage_kitchen_01", "asset_kind": "footage", "use": "paid_advertising"}],
        "transformation_plan": "Original footage shot for the brand; nothing reused.",
    }
    req.update(over)
    return req


def zbm_clearance(**over) -> dict:
    rec = {"record_id": "clr_kitchen_01", "asset_id": "footage_kitchen_01", "asset_kind": "footage",
           "rights_holder": "Acme Foods Ltd", "permitted_uses": ["paid_advertising", "organic_social"],
           "valid_from": "2026-01-01", "valid_until": "2027-01-01", "contract_ref": "contract://acme/msa-2026"}
    rec.update(over)
    return rec


def zbm_work(**over) -> dict:
    w = {
        "deliverable_id": "d1", "variant_index": 0,
        "declared": {"platform": "youtube", "placement": "shorts", "length_seconds": 30.0,
                     "aspect_ratio": "9:16", "format": "mp4", "codec": "h264", "file_ref": "render_d1_v1"},
        "asset_ids": ["footage_kitchen_01"],
        "uses_ai_generative_fill": False,
        "quality": {"opening_text": "Watch this froth.", "hook_ends_at_seconds": 1.5,
                    "script_text": "Watch this froth. Acme oat milk froths like dairy. Try it free.",
                    "supers": ["Acme logo end card"], "disclosure_text": "Paid partnership"},
    }
    w.update(over)
    return w


# --- ZBC -----------------------------------------------------------------------------

CAMPAIGN = "camp_pod_01"


def zbc_goal(**over) -> dict:
    g = {
        "campaign_id": CAMPAIGN,
        "client_id": "client_pod",
        "vertical": "podcasts",
        "objective": "Grow Pod Plus listeners with clips of episode 42.",
        "source_asset_ids": ["src_ep42"],
        "cleared_asset_ids": ["music_brand_sting"],
        "angles": [
            {"name": "Money myths", "description": "Debunk one budgeting myth per clip.",
             "keywords": ["budget", "myth"], "hook_lines": ["This budget myth costs you", "Stop believing this"]},
            {"name": "Founder story", "description": "The garage-to-studio story.",
             "keywords": ["garage", "started"], "hook_lines": ["He started in a garage"]},
        ],
        "must_say": ["Listen on Pod Plus"],
        "never_say": ["guaranteed returns"],
        "disclosure_any_of": ["#ad", "paid partnership"],
        "platforms": [{"platform": "youtube", "placement": "shorts"}, {"platform": "instagram", "placement": "reels"}],
        "min_days_live": 14,
    }
    g.update(over)
    return g


def zbc_license(**over) -> dict:
    lic = {"license_id": "lic_pod_01", "campaign_id": CAMPAIGN, "licensor": "Pod Plus Media",
           "licensee": "Z Best Clips", "covered_asset_ids": ["src_ep42"], "sublicense_to_clippers": True,
           "valid_from": "2026-09-01", "valid_until": "2027-03-01", "contract_ref": "contract://podplus/license-01"}
    lic.update(over)
    return lic


def zbc_music_clearance(**over) -> dict:
    rec = {"record_id": "clr_sting", "asset_id": "music_brand_sting", "asset_kind": "music",
           "rights_holder": "Pod Plus Media", "permitted_uses": ["sublicense_to_clippers", "organic_social"],
           "valid_from": "2026-09-01", "valid_until": "2027-03-01", "contract_ref": "contract://podplus/music-01"}
    rec.update(over)
    return rec


ZBC_ASSETS = [{"asset_id": "src_ep42", "kind": "footage"}, {"asset_id": "music_brand_sting", "kind": "music"}]


def zbc_source() -> dict:
    return {
        "source_asset_id": "src_ep42",
        "duration_seconds": 3600,
        "segments": [
            {"segment_id": "s1", "start_seconds": 10, "end_seconds": 40,
             "transcript": "This budget myth costs you money every month. Listen on Pod Plus."},
            {"segment_id": "s2", "start_seconds": 100, "end_seconds": 160,
             "transcript": "He started in a garage with two microphones."},
            {"segment_id": "s3", "start_seconds": 200, "end_seconds": 230,
             "transcript": "The biggest budget myth is that apps fix it."},
            {"segment_id": "s4", "start_seconds": 300, "end_seconds": 330,
             "transcript": "Another myth: you need a budget spreadsheet."},
            {"segment_id": "s5", "start_seconds": 400, "end_seconds": 402, "transcript": "budget"},
            {"segment_id": "s6", "start_seconds": 150, "end_seconds": 170, "transcript": "garage again"},
            {"segment_id": "s7", "start_seconds": 500, "end_seconds": 900, "transcript": "a long budget rant"},
            {"segment_id": "s8", "start_seconds": 1000, "end_seconds": 1030, "transcript": "the weather was nice"},
            {"segment_id": "s9", "start_seconds": 3590, "end_seconds": 3700, "transcript": "budget outro"},
        ],
    }


def zbc_kit_request(**over) -> dict:
    k = {"seed_count": 3, "caption_styles": ["Bold white captions, disclosure on line 1"],
         "overlays": ["lower-third episode tag"], "templates": ["9x16 split-screen reaction"],
         "brand_asset_ids": ["music_brand_sting"], "do_examples": ["Open on the punchline"],
         "dont_examples": ["Don't crop the host's face"]}
    k.update(over)
    return k


def zbc_clip(submission_id: str = "clip_001", **over) -> dict:
    c = {
        "submission_id": submission_id, "campaign_id": CAMPAIGN, "rulebook_version": 1,
        "clipper_id": "clipper_77", "posted_at": NOW.isoformat(),
        "platform": "youtube", "placement": "shorts", "post_ref": "https://youtube.com/shorts/example77",
        "length_seconds": 45, "resolution_height_px": 1080, "angle_id": "A01", "moment_ids": ["m-s1"],
        "caption": "The budget myth nobody talks about #ad",
        "on_screen_text": "Myth #1", "transcript": "This budget myth costs you. Listen on Pod Plus.",
        "account_bio": "Clips daily", "transformation_elements": ["original_commentary", "captions_added"],
        "is_raw_repost": False, "has_third_party_watermark": False, "paid_partnership_label": False,
        "source_asset_ids": ["src_ep42"], "added_asset_ids": ["music_brand_sting"],
    }
    c.update(over)
    return c


def deep(d: dict) -> dict:
    return copy.deepcopy(d)


def future(days: int) -> date:
    return TODAY + timedelta(days=days)
