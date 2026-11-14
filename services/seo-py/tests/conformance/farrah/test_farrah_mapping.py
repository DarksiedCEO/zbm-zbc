"""farrah-spec/1 section 2: channels and mapping (B-FA-10..13) and A-FA-04 (attribution gaming)."""

from __future__ import annotations

import random
from datetime import date

import pytest

from agents import farrah
from conformance.gen.attribution import arow, crow, dataset, freshness
from conformance.gen.farrah_util import ok_run, rid, set_map, set_plan

D = date(2026, 9, 10)


def test_B_FA_10_channels_closed_set():
    assert farrah.CHANNELS == ("organic_search", "ai_referral", "other_known", "unknown")
    assert farrah.ATTRIBUTED == ("organic_search", "ai_referral")
    assert farrah.LABELS == ("HIGH", "MEDIUM", "LOW", "INSUFFICIENT_EVIDENCE")


def test_B_FA_11_ai_referrer_list_versioned_and_not_complete():
    assert farrah.AI_REFERRERS == ("chatgpt.com", "chat.openai.com", "perplexity.ai", "gemini.google.com",
                                   "copilot.microsoft.com", "claude.ai")
    assert farrah.AI_REFERRERS_VERSION == "farrah-ai-referrers/2026-11-13.1" and farrah.AI_REFERRERS_COMPLETE is False


# one row per entry of the B-FA-11 mapping table: (source, medium, channel group, landing page) -> channel
ANALYTICS_TABLE = [
    (("google", "organic", "Organic Search", "/"), "organic_search"),
    (("bing", "organic", "Organic Search", "/"), "organic_search"),
    (("anything", "referral", "Organic Search", "/"), "organic_search"),          # the source's organic group
    (("chatgpt.com", "organic", "Organic Search", "/"), "organic_search"),        # organic medium first
    (("chatgpt.com", "referral", "Referral", "/"), "ai_referral"),
    (("chat.openai.com", "referral", "Referral", "/"), "ai_referral"),
    (("perplexity.ai", "referral", "Referral", "/"), "ai_referral"),
    (("gemini.google.com", "referral", "Referral", "/"), "ai_referral"),
    (("copilot.microsoft.com", "referral", "Referral", "/"), "ai_referral"),
    (("claude.ai", "referral", "Referral", "/"), "ai_referral"),
    (("(not set)", "(not set)", "Unassigned", "/"), "unknown"),
    (("google", "(not set)", "Paid Search", "/"), "unknown"),
    (("partner.example", "referral", "Unassigned", "/"), "unknown"),
    (("(direct)", "(none)", "Direct", "(not set)"), "unknown"),                   # direct with no landing data
    (("(direct)", "(none)", "Direct", ""), "unknown"),
    (("(direct)", "(none)", "Direct", None), "unknown"),
    (("(direct)", "(none)", "Direct", "/pricing"), "other_known"),                # direct WITH landing data
    (("facebook.com", "cpc", "Paid Social", "/"), "other_known"),
    (("newsletter", "email", "Email", "/"), "other_known"),
    (("(other)", "(other)", "(other)", "(other)"), "other_known"),
]


@pytest.mark.parametrize("dims,channel", ANALYTICS_TABLE)
def test_B_FA_11_analytics_mapping_table_one_row_per_entry(dims, channel):
    src, med, grp, landing = dims
    assert farrah.analytics_channel(arow(D, "1.00", src, med, grp, landing)) == channel


MAP = {"organic_search": ["Google Search", "Organic"], "ai_referral": ["ChatGPT"], "other_known": ["Trade show"]}
CRM_TABLE = [
    ({"utm_medium": "organic", "utm_source": "google"}, "organic_search"),
    ({"utm_medium": "organic", "lead_source": "Trade show"}, "organic_search"),     # utm first
    ({"utm_medium": "referral", "utm_source": "chatgpt.com"}, "ai_referral"),
    ({"utm_source": "perplexity.ai"}, "ai_referral"),
    ({"utm_medium": "cpc", "utm_source": "google", "lead_source": "Organic"}, "other_known"),
    ({"lead_source": "Google Search"}, "organic_search"),
    ({"lead_source": "  organic "}, "organic_search"),                              # case / whitespace only
    ({"lead_source": "ChatGPT"}, "ai_referral"),
    ({"lead_source": "Trade show"}, "other_known"),
    ({"lead_source": "Walk in"}, "unknown"),                                        # outside the mapping
    ({"lead_source": "Googl Search"}, "unknown"),                                   # never guessed
    ({}, "unknown"),
]


@pytest.mark.parametrize("fields,channel", CRM_TABLE)
def test_B_FA_12_crm_mapping(fields, channel):
    row = crow(D, "1.00", **fields)
    assert farrah.crm_channel(row, farrah.normalised_map(MAP)) == channel


def test_B_FA_12_a_value_declared_twice_maps_to_neither():
    m = farrah.normalised_map({"organic_search": ["X"], "ai_referral": ["x "], "other_known": []})
    assert farrah.crm_channel(crow(D, "1.00", lead_source="X"), m) == "unknown"


