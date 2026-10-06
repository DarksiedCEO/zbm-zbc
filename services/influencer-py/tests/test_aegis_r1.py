"""AEGIS round 1 on a58e633 (BLOCKING): the reviewer's reproducing probes, each turned into a regression that fails on
a58e633 and passes after the fix (ADR 0015 amendment, round 1)."""

from __future__ import annotations

import threading

import pytest

import textguard
from helpers import FakeFinance, Harness, rid, wired_ports


@pytest.fixture
def w(tmp_path):
    return Harness(tmp_path, ports=wired_ports())


def _complete(w, inf, fee):
    """A deal approved, contracted, its content live and paid (as the reviewer's probe)."""
    c = w.campaign()
    b = w.brief(c)
    d = w.ok(w.deal(inf, c, b, fee=fee), 201)
    if d["status"] == "approved":
        w.contract(d)
        ct = w.ok(w.content(d), 201)
        w.approve_content(ct)
        w.ok(w.live(ct))
        if not w.svc.influencers[inf["influencer_id"]]["payee"]["payee_ref"]:
            w.tax_confirmed(inf)
            w.ok(w.verify(inf))
        w.ok(w.payout(d, fee, [ct["content_id"]]), 201)
    return w.svc.deals[d["deal_id"]]


# ------------------------------------------------------------------------------------------------ H1

def test_h1_deals_run_one_after_another_still_reach_andre(w):
    inf = w.creator()
    first = _complete(w, inf, "5000.00")
    assert first["status"] == "completed" and first["needs_andre"] == []
    second = _complete(w, inf, "5000.00")
    assert second["status"] == "pending_andre"                   # a58e633: approved, then paid: $10,000, no Andre
    assert second["needs_andre"] == ["INFLUENCER_TOTAL_OVER_LIMIT"]
    third = _complete(w, inf, "1.00")
    assert third["status"] == "pending_andre"


def test_h1_a_window_is_not_built_the_total_is_lifetime():
    import config as config_mod
    from helpers import base_env
    with pytest.raises(RuntimeError, match="INF_DEAL_AGGREGATE_WINDOW_DAYS"):
        config_mod.load(base_env(INF_DEAL_AGGREGATE_WINDOW_DAYS="30"))


# ------------------------------------------------------------------------------------------------ M1 / M2

def test_m1_a_stranger_cannot_attach_a_handle_and_opt_the_creator_out(w):
    victim = w.creator(email="victim@example.test", handles=(("instagram", "victim"),))
    # the stranger submits the public form with the victim's address and the stranger's own TikTok handle
    app = w.ok(w.application("victim@example.test", handles=(("tiktok", "stranger"),)), 201)
    assert app["confirmation_status"] == "pending"
    assert [h["platform"] for h in w.svc.influencers[victim["influencer_id"]]["handles"]] == ["instagram"]
    # ... and "stop" from their own TikTok resolves to no one: a review entry, nothing suppressed
    r = w.ok(w.post("/replies", {"request_id": rid(), "channel": "tiktok", "from_handle": "stranger", "text": "stop"},
                    caller="provider_events"), 201)
    assert r["influencer_id"] is None
    v = w.ok(w.get(f"/influencers/{victim['influencer_id']}"))
    assert v["suppressed"] is False and v["held"] is False


def test_m1_the_confirmation_mail_goes_to_the_address_on_record_and_only_its_token_works(w):
    victim = w.creator(email="victim@example.test", handles=(("instagram", "victim"),))
    app = w.ok(w.application("victim@example.test", handles=(("tiktok", "stranger"),)), 201)
    w.ok(w.job("send-queue"))
    mid, to, msg = w.ports.email.sent[-1]
    assert to == "victim@example.test" and "/c/" in msg["body"] and "If it was not you" in msg["body"]
    token = w.svc.confirmation_token(app["confirmation_id"])
    w.code(w.post("/confirmations", {"request_id": rid(), "token": token[:-1] + ("0" if token[-1] != "0" else "1")},
                  caller="hub"), 404, "CONFIRMATION_UNKNOWN")
    w.code(w.post("/confirmations", {"request_id": rid(), "token": token}, caller="influencer_agent"), 403,
           "CALLER_NOT_ALLOWED")
    w.ok(w.confirm(app["confirmation_id"]))                       # the real owner clicked: the handle is theirs
    w.code(w.confirm(app["confirmation_id"]), 409, "CONFIRMATION_USED")
    assert {h["platform"] for h in w.svc.influencers[victim["influencer_id"]]["handles"]} == {"instagram", "tiktok"}


