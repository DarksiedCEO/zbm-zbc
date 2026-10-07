from _fx import AS_OF, TENANT

from agents import renewal_never_triggered as agent
from fixtures_loader import load_subscriptions


def test_lapsed_no_renewal_attempt_is_flagged():
    subs = load_subscriptions()
    findings = agent.detect(subs, client_id=TENANT, as_of=AS_OF)
    flagged_ids = {f.entity_id for f in findings}
    assert "sub_2001" in flagged_ids


def test_active_subscription_not_flagged():
    subs = load_subscriptions()
    findings = agent.detect(subs, client_id=TENANT, as_of=AS_OF)
    flagged_ids = {f.entity_id for f in findings}
    assert "sub_2002" not in flagged_ids


def test_voluntary_cancellation_not_flagged():
    subs = load_subscriptions()
    findings = agent.detect(subs, client_id=TENANT, as_of=AS_OF)
    flagged_ids = {f.entity_id for f in findings}
    assert "sub_2003" not in flagged_ids, "customer's own cancellation must not be flagged as a trigger failure"


def test_past_due_with_attempted_renewal_not_flagged():
    subs = load_subscriptions()
    findings = agent.detect(subs, client_id=TENANT, as_of=AS_OF)
    flagged_ids = {f.entity_id for f in findings}
    assert "sub_2004" not in flagged_ids, (
        "a failed renewal ATTEMPT is a different leak category (payment/dunning recovery), "
        "explicitly out of scope for Revenue Recovery — must not be conflated with never-triggered"
    )
