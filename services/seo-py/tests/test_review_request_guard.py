"""SEO-L6 review-request guard: unit rules plus certification against the shared MVP fixture pool (SEO-L6, 300
scenarios per seed). The key is the generator's planted answer; this test never imports the pool's oracle.

Two disclosed differences, both stricter: a message that asks for a "5-star" review is also refused as ``gating``
(soliciting only positive reviews, which Google bars; the pool lists only ``incentive`` on its one such message), and
every refused row whose wording is not an approved template also carries ``unapproved_wording``. A row the key says
sends must carry no reason at all.

The pool holds few distinct wordings, so it cannot show the wording rules work (AEGIS round 1: a guard that only
looked up the pool's phrases passed it). The wording proof is the paraphrase corpus below: every reworded offer,
positive-only ask, suggestion or steer-away is refused, whatever the word lists catch, because only an approved
template ever sends."""

import sys
from pathlib import Path

import pytest

import review_request_guard as g

POOLS = Path(__file__).resolve().parents[3] / "fixtures" / "mvp_pools"
LINK = "https://reviews.example.com/r"


def req(message=f"How did we do? We read every review, good or bad: {LINK}", channel="sms", **customer):
    c = {"phone": "+12135550101", "transaction": {"type": "service_visit", "days_ago": 3}, "sms_consent": True,
         "opted_out": False}
    c.update(customer)
    return {"request_id": "revreq-1", "channel": channel, "customer": c, "audience_filter": None, "message": message}


def reasons(r):
    return g.check(r).reasons


@pytest.mark.parametrize("template", g.APPROVED_TEMPLATES)
def test_every_approved_template_goes_to_a_real_customer(template):
    assert g.check(req(template.format(link=LINK))) == g.Verdict("revreq-1", "send", ())


@pytest.mark.parametrize("template", g.APPROVED_TEMPLATES)
def test_no_approved_template_trips_a_wording_reading(template):
    assert g._wording_reasons(g._text(template.format(link=LINK))) == set()


@pytest.mark.parametrize("message", [f"We'd love your honest feedback, whatever it is: {LINK}",
                                     f"How did we do? We read every review, good or bad: {LINK} ",
                                     f"How did we do? We read every review, good or bad: http://reviews.example.com/r",
                                     f"How did we do? We read every review, good or bad: {LINK} {LINK}",
                                     "How did we do? We read every review, good or bad:"])
def test_wording_that_is_not_an_approved_template_is_refused(message):
    assert "unapproved_wording" in reasons(req(message)) or "unreadable" in reasons(req(message))


def test_an_email_subject_must_be_approved():
    r = req(channel="email")
    r["subject"] = "How did we do?"
    assert reasons(r) == ()
    r["subject"] = "Get 20% off for your review!"
    assert set(reasons(r)) == {"incentive", "unapproved_wording"}


@pytest.mark.parametrize("tx", [None, {}, {"type": "quote"}, {"type": "purchase"}, {"type": "purchase", "days_ago": -1},
                                {"type": "purchase", "days_ago": 366}, {"type": "purchase", "days_ago": "3"},
                                {"type": "purchase", "days_ago": True}])
def test_no_real_transaction_refuses(tx):
    assert "no_transaction" in reasons(req(transaction=tx))


@pytest.mark.parametrize("consent", [False, None, "yes", 1])
def test_a_text_needs_sms_consent(consent):
    assert reasons(req(sms_consent=consent)) == ("no_sms_consent",)


@pytest.mark.parametrize("phone", [None, "", "213-555-0101", "+1213555010", 2135550101])
def test_a_text_needs_a_readable_phone(phone):
    assert "no_phone" in reasons(req(phone=phone))


@pytest.mark.parametrize("tx", [{"type": "purchase", "days_ago": 2, "status": "cancelled"},
                                {"type": "purchase", "days_ago": 2, "status": "refunded"},
                                {"type": "purchase", "days_ago": 2, "status": "disputed"},
                                {"type": "purchase", "days_ago": 2, "refunded": True},
                                {"type": "purchase", "days_ago": 2, "chargeback": True}])
def test_a_cancelled_refunded_or_unknown_transaction_is_no_transaction(tx):
    assert "no_transaction" in reasons(req(transaction=tx))


@pytest.mark.parametrize("flag", ["sms_opted_out", "do_not_contact", "unsubscribed", "email_opted_out"])
@pytest.mark.parametrize("value", [True, None, "yes"])
def test_every_opt_out_flag_refuses(flag, value):
    assert "opted_out" in reasons(req(**{flag: value}))


def test_an_unknown_field_is_refused_not_ignored():
    r = req()
    r["segment"] = "promoters_only"
    assert "unknown_field" in reasons(r)
    assert "unknown_field" in reasons(req(nps_score=10))


