"""Client Success (29): explainable health score, at-risk -> alert + save plan with only pre-approved offers,
renewal tracker, NPS (ADR 0014 decisions 18-20)."""

from __future__ import annotations

from datetime import datetime, timezone

import health
from helpers import FixedSignals, Harness, RecordingSender, rid
from ports import ContractEnd, Ports


def _ports(trend=None, payment=None, end=None, wired_senders=False):
    ports = Ports.default()
    sig = FixedSignals(trend, payment, end)
    ports.results, ports.finance = sig, sig
    ports.contracts["onboarding"] = sig
    senders = {}
    if wired_senders:
        senders = {c: RecordingSender() for c in ("email", "sms", "chat")}
        ports.senders.update(senders)
    return ports, senders


def _client(h, login=True, **contact):
    cid = h.contact(display_name="Dana Rivera", **contact)
    h.account("acct-1", contact_id=cid)
    if login:
        h.ok(h.post("/svc/v1/accounts/acct-1/events", {"request_id": rid(), "kind": "portal_login",
                                                       "occurred_at": "2026-10-05T12:00:00Z"}, caller="hub"), 201)
    return cid


def _plan(h):
    plans = h.ok(h.get("/svc/v1/save-plans?status=active"))
    return plans[0] if plans else None


# --------------------------------------------------------------------------------------------------- score

def test_score_unit_is_integer_explainable_and_clamped():
    now = datetime(2026, 10, 6, tzinfo=timezone.utc)
    r = health.compute(now, "up", "current", now, 0, 0, 10)
    assert r["score"] == 100 and {s["signal"] for s in r["signals"]} == {"results_up", "payment_current",
                                                                         "login_recent", "nps_promoter"}
    r = health.compute(now, "down", "failed", None, 9, 9, 0)
    assert r["score"] == 0 and sum(s["points"] for s in r["signals"]) == -25 - 30 - 15 - 30 - 15 - 20
    r = health.compute(now, None, None, now, 0, 0, None)
    assert r["score"] == 90 and [s["signal"] for s in r["signals"]] == ["results_unknown", "payment_unknown",
                                                                        "login_recent"]


def test_unwired_signals_are_unknown_never_fine(h):
    _client(h)
    h.ok(h.job("health-recompute"))
    hv = h.ok(h.get("/svc/v1/accounts/acct-1/health"))
    assert hv["health"]["score"] == 90 and not hv["at_risk"]
    assert {s["signal"] for s in hv["health"]["signals"]} >= {"results_unknown", "payment_unknown"}
    assert all({"signal", "points", "source"} == set(s) for s in hv["health"]["signals"])


def test_complaints_and_escalations_lower_the_score(tmp_path):
    ports, _ = _ports("up", "current")
    h = Harness(tmp_path, ports=ports)
    _client(h)
    h.ok(h.chat("this is the worst, refund me"), 201)
    h.ok(h.job("health-recompute"))
    sig = {s["signal"]: s["points"] for s in h.ok(h.get("/svc/v1/accounts/acct-1/health"))["health"]["signals"]}
    assert sig["complaints_30d"] == -10 and sig["open_escalations"] == -5


# --------------------------------------------------------------------------------------------------- at risk

def test_at_risk_account_alerts_andre_and_starts_a_save_plan(tmp_path):
    ports, _ = _ports("down", "failed")
    h = Harness(tmp_path, ports=ports)
    _client(h)
    r = h.ok(h.job("health-recompute"))
    assert r["save_plans_started"] == ["acct-1"]
    hv = h.ok(h.get("/svc/v1/accounts/acct-1/health"))
    assert hv["at_risk"] is True and hv["health"]["score"] == 45
    assert "ACCOUNT_AT_RISK" in [a["code"] for a in h.ok(h.get("/svc/v1/alerts"))]
    plan = _plan(h)
    assert [s["kind"] for s in plan["steps"]] == ["check_in", "results_review", "offer"]
    assert plan["steps"][2]["status"] == "awaiting_selection"
    assert h.ledger.of_type("save_plan_started")
    assert h.ok(h.job("health-recompute"))["save_plans_started"] == []      # one plan at a time


