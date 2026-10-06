"""FTC Endorsement Guides, fail closed (ADR 0015 decision 14), and contracts through Legal (37) (decision 16)."""

from __future__ import annotations

import pytest

from helpers import FakeLegal, Harness, rid, wired_ports


# ------------------------------------------------------------------------------------------------ briefs

def test_a_brief_carries_its_disclosure_and_the_fixed_ftc_section(w):
    c = w.campaign()
    b = w.brief(c, approve=False)
    assert "16 CFR Part 255" in b["rendered"] and '"#ad"' in b["rendered"] and b["status"] == "draft"
    r = w.post("/briefs", {"request_id": rid(), "campaign_id": c["campaign_id"], "title": "t", "text": "x"},
               caller="influencer_agent")
    assert r.status_code == 422                                      # a brief without a disclosure does not exist
    r = w.post("/briefs", {"request_id": rid(), "campaign_id": c["campaign_id"], "title": "t", "text": "x",
                           "disclosure": "#collab"}, caller="influencer_agent")
    assert r.status_code == 422                                      # only the closed list


def test_the_other_brands_phrase_is_refused(w):
    c = w.campaign(brand="zbm")
    w.code(w.post("/briefs", {"request_id": rid(), "campaign_id": c["campaign_id"], "title": "t", "text": "x",
                              "disclosure": "Paid partnership with Z Best Clips"}, caller="influencer_agent"),
           422, "DISCLOSURE_NOT_ALLOWED")


def test_andre_approves_the_brief_as_issued(w):
    c = w.campaign()
    b = w.brief(c, approve=False)
    w.code(w.post(f"/briefs/{b['brief_id']}/approve", {"request_id": rid(), "content_sha256": b["content_sha256"]},
                  caller="influencer_agent", andre=True), 403, "CALLER_NOT_ALLOWED")
    w.svc.briefs[b["brief_id"]]["text"] += " Do not mention it is paid."
    w.code(w.post(f"/briefs/{b['brief_id']}/approve", {"request_id": rid(), "content_sha256": b["content_sha256"]},
                  andre=True), 409, "CONTENT_HASH_MISMATCH")


def test_hidden_characters_in_a_brief_are_refused(w):
    c = w.campaign()
    w.code(w.post("/briefs", {"request_id": rid(), "campaign_id": c["campaign_id"], "title": "t",
                              "text": "Say it​ loud", "disclosure": "#ad"}, caller="influencer_agent"),
           422, "CONTENT_HIDDEN_CHARACTERS")


# ------------------------------------------------------------------------------------------------ content

@pytest.mark.parametrize("caption,code", [
    ("Loving this tool from Z Best Media. #gaming", "DISCLOSURE_MISSING"),
    ("#adventure time with Z Best Media", "DISCLOSURE_MISSING"),
    ("#advert Loving it", "DISCLOSURE_MISSING"),
    ("ad Loving it", "DISCLOSURE_MISSING"),
    ("#аd Loving it", "DISCLOSURE_MISSING"),                     # Cyrillic a
    ("#a​d Loving it", "CONTENT_HIDDEN_CHARACTERS"),             # zero-width space
    ("#a‍d Loving it", "CONTENT_HIDDEN_CHARACTERS"),             # zero-width joiner between letters
    ("‮#ad Loving it", "CONTENT_HIDDEN_CHARACTERS"),             # right-to-left override
    ("Loving it #gaming #fun #ad", "DISCLOSURE_NOT_PROMINENT"),       # buried in a hashtag block
    ("x" * 99 + " #ad", "DISCLOSURE_NOT_PROMINENT"),                  # below the fold
    ("#gaming #ad loving it", "DISCLOSURE_NOT_PROMINENT"),
])
def test_content_without_a_clear_upfront_disclosure_is_refused(w, caption, code):
    inf, c, b, d = w.setup_deal()
    w.code(w.content(d, caption=caption), 422, code)
    assert not w.svc.contents


@pytest.mark.parametrize("caption", ["#ad Loving this. #gaming", "#AD: loving this", "(#ad) loving this",
                                     "Loving this #ad #gaming", "x" * 96 + " #ad",
                                     "\U0001F468‍\U0001F469‍\U0001F467 #ad family day"])
def test_clear_disclosures_pass(w, caption):
    inf, c, b, d = w.setup_deal()
    w.ok(w.content(d, caption=caption), 201)


def test_a_phrase_disclosure(w):
    inf = w.creator()
    c = w.campaign()
    b = w.brief(c, disclosure="Paid partnership with Z Best Media")
    d = w.ok(w.deal(inf, c, b), 201)
    w.code(w.content(d, caption="#ad loving it"), 422, "DISCLOSURE_MISSING")
    w.code(w.content(d, caption="Paid partnership with Z Best Medias loving it"), 422, "DISCLOSURE_MISSING")
    w.ok(w.content(d, caption="Paid partnership with Z Best Media: loving it"), 201)


@pytest.mark.parametrize("platform,label,ok", [("instagram", False, False), ("tiktok", False, False),
                                               ("youtube", False, False), ("x", False, True), ("instagram", True,
                                                                                               True)])
def test_the_platform_paid_partnership_label(w, platform, label, ok):
    inf, c, b, _ = w.setup_deal()
    d = w.ok(w.deal(inf, c, b, platform=platform, fee="10.00"), 201)
    r = w.content(d, platform=platform, label=label)
    assert (r.status_code == 201) is ok, r.text
    if not ok:
        assert r.json()["detail"] == "PLATFORM_LABEL_REQUIRED"


