"""
Property tests:
- no admission (and no enrolment) while ANY gate input is absent, a stand-in, exploding or negative;
- no outbound message is ever handed to the provider without consent (a member relationship or an active
  opt-in) and outside the recipient-local quiet window (in-app without a time zone excepted).
Seeded randomness, so a failure reproduces.
"""

from __future__ import annotations

import random
from datetime import timedelta

import pytest

from fakes import ExplodingPort, FakeMessaging
from helpers import ANDRE_TOKEN, Harness, rid
from intelligences import i06_comms
from ports import AgeAnswer, Ports


def _k_vi_stand_in(h, cid): h.ports.vi = Ports().vi
def _k_vi_explode(h, cid): h.ports.vi = ExplodingPort()
def _k_age_unknown(h, cid): h.vi.age[cid] = AgeAnswer(True, "unknown", None)
def _k_age_minor(h, cid): h.vi.age[cid] = AgeAnswer(True, "minor", "vi-age-m")
def _k_dup(h, cid): h.vi.identity[cid] = "duplicate"
def _k_incomplete(h, cid): h.vi.identity[cid] = "incomplete"
def _k_no_conn(h, cid): h.vi.conns[cid] = []
def _k_conn_revoked(h, cid): h.vi.conns[cid] = [c.__class__(c.connection_id, c.platform, "revoked") for c in h.vi.conns.get(cid, [])] or []
def _k_x_only(h, cid):
    h.vi.conns[cid] = []
    h.vi.connect(cid, "x")
def _k_integrity(h, cid): h.vi.integrity_clear[cid] = False
def _k_cmp_stand_in(h, cid): h.ports.compliance = Ports().compliance
def _k_cmp_explode(h, cid): h.ports.compliance = ExplodingPort()
def _k_refuse(h, cid): h.ports.compliance.classes["US"] = h.ports.compliance.classes["US-CA"] = "refuse"
def _k_creator_blocked(h, cid): h.ports.compliance.creator_allowed = False
def _k_latest_other(h, cid):
    orig = h.ports.compliance.latest_activation
    h.ports.compliance.latest_activation = lambda lane, s: orig(lane, s).__class__(True, True, "cmp-rul-OTHER")
def _k_fin_stand_in(h, cid): h.ports.finance = Ports().finance
def _k_fin_explode(h, cid): h.ports.finance = ExplodingPort()
def _k_no_form(h, cid): h.ports.finance.form = False
def _k_legal_stand_in(h, cid): h.ports.legal = Ports().legal
def _k_legal_explode(h, cid): h.ports.legal = ExplodingPort()
def _k_new_agreement(h, cid): h.ports.legal.version = "v4"
def _k_no_training(h, cid): h.svc.st["trainings"].pop(cid, None)
def _k_no_acceptance(h, cid):
    for k in [k for k, a in h.svc.st["acceptances"].items() if a["clipper_id"] == cid]:
        h.svc.st["acceptances"].pop(k)
def _k_wrong_type(h, cid):
    h.ports.vi.integrity = lambda c: True       # a port answering the wrong type is unavailable


KNOCKOUTS = [v for k, v in sorted(globals().items()) if k.startswith("_k_")]


@pytest.mark.parametrize("ko", KNOCKOUTS, ids=[k.__name__ for k in KNOCKOUTS])
def test_single_input_absent_never_admits(ko):
    h = Harness().ready()
    cid = h.ready_applicant()
    ko(h, cid)
    j = h.admit(cid).json()
    assert j["admitted"] is False and j["unmet"], ko.__name__
    assert h.svc.st["clippers"][cid]["status"] in ("applicant", "refused", "offboarding")


def test_random_subsets_of_absent_inputs_never_admit():
    rnd = random.Random(20260926)
    for _ in range(60):
        h = Harness().ready()
        cid = h.ready_applicant()
        for ko in rnd.sample(KNOCKOUTS, rnd.randint(1, 5)):
            try:
                ko(h, cid)
            except (AttributeError, TypeError):
                pass      # a later knockout on a port an earlier one already replaced
        assert h.admit(cid).json()["admitted"] is False


