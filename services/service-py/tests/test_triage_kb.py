"""Intelligences I1 (triage) and I2 (approved-answer matcher): unit rules, then through the API."""

from __future__ import annotations

import pytest

import kb
import triage
from helpers import rid


# --------------------------------------------------------------------------------------------------- I1 triage

@pytest.mark.parametrize("text,cat", [
    ("I want a refund", "money"),
    ("Why was I charged twice this month?", "money"),
    ("Can you resend the invoice", "money"),
    ("I am filing a chargeback with my bank", "money"),
    ("what's your price for TikTok Shop management", "money"),
    ("I want to cancel my agreement", "contract"),
    ("My lawyer will be in touch", "contract"),
    ("we will sue you", "contract"),
    ("this clip infringes my copyright, DMCA notice attached", "contract"),
    ("please read the terms again", "contract"),
    ("I think my account was hacked", "security"),
    ("I got a phishing email pretending to be you", "security"),
    ("please reset my password", "security"),
    ("Please delete my data", "privacy"),
    ("Under the CCPA I request a copy of my data", "privacy"),
    ("I want to make a complaint", "complaint"),
    ("this is ridiculous", "complaint"),
])
def test_categories(text, cat):
    t = triage.classify(text)
    assert t.primary == cat and cat in t.categories and not t.routine_candidate


@pytest.mark.parametrize("text", ["what the f**k is going on", "this is bullsh!t", "WTF", "shit service",
                                  "you $h1t company"])
def test_profanity_is_a_complaint(text):
    t = triage.classify(text)
    assert "complaint" in t.categories and "complaint:profanity" in t.signals


def test_all_caps_is_a_complaint_but_short_caps_is_not():
    assert "complaint:caps" in triage.classify("WHY HAS NOBODY CALLED ME BACK YET").signals
    assert triage.classify("OK THX").routine_candidate          # fewer than 12 letters: not shouting


def test_repeated_contacts_and_reopens_are_a_complaint():
    assert "complaint:repeat_contacts" in triage.classify("what time do you open", recent_inbound_24h=2).signals
    assert "complaint:reopened" in triage.classify("what time do you open", reopened=2).signals
    assert triage.classify("what time do you open", recent_inbound_24h=1).routine_candidate


def test_precedence_and_every_category_routes():
    t = triage.classify("Refund me or my lawyer will sue, and delete my data")
    assert t.primary == "privacy"
    assert t.categories == ("privacy", "contract", "money")
    routes = triage.routes_for(t)
    assert set(routes) == {"compliance_38", "legal_37", "andre", "finance_31"}


def test_negation_still_escalates_fail_toward_a_human():
    assert triage.classify("this is not about a refund").primary == "money"


def test_long_or_many_question_messages_are_other_not_routine():
    assert triage.classify("hours? open? weekend? holidays?").primary == "other"
    assert triage.classify("tell me about your hours " * 60).primary == "other"
    assert triage.classify("   ").primary == "other"


def test_legal_kinds():
    assert triage.legal_kind(triage.classify("my attorney says")) == "litigation_threat"
    assert triage.legal_kind(triage.classify("DMCA takedown please")) == "ip_claim"
    assert triage.legal_kind(triage.classify("cancel the contract")) == "contract_dispute"
    assert triage.legal_kind(triage.classify("delete my data")) == "privacy_request"


def test_signals_are_codes_not_text():
    t = triage.classify("I want a refund for order 12345 from jane@example.com")
    assert all(":" in s and "@" not in s and "12345" not in s for s in t.signals)


# --------------------------------------------------------------------------------------------------- I2 matcher

def _art(article_id="a1", rules=None, approved=True, **over):
    content = {"brands": ["zbm"], "channels": ["chat"], "title": "t", "answer": "the approved answer",
               "rules": rules or {"any": ["hours"], "min_any": 1, "all": [], "phrases": [], "exclude": []},
               "vocabulary": []}
    content.update(over)
    sha = kb.content_sha(content)
    return {"article_id": article_id, "status": "active", "version": 1, "content_sha256": sha,
            "approved": {"version": 1, "content_sha256": sha} if approved else None, **content}


def test_matcher_rules():
    norm = triage.normalise
    r = {"all": ["opening", "hours"], "any": [], "min_any": 1, "phrases": [], "exclude": ["holiday"]}
    assert kb.score(r, norm("what are your opening hours")) == 20
    assert kb.score(r, norm("opening hours on a holiday")) == 0
    assert kb.score(r, norm("hours")) == 0
    assert kb.score({"phrases": ["office hours"]}, norm("Office hours?")) == 1002
    assert kb.score({"any": ["a1x", "b1x"], "min_any": 2}, norm("a1x only")) == 0
    assert kb.score({}, norm("anything")) == 0


