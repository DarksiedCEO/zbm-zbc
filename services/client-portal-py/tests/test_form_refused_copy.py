"""A refused retry cannot deny an earlier committed acceptance."""

from helpers import TOKEN, call, form_post, make


def test_expired_retry_after_commit_does_not_claim_nothing_was_recorded():
    app, legal, clock, _ = make()
    page = call(app, "GET", f"/accept/{TOKEN}")
    accepted = form_post(app, get=page)
    assert accepted.status == 200 and len(legal.accepts) == 1
    clock.t += app.s.form_max_age_s + 1
    refused = form_post(app, get=page)
    assert refused.status == 403 and len(legal.accepts) == 1
    assert "Nothing was recorded" not in refused.text
    assert "An earlier submission may already have been recorded" in refused.text