def test_an_email_needs_no_sms_consent():
    assert reasons(req(channel="email", sms_consent=False)) == ()


@pytest.mark.parametrize("flag", [True, None, "no"])
def test_an_opted_out_or_unknown_customer_is_refused(flag):
    assert "opted_out" in reasons(req(opted_out=flag))


@pytest.mark.parametrize("filt", [{"only_if_rating_at_least": 4}, {"only_if_survey_score_at_least": 9},
                                  {"exclude_if_complained": True}, ["promoters"], "happy_only"])
def test_any_audience_filter_is_gating(filt):
    r = req()
    r["audience_filter"] = filt
    assert reasons(r) == ("gating",)


@pytest.mark.parametrize("message", [f"Loved your visit? Leave us a 5-star review! {LINK}",
                                     f"If you were happy, please leave a positive review: {LINK}",
                                     f"Déjenos una reseña de 5 estrellas: {LINK}"])
def test_asking_only_for_positive_reviews_is_gating(message):
    assert "gating" in reasons(req(message))


@pytest.mark.parametrize("message", ["Leave us a review and get 10% off your next visit!",
                                     "Review us and we'll send you a $25 gift card.",
                                     "Review us for a chance to win dinner for two.",
                                     "Your next cleaning is free when you review us.",
                                     "Review us and earn 100 reward points.", "Get 5 dollars credit for a review.",
                                     "Deje su reseña y reciba un descuento.", "Reseña y participa en el sorteo."])
def test_anything_offered_for_a_review_is_an_incentive(message):
    assert "incentive" in reasons(req(f"{message} {LINK}"))


@pytest.mark.parametrize("message", ["You could say: 'Best service in town, highly recommend!'",
                                     "Feel free to copy this: 'Fast, friendly and fair prices.'",
                                     "Say something like 'great team and quick work'.",
                                     "Puede decir: 'El mejor servicio de la ciudad'."])
def test_suggested_review_text_refuses(message):
    assert "suggested_text" in reasons(req(f"How did we do? {LINK} {message}"))


@pytest.mark.parametrize("message", ["If you weren't happy, please contact us instead of posting a review.",
                                     "Please call us before leaving a review.",
                                     "Si no quedó satisfecho, llámenos en lugar de publicar una reseña."])
def test_steering_unhappy_customers_away_refuses(message):
    assert "review_suppression" in reasons(req(f"{message} {LINK}"))


def test_every_reason_is_listed():
    r = req(f"Review us and get 10% off! You could say: 'Best in town, truly the best'. {LINK}", transaction=None,
            opted_out=True)
    r["audience_filter"] = {"only_if_rating_at_least": 4}
    assert reasons(r) == ("gating", "incentive", "no_transaction", "opted_out", "suggested_text", "unapproved_wording")


def test_an_unreadable_request_still_lists_every_other_reason():
    v = g.check({"request_id": "x", "channel": "fax", "customer": {"opted_out": True},
                 "message": f"Get 20% off for a 5-star review {LINK}"})
    assert {"unreadable", "opted_out", "incentive", "gating", "no_transaction", "unapproved_wording"} <= set(v.reasons)


@pytest.mark.parametrize("bad", [None, [], {"request_id": "x"}, {"request_id": "x", "channel": "fax", "customer": {}},
                                 {"request_id": "x", "channel": "sms", "customer": None}])
def test_an_unreadable_request_is_refused(bad):
    assert g.check(bad).decision == "refuse" and "unreadable" in g.check(bad).reasons


def test_an_empty_message_is_refused():
    assert "unreadable" in reasons(req(""))


# ---------------------------------------------------------------- certification on the shared pool
def _pool():
    if str(POOLS) not in sys.path:
        sys.path.insert(0, str(POOLS))
    from poolgen import local_ops
    return local_ops


def _grade(seed, check=g.check):
    requests, key = _pool().generate_l6(seed)
    by_id = {k["request_id"]: k for k in key}
    assert len(by_id) == len(requests) == len(key)
    wrong = []
    for r in requests:
        k, v = by_id[r["request_id"]], check(r)
        extra = set(v.reasons) - set(k["reasons"]) - ({"unapproved_wording"} if k["decision"] == "refuse" else set())
        stricter_ok = extra <= {"gating"} and "5-star" in r["message"]
        if v.decision != k["decision"] or not set(k["reasons"]) <= set(v.reasons) or (extra and not stricter_ok):
            wrong.append((k["reasons"], v.decision, v.reasons, r["message"][:60]))
    return len(requests), wrong


@pytest.mark.parametrize("seed", ["dev-a", "dev-b", "dev-c", "l6-cert-1", "l6-cert-2", "l6-cert-3"])
def test_certification_on_the_shared_l6_pool(seed):
    n, wrong = _grade(seed)
    assert n == 300
    assert wrong == [], wrong[:5]