def test_B_FA_12_channel_map_route_andre_only_and_used_by_runs(fa):
    set_plan(fa, "acme", "managed_3", "crm")
    assert set_map(fa, "acme", MAP, andre=False).status_code == 403
    assert set_map(fa, "acme", MAP, caller="seo_agent", andre=False).status_code == 403
    assert set_map(fa, "acme", MAP).status_code == 200
    rows = [crow(D, "100.00", lead_source="Google Search", ref=f"r{i}") for i in range(12)]
    rows += [crow(D, "7.00", lead_source="Walk in", ref="w")]
    fa.store.add(fa.svc, dataset("acme", "crm", rows))
    fa.store.tenant("acme")["freshness"] = freshness("acme", {"crm": "FRESH"})
    v = ok_run(fa)
    org = v["result"]["channels"]["organic_search"]
    assert (org["figure"], org["upper"], org["rows"]) == ("1200", "1207", 12)
    t = fa.ok(fa.get("/tenants/acme", caller="compliance_38"))
    assert "channel_map" not in t and t["channel_map_counts"] == {"organic_search": 2, "ai_referral": 1,
                                                                  "other_known": 1}


def test_B_FA_13_revenue_basis_per_tenant(fa):
    rows_a = [arow(D, "50.00", landing=f"/{i}") for i in range(10)]
    rows_c = [crow(D, "900.00", utm_medium="organic", ref=f"c{i}") for i in range(10)]
    fa.store.add(fa.svc, dataset("acme", "analytics", rows_a))
    fa.store.add(fa.svc, dataset("acme", "crm", rows_c))
    fa.store.tenant("acme")["freshness"] = freshness("acme")
    v = ok_run(fa)
    assert v["basis"] == "analytics" and v["result"]["channels"]["organic_search"]["figure"] == "500"
    assert "SESSION_SCOPED_LAST_CLICK" in v["assumptions"]
    set_plan(fa, "acme", "managed_2", "crm")
    v = ok_run(fa)
    assert v["basis"] == "crm" and v["result"]["channels"]["organic_search"]["figure"] == "9000"
    assert v["supersedes"] is not None and "SESSION_SCOPED_LAST_CLICK" not in v["assumptions"]
    fa.tenant("newco")
    fa.svc.tenants["newco"]["plan"] = "managed_2"                  # a plan without a declared basis
    r = fa.post("/tenants/newco/attribution-runs", {"request_id": rid(), "month": "2026-09"}, caller="dashboard")
    fa.refused(r, 409, "REVENUE_BASIS_UNSET")


def test_B_FA_13_crm_basis_counts_won_deals_only(fa):
    set_plan(fa, "acme", "managed_2", "crm")
    rows = [crow(D, "100.00", utm_medium="organic", ref="w"), crow(D, "500.00", stage="open", utm_medium="organic"),
            crow(D, "700.00", stage="lost", utm_medium="organic")]
    fa.store.add(fa.svc, dataset("acme", "crm", rows))
    fa.store.tenant("acme")["freshness"] = freshness("acme", {"crm": "FRESH"})
    v = ok_run(fa)
    assert v["result"]["channels"]["organic_search"]["figure"] == "100"
    assert v["result"]["totals"]["basis_revenue"] == "100"


# ------------------------------------------------------------------------------------------------ A-FA-04

CONFUSABLE = {"o": "\u043e", "a": "\u0430", "i": "\u0456", "c": "\u0441"}      # Cyrillic lookalikes


def _game(rng):
    kind = rng.choice(("confusable", "spaced_case", "lookalike_host", "fullwidth", "zero_width", "suffix"))
    if kind == "confusable":
        w = "".join(CONFUSABLE.get(ch, ch) if rng.random() < 0.5 else ch for ch in "organic")
        w = w if w != "organic" else "\u043erganic"
        return kind, {"sessionMedium": w}, "other_known"
    if kind == "spaced_case":
        w = rng.choice(("Organic ", " ORGANIC", "\torganic\n", "OrGaNiC"))
        return kind, {"sessionMedium": w}, "organic_search"
    if kind == "lookalike_host":
        h = rng.choice(("chatgpt.com.evil.test", "chatgpt.co", "chat-gpt.com", "claude.ai.example", "xclaude.ai",
                        "perplexity.ai.attacker.test", "gemini.google.com.evil", "chatgpt\u2024com"))
        return kind, {"sessionSource": h, "sessionMedium": "referral"}, "other_known"
    if kind == "fullwidth":
        return kind, {"sessionMedium": "\uff4f\uff52\uff47\uff41\uff4e\uff49\uff43"}, "other_known"
    if kind == "zero_width":
        return kind, {"sessionMedium": "orga\u200bnic"}, "other_known"
    return kind, {"sessionSource": rng.choice(("CHATGPT.COM", " Claude.AI ")), "sessionMedium": "referral"}, \
        "ai_referral"


def _a_fa_04(n, seed):
    rng = random.Random(seed)
    for i in range(n):
        kind, fields, want = _game(rng)
        row = arow(D, "1.00", "somewhere.example", "referral", "Referral", "/x")
        row.update(fields)
        got = farrah.analytics_channel(row)
        assert got == want, (i, seed, kind)
        crm = crow(D, "1.00", utm_medium=fields.get("sessionMedium"), utm_source=fields.get("sessionSource"))
        assert farrah.crm_channel(crm, {}) == want, (i, seed, kind)


def test_A_FA_04_attribution_gaming_fast():
    _a_fa_04(200, 41)


@pytest.mark.lead_gate
def test_A_FA_04_attribution_gaming_lead():
    _a_fa_04(2000, 42)