def test_m1_a_confirmation_expires(w):
    app = w.ok(w.application(), 201)
    w.clock.advance(days=8)
    w.code(w.confirm(app["confirmation_id"]), 409, "CONFIRMATION_EXPIRED")
    assert w.svc.influencers[app["influencer_id"]]["adult_attested"] is False


def test_m2_a_stranger_cannot_attest_for_a_prospect(w):
    p = w.prospect(email="found@example.test")
    w.ok(w.application("found@example.test", handles=()), 201)
    assert w.svc.influencers[p["influencer_id"]]["adult_attested"] is False   # a58e633: True at once


def test_m2_the_hub_cannot_replace_a_verified_creators_tax_reference(w):
    inf, d, ct = w.paid_ready()
    conf = w.ok(w.tax(inf, ref="stripe:acct_ATTACKERreference00"), 201)
    assert conf["status"] == "pending"
    assert w.svc.influencers[inf["influencer_id"]]["payee"]["status"] == "verified"   # nothing changed yet
    out = w.ok(w.confirm(conf["conf_id"]))
    assert out["status"] == "pending_andre"                       # verified payee: Andre approves the change by hash
    assert w.svc.influencers[inf["influencer_id"]]["tax"]["tax_ref"] != "stripe:acct_ATTACKERreference00"
    w.code(w.post(f"/confirmations/{conf['conf_id']}/approve", {"request_id": rid(), "content_sha256": "0" * 64},
                  andre=True), 409, "CONTENT_HASH_MISMATCH")
    w.ok(w.post(f"/confirmations/{conf['conf_id']}/reject", {"request_id": rid()}, andre=True))
    assert w.svc.influencers[inf["influencer_id"]]["payee"]["status"] == "verified"


def test_m2_finance_gets_the_confirmed_identity_to_match(w):
    inf, d, ct = w.paid_ready()
    identity = w.ports.finance.registered[-1][5]
    import hashlib
    assert identity == f"{inf['influencer_id']}:" + hashlib.sha256(b"creator@example.test").hexdigest()


def test_m2_no_tax_reference_before_the_address_is_confirmed(w):
    app = w.ok(w.application(), 201)                                # applied, not yet confirmed
    w.code(w.tax({"influencer_id": app["influencer_id"]}), 403, "AGE_ATTESTATION_REQUIRED")


# ------------------------------------------------------------------------------------------------ M3

@pytest.mark.parametrize("v", ["123​45​6789", "123/45/6789", "123,45,6789", "123    45    6789",
                               "123 ⁃ 45 ⁃ 6789", "12:3456789", "123*45*6789", "123́456789"])
def test_m3_tin_bypasses_in_free_text(v):
    assert textguard.problem({"display_name": f"SSN {v}"}) == "TAX_ID_REFUSED"


@pytest.mark.parametrize("ref", ["vault:ssn123456789", "stripe:acct_123456789abcdefghij",
                                 "stripe:acct_ATTACKER1", "vault:not-a-uuid-at-all"])
def test_m3_tax_ref_must_be_a_provider_reference_with_no_long_digit_run(w, ref):
    inf = w.creator()
    assert w.tax(inf, ref=ref).status_code == 422
    assert not w.svc.confirmations or all(c["kind"] != "tax_profile" for c in w.svc.confirmations.values())


def test_m3_a_tin_in_a_display_name_is_refused(w):
    r = w.post("/influencers", {"request_id": rid(), "display_name": "Jo 123    45    6789",
                                "handles": [{"platform": "x", "handle": "jo"}], "evidence_ref": "ev-1"})
    w.code(r, 422, "TAX_ID_REFUSED")


# ------------------------------------------------------------------------------------------------ M4