def test_the_certification_grader_catches_one_gated_send():
    def leaky(r):
        v = g.check(r)
        if v.reasons == ("gating",) and not getattr(leaky, "done", False):
            leaky.done = True
            return g.Verdict(v.request_id, "send", ())
        return v
    assert _grade("dev-a", leaky)[1]


# ---------------------------------------------------------------- AEGIS round 1 (Inquisitor, bbb4e06c): paraphrases
INCENTIVES = [
    "Leave us a review and your next visit is on the house!", "Review us and treat yourself to a dessert, our thank-you.",
    "Reviewers unlock VIP perks. Leave yours today!", "Leave a review and we'll send you a thank-you gift.",
    "Review us and save 10 percent on your next service.", "Review us and save 10 per cent next time.",
    "Review us and get fifty bucks toward your next visit.", "Leave a review and get twenty dollars back.",
    "Leave a review and get \u20ac10 toward your next order.", "Leave a review and get USD10 toward your next order.",
    "Leave a review and get \U0001F4B210 toward your next order.", "Review us = \U0001F381 for you!",
    "Review us and save 10\u066a next time.", "Every review earns store credits.",
    "One lucky reviewer will be the winner of an iPad.", "Review us and get a fr\u200bee oil change.",
    "Review us and get a fr\u0435e oil change.", "Review us and get a free\u00adbie.", "Review us and get freebies.",
    "Leave a review and we'll waive your next delivery fee.", "Leave a review and we'll refund your delivery fee.",
    "Leave a review and get 2-for-1 tacos on Tuesday.",
    "Leave a review and you'll feel free of charge on your next wash.", "Deje una rese\u00f1a y gane un iPad.",
    "Deje una rese\u00f1a y su pr\u00f3xima visita es cortes\u00eda de la casa.",
    "Deje una rese\u00f1a y reciba un regalo.", "Deje una rese\u00f1a y reciba una bebida sin costo.",
    "Deje una rese\u00f1a y obtenga un 2x1.", "Deje una rese\u00f1a y le hacemos una rebaja.",
    "Deje una rese\u00f1a y reciba un obsequio."]
GATING = [
    "If you're happy with our service, please leave us a review!", "If you are happy with us, please leave a review.",
    "Had a great experience? Leave us a review!", "Satisfied customers only: please share a review.",
    "Leave us a \u2b50\u2b50\u2b50\u2b50\u2b50 review!", "Leave us a 5\u2605 review!", "Leave us a five\u2011star review!",
    "Leave us a 5\u2013star review!", "Leave us a rave review!", "Please share a positive experience on Google!",
    "Please leave positive feedback on Google!",
    "Rate us 1-10 first; if you give us a 9 or 10 we'll send the Google link.",
    "Si qued\u00f3 contento, d\u00e9jenos una rese\u00f1a.", "Si le encant\u00f3, d\u00e9jenos una rese\u00f1a.",
    "D\u00e9jenos un comentario positivo."]
SUGGESTIONS = ["Mention our friendly staff and fast service in your review!",
               "Tell people we were Best In Town \u2014 Fast And Friendly!", "Use these words: best service in town."]
STEER_AWAY = ["Not happy? Text us back and we'll fix it, no need to post.",
              "Unhappy? Reply to this message first so we can make it right.",
              "\u00bfNo qued\u00f3 contento? Resp\u00f3ndanos primero."]


@pytest.mark.parametrize("message", INCENTIVES + GATING + SUGGESTIONS + STEER_AWAY)
def test_no_reworded_offer_gate_suggestion_or_steer_away_is_ever_sent(message):
    assert g.check(req(f"{message} {LINK}")).decision == "refuse"


@pytest.mark.parametrize("message", INCENTIVES)
def test_reworded_offers_are_named_as_incentives(message):
    assert "incentive" in reasons(req(f"{message} {LINK}"))


@pytest.mark.parametrize("message", GATING)
def test_reworded_positive_only_asks_are_named_as_gating(message):
    assert "gating" in reasons(req(f"{message} {LINK}"))


@pytest.mark.parametrize("message", SUGGESTIONS)
def test_reworded_suggestions_are_named(message):
    assert "suggested_text" in reasons(req(f"How did we do? {message} {LINK}"))


@pytest.mark.parametrize("message", STEER_AWAY)
def test_reworded_steer_aways_are_named(message):
    assert "review_suppression" in reasons(req(f"{message} {LINK}"))


def test_an_apostrophe_pair_is_not_a_quoted_review():
    assert "suggested_text" not in reasons(req(f"We'd love to hear how we did. It's quick: {LINK}"))


def test_a_template_with_a_planted_offer_is_refused():
    planted = g.APPROVED_TEMPLATES[1].format(link=LINK) + " Get 10% off!"
    assert set(reasons(req(planted))) == {"incentive", "unapproved_wording"}