def test_rules_not_approved_never_admits_even_with_every_fake_passing():
    h = Harness()
    cid = h.apply().json()["clipper_id"]
    assert h.admit(cid).json()["admitted"] is False


ENROL_KOS = {
    "creative_stand_in": lambda h, c: setattr(h.ports, "creative", Ports().creative),
    "kit_unsigned": lambda h, c: h.ports.creative.kits.__setitem__("camp-1", h.ports.creative.kit("camp-1").__class__(
        True, "kit-x", 1, "draft", "a" * 64)),
    "kit_old_version": lambda h, c: (h.ports.creative.kits.__setitem__("camp-1", h.ports.creative.kit("camp-1")),
                                     h.ports.creative.live.__setitem__("camp-1", 2)),
    "brand_blocked": lambda h, c: setattr(h.ports.compliance, "brand_allowed", False),
    "rate_card_missing": lambda h, c: h.ports.finance.cards.__setitem__(("rc-1", "v1"), h.ports.finance.rate_card(
        "zz", "zz").__class__(True, False)),
    "finance_stand_in": lambda h, c: setattr(h.ports, "finance", Ports().finance),
    "legal_new_version": lambda h, c: setattr(h.ports.legal, "version", "v9"),
    "no_campaign_platform": lambda h, c: (h.vi.conns.__setitem__(c, []), h.vi.connect(c, "instagram")),
    "vi_stand_in": lambda h, c: setattr(h.ports, "vi", Ports().vi),
    "compliance_stand_in": lambda h, c: setattr(h.ports, "compliance", Ports().compliance),
    "counsel_cq03_sag": lambda h, c: [a.__setitem__("sag_aftra_member", True) for a in h.svc.st["applications"].values()],
}


@pytest.mark.parametrize("name", sorted(ENROL_KOS))
def test_enrolment_never_passes_with_an_absent_input(name):
    h = Harness().ready()
    cid = h.admitted_clipper()
    h.config()
    ENROL_KOS[name](h, cid)
    j = h.enrol(cid).json()
    assert j["eligible"] is False and j["enrolment_id"] is None, name
    assert not h.svc.st["enrolments"]


def test_enrolment_counsel_hold_cq01_blocks_until_memo():
    h = Harness()
    h.approve_seed()
    cid = h.admitted_clipper()
    h.config()
    j = h.enrol(cid).json()
    assert ("CN-CQ-01", "COUNSEL_HOLD") in {(u["rule_id"], u["code"]) for u in j["unmet"]}
    h.clear_counsel("CN-CQ-01")
    assert h.enrol(cid).json()["eligible"] is True


def test_caps_tier_and_campaign():
    h = Harness().ready()
    cid = h.admitted_clipper()
    for i in range(3):
        h.config(campaign=f"c{i}")
    assert h.enrol(cid, "c0").json()["eligible"] and h.enrol(cid, "c1").json()["eligible"]
    j = h.enrol(cid, "c2").json()                                    # T0 cap is 2
    assert ("CN-13", "CAP_CLIPPER_ENROLMENTS") in {(u["rule_id"], u["code"]) for u in j["unmet"]}
    h.config(campaign="tiny", max_clippers=1)
    other = h.admitted_clipper("o@example.com")
    assert h.enrol(other, "tiny").json()["eligible"]
    third = h.admitted_clipper("t@example.com")
    assert ("CN-13", "CAP_CAMPAIGN_FULL") in {(u["rule_id"], u["code"]) for u in h.enrol(third, "tiny").json()["unmet"]}


# ------------------------------------------------------------------------------------------------ comms

class ClockedMessaging(FakeMessaging):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.at: dict[str, object] = {}

    def send(self, message_id, channel, recipient, body):
        self.at[message_id] = self.clock.now()
        return super().send(message_id, channel, recipient, body)


ZONES = [None, "America/Los_Angeles", "America/New_York", "Europe/London", "Asia/Tokyo", "Asia/Kolkata",
         "Pacific/Auckland", "Australia/Adelaide"]