def test_m4_an_unknown_message_id_never_drops_an_opt_out(w):
    inf = w.creator()
    r = w.ok(w.post("/replies", {"request_id": rid(), "channel": "email", "message_id": "msg-unknown",
                                 "from_email": "creator@example.test", "text": "unsubscribe"},
                    caller="provider_events"), 201)
    assert r["ignored"] == ["message_id"] and r["suppressed"] is True
    assert w.ok(w.get(f"/influencers/{inf['influencer_id']}"))["suppressed"] is True


def test_m4_a_named_sender_never_drops_an_opt_out(w):
    inf = w.creator()
    w.ok(w.post("/replies", {"request_id": rid(), "channel": "email", "from_email": "Casey <creator@example.test>",
                             "text": "STOP"}, caller="provider_events"), 201)
    assert w.ok(w.get(f"/influencers/{inf['influencer_id']}"))["suppressed"] is True


@pytest.mark.parametrize("fields", [{"from_email": "not an address"}, {"from_handle": "@who"},
                                    {"message_id": "x" * 900}, {"from_email": "a@b", "from_handle": "!!"}, {}])
def test_m4_a_reply_that_resolves_nothing_is_still_recorded_for_andre(w, fields):
    r = w.ok(w.post("/replies", {"request_id": rid(), "channel": "email", "text": "stop", **fields},
                    caller="provider_events"), 201)
    assert r["held"] is True and r["influencer_id"] is None
    assert w.svc.holds[r["hold_id"]]["status"] == "active" and w.svc.replies[r["reply_id"]]
    assert w.ledger.of_type("outreach_hold_applied")[-1]["_payload"]["unresolved"] is True


# ------------------------------------------------------------------------------------------------ M5

def test_m5_two_records_one_tax_reference_are_one_person(w):
    a = w.creator(email="p1@example.test", handles=(("instagram", "pa"),))
    b = w.creator(email="p2@example.test", handles=(("tiktok", "pb"),))
    w.tax_confirmed(a)
    w.tax_confirmed(b)                                            # the same tax reference
    c = w.campaign()
    br = w.brief(c)
    assert w.ok(w.deal(a, c, br, fee="5000.00"), 201)["status"] == "approved"
    second = w.ok(w.deal(b, c, br, fee="5000.00"), 201)
    assert second["status"] == "pending_andre" and "INFLUENCER_TOTAL_OVER_LIMIT" in second["needs_andre"]


def test_m5_the_limit_is_checked_again_at_payout_keyed_on_the_payee(w):
    a = w.creator(email="p1@example.test", handles=(("instagram", "pa"),))
    b = w.creator(email="p2@example.test", handles=(("instagram", "pb"),))
    c = w.campaign()
    br = w.brief(c)
    da = w.ok(w.deal(a, c, br, fee="5000.00"), 201)
    db = w.ok(w.deal(b, c, br, fee="5000.00"), 201)               # no tax reference yet: not linked here
    assert da["status"] == db["status"] == "approved"
    paid = []
    for inf, d in ((a, da), (b, db)):
        w.contract(d)
        ct = w.ok(w.content(d), 201)
        w.approve_content(ct)
        w.ok(w.live(ct))
        w.tax_confirmed(inf)                                      # the same person: the same tax reference
        w.ok(w.verify(inf))
        paid.append(w.ok(w.payout(d, "5000.00", [ct["content_id"]]), 201))
    # a's payout came before b's tax reference tied the two records together; b's payout sees $10,000 for one payee
    assert paid[0]["status"] == "submitted" and paid[1]["status"] == "pending_andre"
    assert paid[1]["needs_andre"] == ["PAYEE_TOTAL_OVER_LIMIT"] and len(w.ports.finance.payouts) == 1
    url = f"/payouts/{paid[1]['payout_id']}/approve"
    w.code(w.post(url, {"request_id": rid(), "content_sha256": "1" * 64}, andre=True), 409, "CONTENT_HASH_MISMATCH")
    out = w.ok(w.post(url, {"request_id": rid(), "content_sha256": paid[1]["content_sha256"]}, andre=True))
    assert out["status"] == "submitted" and len(w.ports.finance.payouts) == 2