def test_threshold_is_configurable(tmp_path):
    ports, _ = _ports("flat", "current")
    h = Harness(tmp_path, ports=ports, SVC_AT_RISK_THRESHOLD="95")
    _client(h)
    assert h.ok(h.job("health-recompute"))["save_plans_started"] == ["acct-1"]


def test_save_plan_check_in_uses_an_approved_template_and_obeys_consent(tmp_path):
    ports, senders = _ports("down", "failed", wired_senders=True)
    h = Harness(tmp_path, ports=ports)
    cid = _client(h, phone="+13105551234", timezone="America/Los_Angeles")
    h.template("checkin-unapproved", "check_in", approve=False)
    h.ok(h.job("health-recompute"))
    tick = h.ok(h.job("save-plan-tick"))
    assert tick["check_ins"] == 0 and tick["blocked"] == 1 and tick["tasks"] == 1
    assert _plan(h)["steps"][0]["reason"] == "NO_APPROVED_TEMPLATE"
    h.template("checkin", "check_in")
    assert h.ok(h.job("save-plan-tick"))["check_ins"] == 1
    out = h.ok(h.get("/svc/v1/outbound"))
    assert len(out) == 1 and out[0]["channel"] == "chat" and out[0]["proactive"] is True   # no consent: never SMS
    h.ok(h.job("outbound-tick"))
    assert senders["chat"].sent and not senders["sms"].sent
    assert senders["chat"].sent[0].text == "Hi Dana, checking in from Z Best Media. How are results looking?"
    assert cid


def test_save_plan_check_in_goes_by_sms_only_with_consent(tmp_path):
    ports, senders = _ports("down", "failed", wired_senders=True)
    h = Harness(tmp_path, ports=ports)
    cid = _client(h, phone="+13105551234", timezone="America/Los_Angeles")
    h.ok(h.consent(cid), 201)
    h.template("checkin", "check_in")
    h.ok(h.job("health-recompute"))
    h.ok(h.job("save-plan-tick"))
    h.ok(h.job("outbound-tick"))
    assert senders["sms"].sent and senders["sms"].sent[0].text.endswith("Reply STOP to opt out.")


def test_results_review_task_is_recorded_and_completed(tmp_path):
    ports, _ = _ports("down", "failed")
    h = Harness(tmp_path, ports=ports)
    _client(h)
    h.ok(h.job("health-recompute"))
    plan = _plan(h)
    review = plan["steps"][1]
    path = f"/svc/v1/save-plans/{plan['plan_id']}/steps/{review['step_id']}/done"
    assert h.post(path, {"request_id": rid()}).json()["detail"] == "STEP_NOT_OPEN"
    h.ok(h.job("save-plan-tick"))
    h.ok(h.post(path, {"request_id": rid()}))
    assert _plan(h)["steps"][1]["status"] == "done"


def test_only_a_pre_approved_offer_can_be_selected(tmp_path):
    ports, _ = _ports("down", "failed")
    h = Harness(tmp_path, ports=ports)
    _client(h)
    h.offer("draft-offer", approve=False)
    h.offer("zbc-offer", brand="zbc")
    h.ok(h.job("health-recompute"))
    pid = _plan(h)["plan_id"]
    for offer_id in ("draft-offer", "zbc-offer", "no-such-offer"):
        r = h.post(f"/svc/v1/save-plans/{pid}/offer", {"request_id": rid(), "offer_id": offer_id}, andre=True)
        assert r.status_code == 409 and r.json()["detail"] == "OFFER_NOT_APPROVED"
    # nothing ad hoc: a price, terms or text in the selection is refused
    r = h.post(f"/svc/v1/save-plans/{pid}/offer", {"request_id": rid(), "offer_id": "draft-offer", "price": "1.00"},
               andre=True)
    assert r.status_code == 422
    h.offer("month-free")
    assert h.post(f"/svc/v1/save-plans/{pid}/offer", {"request_id": rid(), "offer_id": "month-free"}).status_code \
        == 403                                                         # Andre only
    h.ok(h.post(f"/svc/v1/save-plans/{pid}/offer", {"request_id": rid(), "offer_id": "month-free"}, andre=True))
    assert h.ledger.of_type("offer_selected")