def test_property_no_message_without_consent_or_outside_quiet_hours():
    rnd = random.Random(7)
    for trial in range(12):
        h = Harness().ready()
        h.ports.messaging = ClockedMessaging(h.clock)
        h.clock.at = h.clock.at + timedelta(minutes=rnd.randrange(0, 60 * 24 * 7))
        for n in range(4):
            tz = rnd.choice(ZONES)
            over = {"time_zone": tz} if tz else {"time_zone": "__omit__"}
            r = h.apply(email=f"p{trial}-{n}@example.com", **over)
            assert r.status_code == 201, r.text
            h.clock.advance(minutes=rnd.randrange(0, 600))
        for n in range(3):
            email = f"r{trial}-{n}@example.com"
            if rnd.random() < 0.6:
                h.post("/cn/v1/opt-ins", {"request_id": rid(), "email": email, "recipient_country": "US",
                                          "time_zone": rnd.choice(ZONES[1:]), "consent_text_sha256": "c" * 64,
                                          "source_form_id": "f", "captured_at": "2026-09-28T10:00:00Z",
                                          "age_18_plus_confirmed": True}, caller="hub")
            rc = h.post("/cn/v1/recruiting/campaigns", {"request_id": rid(), "channel": "email_opt_in",
                                                        "template_id": "recruiting_invite", "recipients": [email]},
                        andre=ANDRE_TOKEN)
            h.run(f"/cn/v1/recruiting/campaigns/{rc.json()['recruit_id']}/send")
            if rnd.random() < 0.3:
                h.post("/cn/v1/opt-outs", {"request_id": rid(), "email": email}, caller="hub")
        for _ in range(8):
            h.clock.advance(minutes=rnd.randrange(30, 400))
            h.run("/cn/v1/messages/flush")
        for mid, at in h.ports.messaging.at.items():
            m = h.svc.st["messages"][mid]
            if m["clipper_id"]:
                c = h.svc.st["clippers"][m["clipper_id"]]
                assert m["consent_basis"] == "member_relationship"
                tz = c.get("time_zone")
                if not tz:
                    assert m["channel"] == "in_app"
                    continue
            else:
                o = h.svc.st["opt_ins"][m["opt_in_record_id"]]
                assert m["consent_basis"] == o["opt_in_record_id"]
                tz = o["time_zone"]
            assert i06_comms.inside_window(at, tz, "08:00-20:00"), (mid, at, tz)
        # every message the provider saw was an approved template version
        for mid in h.ports.messaging.at:
            m = h.svc.st["messages"][mid]
            assert h.svc.current.template(m["template_id"])["version"] == m["template_version"]


def test_sent_recruiting_needs_an_opt_in_that_was_active_when_sent():
    h = Harness().ready()
    h.ports.messaging = ClockedMessaging(h.clock)
    h.clock.at = h.clock.at.replace(hour=3)          # 23:00 in New York: the invite waits
    h.post("/cn/v1/opt-ins", {"request_id": rid(), "email": "late@example.com", "recipient_country": "US",
                              "time_zone": "America/New_York", "consent_text_sha256": "c" * 64, "source_form_id": "f",
                              "captured_at": "2026-09-28T10:00:00Z", "age_18_plus_confirmed": True}, caller="hub")
    rc = h.post("/cn/v1/recruiting/campaigns", {"request_id": rid(), "channel": "email_opt_in",
                                                "template_id": "recruiting_invite", "recipients": ["late@example.com"]},
                andre=ANDRE_TOKEN)
    out = h.run(f"/cn/v1/recruiting/campaigns/{rc.json()['recruit_id']}/send").json()
    assert out["queued"] == 1 and out["sent"] == 0
    h.post("/cn/v1/opt-outs", {"request_id": rid(), "email": "late@example.com"}, caller="hub")
    h.clock.advance(hours=12)
    h.run("/cn/v1/messages/flush")
    assert h.ports.messaging.at == {}
    assert h.ledger.of_type("recruiting_send_refused")