# ------------------------------------------------------------------------------------------------ L1

@pytest.mark.parametrize("caption,code", [("Loving this" + "\n" * 60 + "#ad", "DISCLOSURE_NOT_PROMINENT"),
                                          ("Loving\n#ad this", "DISCLOSURE_NOT_PROMINENT"),
                                          ("#ad" + "̶" * 3 + "⃠" + " Loving this", "DISCLOSURE_OBSCURED"),
                                          ("⃝#ad Loving this", "DISCLOSURE_OBSCURED")])
def test_l1_the_disclosure_is_on_the_first_line_and_unmarked(w, caption, code):
    inf, c, b, d = w.setup_deal()
    w.code(w.content(d, caption=caption), 422, code)


# ------------------------------------------------------------------------------------------------ L2

def test_l2_gmail_dot_and_plus_variants_are_one_address(w):
    a = w.creator(email="jane.doe@gmail.com", handles=(("instagram", "jd1"),))
    w.ok(w.post("/suppressions", {"request_id": rid(), "influencer_id": a["influencer_id"]}), 201)
    r = w.post("/influencers", {"request_id": rid(), "display_name": "J", "email": "Jane.Doe+x@googlemail.com",
                                "handles": [{"platform": "x", "handle": "jd2"}], "evidence_ref": "ev-2"})
    w.code(r, 409, "INFLUENCER_EXISTS")                           # the same mailbox: a58e633 made a new record
    w.ok(w.post("/suppressions", {"request_id": rid(), "email": "someone+news@example.test"}), 201)
    q = w.prospect(email="someone@example.test", handles=(("tiktok", "so1"),))
    assert q["suppressed"] is True


# ------------------------------------------------------------------------------------------------ L3

def test_l3_a_payout_is_handed_to_finance_once_at_a_time_and_verification_is_reread(tmp_path):
    class Slow(FakeFinance):
        def __init__(self):
            super().__init__(payout="unavailable")
            self.inside = threading.Event()
            self.go = threading.Event()

        def request_payout(self, *a):
            if self.payout == "accepted":
                self.inside.set()
                self.go.wait(10)
            return super().request_payout(*a)
    fin = Slow()
    h = Harness(tmp_path, ports=wired_ports(finance=fin))
    inf, d, ct = h.paid_ready()
    p = h.ok(h.payout(d, "100.00", [ct["content_id"]]), 201)
    assert p["status"] == "pending_finance"
    fin.payout = "accepted"
    t = threading.Thread(target=lambda: h.svc._hand_to_finance(p["payout_id"]))
    t.start()
    assert fin.inside.wait(10)
    assert h.svc._hand_to_finance(p["payout_id"]) == "in_flight"   # the retry job cannot race the hand-over
    fin.go.set()
    t.join(10)
    assert [x[0] for x in fin.payouts].count(p["payout_id"]) == 2   # the first refused attempt + this one
    assert h.svc.payouts[p["payout_id"]]["status"] == "submitted"


def test_l3_retry_rereads_verification_from_finance(tmp_path):
    fin = FakeFinance(payout="unavailable")
    h = Harness(tmp_path, ports=wired_ports(finance=fin))
    inf, d, ct = h.paid_ready()
    p = h.ok(h.payout(d, "100.00", [ct["content_id"]]), 201)
    fin.status, fin.payout = "refused", "accepted"
    out = h.ok(h.job("payout-retry"))
    assert out["cancelled"] == 1 and h.svc.payouts[p["payout_id"]]["reason"] == "PAYEE_NOT_VERIFIED"
    assert len(fin.payouts) == 1


# ------------------------------------------------------------------------------------------------ L4

def test_l4_a_false_minor_declaration_does_not_halt_an_attested_creator(w):
    inf = w.creator()
    w.code(w.application(adult=False), 422, "MINOR_REFUSED")
    out = w.ok(w.post(f"/influencers/{inf['influencer_id']}/minor-review",
                      {"request_id": rid(), "decision": "not_a_minor"}, andre=True))
    assert out["adult_attested"] is True and out["attestation"] == inf["attestation"]