def test_content_must_match_the_deal(w):
    inf, c, b, d = w.setup_deal()
    w.code(w.content(d, platform="tiktok"), 422, "PLATFORM_NOT_IN_DEAL")
    big = w.ok(w.deal(inf, c, b, fee="9000.00"), 201)
    w.code(w.content(big), 409, "DEAL_NOT_APPROVED")


def test_andre_approves_final_content_by_hash_before_it_counts_live(w):
    inf, c, b, d = w.setup_deal()
    content = w.ok(w.content(d), 201)
    w.code(w.live(content), 409, "CONTENT_NOT_APPROVED")
    url = f"/contents/{content['content_id']}/approve"
    w.code(w.post(url, {"request_id": rid(), "content_sha256": content["content_sha256"]}, caller="influencer_agent",
                  andre=True), 403, "CALLER_NOT_ALLOWED")
    w.code(w.post(url, {"request_id": rid(), "content_sha256": "b" * 64}, andre=True), 409, "CONTENT_HASH_MISMATCH")
    w.approve_content(content)
    w.code(w.live(content), 409, "CONTRACT_NOT_IN_FORCE")
    w.contract(d)
    r = w.post(f"/contents/{content['content_id']}/live", {"request_id": rid(), "content_sha256": "c" * 64,
                                                           "post_ref": "p1"}, caller="influencer_agent")
    w.code(r, 409, "CONTENT_HASH_MISMATCH")
    assert w.ok(w.get(f"/campaigns/{c['campaign_id']}"))["live_content"] == 0
    w.ok(w.live(content))
    assert w.ok(w.get(f"/campaigns/{c['campaign_id']}"))["live_content"] == 1


def test_content_tampered_after_submission_is_never_approved(w):
    inf, c, b, d = w.setup_deal()
    content = w.ok(w.content(d), 201)
    w.svc.contents[content["content_id"]]["caption"] = "no disclosure here"
    w.code(w.post(f"/contents/{content['content_id']}/approve",
                  {"request_id": rid(), "content_sha256": content["content_sha256"]}, andre=True), 422,
           "DISCLOSURE_MISSING")


def test_andre_rejects_content(w):
    inf, c, b, d = w.setup_deal()
    content = w.ok(w.content(d), 201)
    w.ok(w.post(f"/contents/{content['content_id']}/reject", {"request_id": rid()}, andre=True))
    w.code(w.live(content), 409, "CONTENT_NOT_APPROVED")


# ------------------------------------------------------------------------------------------------ contracts

def test_legal_stand_in_refuses_and_nothing_is_recorded(h):
    inf, c, b, d = h.setup_deal()
    n = len(h.svc.log)
    h.code(h.post(f"/deals/{d['deal_id']}/contract", {"request_id": rid()}, caller="influencer_agent"), 503,
           "LEGAL_UNAVAILABLE")
    assert len(h.svc.log) == n and h.svc.deals[d["deal_id"]]["status"] == "approved"


def test_contract_flow_with_legal(w):
    inf, c, b, d = w.setup_deal()
    w.code(w.post(f"/deals/{d['deal_id']}/contract/confirm", {"request_id": rid()}, caller="influencer_agent"), 409,
           "CONTRACT_NOT_SENT")
    out = w.contract(d)
    assert out["status"] == "contracted"
    sent = w.ledger.of_type("contract_sent")[0]["_payload"]
    assert sent["content_sha256"] == d["content_sha256"] and "env-" not in str(sent)


def test_a_pending_deal_cannot_get_a_contract(w):
    inf = w.creator()
    c = w.campaign()
    b = w.brief(c)
    d = w.ok(w.deal(inf, c, b, fee="9000.00"), 201)
    w.code(w.post(f"/deals/{d['deal_id']}/contract", {"request_id": rid()}, caller="influencer_agent"), 409,
           "DEAL_NOT_APPROVED")


@pytest.mark.parametrize("send,status,code,http", [("refused", "in_force", "CONTRACT_REFUSED", 409),
                                                    ("sent", "not_in_force", "CONTRACT_NOT_IN_FORCE", 409),
                                                    ("sent", "unavailable", "LEGAL_UNAVAILABLE", 503)])
def test_legal_answers(tmp_path, send, status, code, http):
    h = Harness(tmp_path, ports=wired_ports(legal=FakeLegal(send=send, status=status)))
    inf, c, b, d = h.setup_deal()
    r = h.post(f"/deals/{d['deal_id']}/contract", {"request_id": rid()}, caller="influencer_agent")
    if send != "sent":
        h.code(r, http, code)
        return
    h.ok(r)
    h.code(h.post(f"/deals/{d['deal_id']}/contract/confirm", {"request_id": rid()}, caller="influencer_agent"),
           http, code)
    assert h.svc.deals[d["deal_id"]]["status"] == "contract_sent"


def test_a_contracted_deal_is_not_cancelled_here(w):
    inf, c, b, d = w.setup_deal()
    w.contract(d)
    w.code(w.post(f"/deals/{d['deal_id']}/cancel", {"request_id": rid()}, caller="influencer_agent"), 409,
           "DEAL_NOT_CANCELLABLE")