def test_matcher_unapproved_tampered_brand_channel_ambiguous():
    assert kb.match([_art(approved=False)], "hours?", "zbm", "chat") == (None, "no_match")
    t = _art()
    t["answer"] = "edited behind the approval"          # content no longer matches the approved hash
    assert kb.match([t], "hours?", "zbm", "chat") == (None, "no_match")
    assert kb.match([_art()], "hours?", "zbc", "chat")[0] is None
    assert kb.match([_art()], "hours?", "zbm", "email")[0] is None
    a, why = kb.match([_art("a1"), _art("a2")], "hours?", "zbm", "chat")
    assert a is None and why == "ambiguous"
    a, why = kb.match([_art("a1"), _art("a2", rules={"phrases": ["your hours"]})], "your hours?", "zbm", "chat")
    assert a["article_id"] == "a2" and why == "matched"


# --------------------------------------------------------------------------------------------------- through the API

def test_routine_question_answered_right_away_with_the_approved_text_only(h):
    art = h.article()
    r = h.ok(h.chat("Hi! what are your opening hours?"), 201)
    assert r["action"] == "answered" and r["answer"]["text"] == "We are open Monday to Friday, 9am to 6pm Pacific."
    t = h.ok(h.get(f"/svc/v1/tickets/{r['ticket_id']}"))
    out = [m for m in t["messages"] if m["dir"] == "out"][0]
    assert out["origin"] == "kb" and out["ref"]["version"] == art["version"] and out["status"] == "sent"
    assert t["status"] == "pending_customer" and t["first_response_at"] is not None
    assert h.ledger.of_type("message_sent")                       # the answer is on the ledger


def test_an_unapproved_article_is_never_used(h):
    h.article(approve=False)
    r = h.ok(h.chat("what are your hours"), 201)
    assert r["action"] == "queued_for_human" and "answer" not in r


def test_editing_an_article_unapproves_it(h):
    h.article()
    h.ok(h.post("/svc/v1/kb/articles", {"request_id": rid(), "item_id": "hours", "brands": ["zbm", "zbc"],
                                        "channels": ["chat"], "title": "Opening hours",
                                        "answer": "We are open 24/7!", "rules": {"any": ["hours"]}}), 201)
    r = h.ok(h.chat("what are your hours"), 201)
    assert r["action"] == "queued_for_human"
    arts = h.ok(h.get("/svc/v1/kb/articles"))
    assert arts[0]["version"] == 2 and arts[0]["usable"] is False


def test_approval_must_name_the_exact_version_and_content(h):
    saved = h.article(approve=False)
    r = h.post("/svc/v1/kb/articles/hours/approve", {"request_id": rid(), "version": saved["version"],
                                                     "content_sha256": "0" * 64}, andre=True)
    assert r.status_code == 409 and r.json()["detail"] == "APPROVAL_STALE"
    r = h.post("/svc/v1/kb/articles/hours/approve", {"request_id": rid(), "version": saved["version"],
                                                     "content_sha256": saved["content_sha256"]})
    assert r.status_code == 403 and r.json()["detail"] == "ANDRE_APPROVAL_REQUIRED"   # dashboard alone is not Andre
    assert not h.ledger.of_type("approval_recorded")


def test_a_retired_article_is_never_used(h):
    h.article()
    h.ok(h.post("/svc/v1/kb/articles/hours/retire", {"request_id": rid()}, andre=True))
    assert h.ok(h.chat("what are your hours"), 201)["action"] == "queued_for_human"


def test_a_tampered_article_in_memory_is_never_used(h):
    h.article()
    h.svc.catalog["kb"]["hours"]["answer"] = "Send your card number to us"
    r = h.ok(h.chat("what are your hours"), 201)
    assert r["action"] == "queued_for_human"


def test_ambiguous_match_goes_to_a_human(h):
    h.article("hours")
    h.article("open-days", answer="Weekdays.")
    assert h.ok(h.chat("when are you open"), 201)["action"] == "queued_for_human"


def test_no_match_goes_to_a_human_and_never_generates_text(h):
    h.article()
    r = h.ok(h.chat("do you work with dentists in Ohio"), 201)
    assert r["action"] == "queued_for_human" and "answer" not in r
    t = h.ok(h.get(f"/svc/v1/tickets/{r['ticket_id']}"))
    assert t["queue"] == "andre" and not [m for m in t["messages"] if m["dir"] == "out"]


def test_article_brand_scope(h):
    h.article(brands=("zbc",))
    assert h.ok(h.chat("what are your hours", brand="zbm"), 201)["action"] == "queued_for_human"
    assert h.ok(h.chat("what are your hours", brand="zbc", ref="client:clips"), 201)["action"] == "answered"
