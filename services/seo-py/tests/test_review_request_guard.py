"""SEO-L6 review-request guard: unit rules plus certification against the shared MVP fixture pool (SEO-L6, 300
scenarios per seed). The key is the generator's planted answer; this test never imports the pool's oracle.

One disclosed difference, always stricter: a message that asks for a "5-star" review is also refused as ``gating``
(soliciting only positive reviews, which Google bars); the pool lists only ``incentive`` on its one such message."""

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


@pytest.mark.parametrize("message", [f"Thanks for choosing us! Would you share your honest experience? {LINK}",
                                     f"Feel free to tell us how we did, good or bad: {LINK}",
                                     f"¿Cómo le fue? Leemos todas las reseñas, buenas o malas: {LINK}",
                                     f"We'd love your honest feedback, whatever it is: {LINK}"])
def test_an_honest_request_to_a_real_customer_goes(message):
    assert g.check(req(message)) == g.Verdict("revreq-1", "send", ())


@pytest.mark.parametrize("tx", [None, {}, {"type": "quote"}, {"type": "purchase"}, {"type": "purchase", "days_ago": -1},
                                {"type": "purchase", "days_ago": 366}, {"type": "purchase", "days_ago": "3"},
                                {"type": "purchase", "days_ago": True}])
def test_no_real_transaction_refuses(tx):
    assert "no_transaction" in reasons(req(transaction=tx))


@pytest.mark.parametrize("consent", [False, None, "yes", 1])
def test_a_text_needs_sms_consent(consent):
    assert reasons(req(sms_consent=consent)) == ("no_sms_consent",)


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
    assert reasons(r) == ("gating", "incentive", "no_transaction", "opted_out", "suggested_text")


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
        extra = set(v.reasons) - set(k["reasons"])
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