def test_the_selected_offer_is_sent_with_its_approved_terms_and_price(tmp_path):
    ports, senders = _ports("down", "failed", wired_senders=True)
    h = Harness(tmp_path, ports=ports)
    _client(h)
    h.template("offer-msg", "offer")
    h.offer("month-free", price="1250.00")
    h.ok(h.job("health-recompute"))
    pid = _plan(h)["plan_id"]
    h.ok(h.post(f"/svc/v1/save-plans/{pid}/offer", {"request_id": rid(), "offer_id": "month-free"}, andre=True))
    assert h.ok(h.job("save-plan-tick"))["offers"] == 1
    h.ok(h.job("outbound-tick"))
    text = senders["chat"].sent[0].text
    assert "One month of management at no charge" in text and "$1,250.00" in text


def test_save_plan_never_sends_an_offer_edited_after_selection(tmp_path):
    ports, senders = _ports("down", "failed", wired_senders=True)
    h = Harness(tmp_path, ports=ports)
    _client(h)
    h.template("offer-msg", "offer")
    h.offer("month-free")
    h.ok(h.job("health-recompute"))
    pid = _plan(h)["plan_id"]
    h.ok(h.post(f"/svc/v1/save-plans/{pid}/offer", {"request_id": rid(), "offer_id": "month-free"}, andre=True))
    h.offer("month-free", price="0.00", terms="Two months free, no strings.", approve=False)   # edited: unapproved
    tick = h.ok(h.job("save-plan-tick"))
    assert tick["offers"] == 0 and tick["blocked"] >= 1
    step = _plan(h)["steps"][2]
    assert step["status"] == "awaiting_selection" and step["reason"] == "OFFER_NOT_APPROVED"
    h.ok(h.job("outbound-tick"))
    assert not any("Two months" in m.text for m in senders["chat"].sent)


def test_an_offer_edited_after_queueing_is_cancelled_before_sending(tmp_path):
    ports, senders = _ports("down", "failed", wired_senders=True)
    h = Harness(tmp_path, ports=ports)
    _client(h)
    h.template("offer-msg", "offer")
    h.offer("month-free")
    h.ok(h.job("health-recompute"))
    pid = _plan(h)["plan_id"]
    h.ok(h.post(f"/svc/v1/save-plans/{pid}/offer", {"request_id": rid(), "offer_id": "month-free"}, andre=True))
    h.ok(h.job("save-plan-tick"))
    h.ok(h.post("/svc/v1/offers/month-free/retire", {"request_id": rid()}, andre=True))
    tick = h.ok(h.job("outbound-tick"))
    assert tick["cancelled"] >= 1 and not senders["chat"].sent
    assert [m for m in h.ok(h.get("/svc/v1/outbound")) if m["reason"] == "OFFER_NOT_APPROVED"]


def test_offer_prices_are_decimal_strings(h):
    for bad in (12.5, "12.5", "012.50", "1e3", "-1.00"):
        r = h.post("/svc/v1/offers", {"request_id": rid(), "item_id": "bad-offer", "brand": "zbm", "title": "t",
                                      "terms": "x", "price": bad})
        assert r.status_code == 422


def test_close_plan_andre_only(tmp_path):
    ports, _ = _ports("down", "failed")
    h = Harness(tmp_path, ports=ports)
    _client(h)
    h.ok(h.job("health-recompute"))
    pid = _plan(h)["plan_id"]
    assert h.post(f"/svc/v1/save-plans/{pid}/close", {"request_id": rid(), "outcome": "saved"}).status_code == 403
    h.ok(h.post(f"/svc/v1/save-plans/{pid}/close", {"request_id": rid(), "outcome": "saved"}, andre=True))
    assert h.ok(h.get("/svc/v1/accounts/acct-1/health"))["save_plan"] is None


# --------------------------------------------------------------------------------------------------- renewals, NPS

