"""
The ONE list of Stripe webhook events Finance's endpoint subscribes to (red team OPS-C4; ADR 0026).

docs/deploy/railway.md (step 9), services/finance-py/README.md and devtools/stripe_webhook_events.py all show this
list; tests/test_stripe_webhook_events.py fails if any of them differs from it, or if an event here is one Finance
would ignore (``svc_stripe.event_kind`` returns None).

Why each group:
- ``checkout.session.*``: a page was paid, its ACH payment settled or failed later, or the page expired.
- ``payment_intent.*``: the payment itself (the money date; ACH settles days after the page completes).
- ``charge.failed``: an ACH debit that failed after it looked successful (F13x).
- ``charge.refunded`` / ``charge.refund.updated`` / ``refund.*``: a refund made in the Stripe Dashboard (or failing
  afterwards). Without them a Dashboard refund never reaches the books (F7r) and a credit note's refund never links.
- ``charge.dispute.*``: chargebacks, their funds withdrawn and reinstated, and the outcome (F7 / F7a / F7l).
- ``payout.*``: Stripe paying the balance out to the operating account (F13p / F13q).
"""

WEBHOOK_EVENTS = (
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed",
    "checkout.session.expired",
    "payment_intent.succeeded",
    "payment_intent.payment_failed",
    "charge.failed",
    "charge.refunded",
    "charge.refund.updated",
    "refund.created",
    "refund.updated",
    "refund.failed",
    "charge.dispute.created",
    "charge.dispute.updated",
    "charge.dispute.funds_withdrawn",
    "charge.dispute.funds_reinstated",
    "charge.dispute.closed",
    "payout.created",
    "payout.updated",
    "payout.paid",
    "payout.failed",
    "payout.canceled",
)