def test_renewal_tracker_from_onboarding_and_from_the_port(tmp_path):
    class OnlyAcct1(FixedSignals):
        def contract_end(self, account_id):
            return super().contract_end(account_id) if account_id == "acct-1" else ContractEnd(False)
    ports, _ = _ports("up", "current")
    ports.contracts["onboarding"] = OnlyAcct1(end="2026-11-20")
    h = Harness(tmp_path, ports=ports)
    _client(h)
    h.account("acct-2", contract_end="2027-06-01")
    r = h.ok(h.job("health-recompute"))
    assert r["renewals_flagged"] == ["acct-1"]
    ren = h.ok(h.get("/svc/v1/renewals"))
    assert [x["account_id"] for x in ren] == ["acct-1"] and ren[0]["source"] == "port" and ren[0]["days_left"] == 45
    assert h.ok(h.job("health-recompute"))["renewals_flagged"] == []          # alerted once per end date


def test_nps_survey_needs_an_approved_template_and_consent(tmp_path):
    ports, senders = _ports(wired_senders=True)
    h = Harness(tmp_path, ports=ports)
    _client(h, phone="+13105551234", timezone="America/Los_Angeles", email="dana@acme.test")
    body = {"account_id": "acct-1", "channel": "sms"}
    assert h.post("/svc/v1/nps/surveys", {"request_id": rid(), **body}).json()["detail"] == "SMS_CONSENT_REQUIRED"
    assert h.post("/svc/v1/nps/surveys", {"request_id": rid(), "account_id": "acct-1", "channel": "email"}).json()[
        "detail"] == "EMAIL_CONSENT_REQUIRED"
    assert h.post("/svc/v1/nps/surveys", {"request_id": rid(), "account_id": "acct-1", "channel": "chat"}).json()[
        "detail"] == "NO_APPROVED_TEMPLATE"
    h.template("nps", "nps_survey")
    s = h.ok(h.post("/svc/v1/nps/surveys", {"request_id": rid(), "account_id": "acct-1", "channel": "chat"}), 201)
    h.ok(h.job("outbound-tick"))
    assert s["survey_id"] in senders["chat"].sent[0].text


def test_nps_detractor_alerts_and_counts_in_the_score(h):
    _client(h)
    h.template("nps", "nps_survey")
    s = h.ok(h.post("/svc/v1/nps/surveys", {"request_id": rid(), "account_id": "acct-1", "channel": "chat"}), 201)
    r = h.ok(h.post("/svc/v1/nps/responses", {"request_id": rid(), "survey_id": s["survey_id"], "score": 3,
                                              "comment": "slow replies"}, caller="hub"), 201)
    assert r["band"] == "detractor"
    assert h.post("/svc/v1/nps/responses", {"request_id": rid(), "survey_id": s["survey_id"], "score": 9},
                  caller="hub").json()["detail"] == "SURVEY_ANSWERED"
    assert "NPS_DETRACTOR" in [a["code"] for a in h.ok(h.get("/svc/v1/alerts"))]
    h.ok(h.job("health-recompute"))
    assert {"signal": "nps_detractor", "points": -20, "source": "nps"} in \
        h.ok(h.get("/svc/v1/accounts/acct-1/health"))["health"]["signals"]
    assert "slow replies" not in str(h.ledger.events)


def test_nps_score_is_a_strict_integer(h):
    for bad in (11, -1, "7", 7.0, True):
        r = h.post("/svc/v1/nps/responses", {"request_id": rid(), "survey_id": "sv-nps-" + "0" * 40, "score": bad},
                   caller="hub")
        assert r.status_code == 422


def test_account_brand_cannot_change_and_contact_must_match(h):
    cid = h.contact(brand="zbc", contact_ref="client:clips")
    h.account("acct-9")
    r = h.post("/svc/v1/accounts", {"request_id": rid(), "account_id": "acct-9", "brand": "zbc"}, caller="onboarding")
    assert r.json()["detail"] == "ACCOUNT_BRAND_MISMATCH"
    r = h.post("/svc/v1/accounts", {"request_id": rid(), "account_id": "acct-8", "brand": "zbm",
                                    "primary_contact_id": cid}, caller="onboarding")
    assert r.json()["detail"] == "CONTACT_NOT_FOUND"
