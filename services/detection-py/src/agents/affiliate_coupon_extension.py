"""
Agent: Affiliate & Coupon-Extension Leak Detection
Single job (agent doctrine): flag orders where an affiliate-linked coupon
was honored well outside any legitimate attribution window — the
"extended cookie" fraud pattern where an affiliate coupon/link is stretched
to claim commission on organic, unrelated purchases.

This agent does ONE thing: detect the anomaly and label it. It never
executes a fix (that's the Action Execution Agent's job) and never
retrains itself on feedback (Decision 3 — corroboration required).

Revenue Recovery fix wave (Oct 6 2026):
  - E-4: what is at risk is the COMMISSION paid on the order, not the order.
    The agent used to claim the whole subtotal as ATTRIBUTED/HIGH (probe P5:
    $400.00 on a $400 order). It now claims subtotal x the affiliate's
    commission rate when the rate is known (ESTIMATED: it assumes the
    commission was paid at that rate), and no dollar figure otherwise.
  - E-14: an order timestamped BEFORE the affiliate click (negative gap)
    used to count as "within the window" and pass silently. It is either a
    data error or click injection after the fact; the agent now flags it
    UNCERTAIN with no dollar figure.
"""

from __future__ import annotations

from zbm_schema import (
    format_money,
    percent_of,
    CauseCertainty,
    DecisionConfidence,
    EntityType,
    EvidenceClass,
    Finding,
    LabeledValue,
    LeakCategory,
    Order,
    ValueBasis,
    ValueClassification,
    new_finding,
    rate_percent_text,
)

AGENT_ID = "affiliate-coupon-extension-v1"

# An affiliate click-to-order gap beyond this multiple of the order's own
# stated attribution window is treated as suspicious extension, not a
# legitimate late conversion. Kept as a named constant, not a magic number,
# so it's an explicit, reviewable policy rather than buried logic.
SUSPICIOUS_WINDOW_OVERRUN_MULTIPLIER = 2.0

METHODOLOGY_COMMISSION = (
    "Order subtotal times the affiliate's stated commission rate, exact cent arithmetic. Estimated: assumes "
    "the commission was paid on this order at that rate; the payout itself is not in the data."
)
METHODOLOGY_RATE_UNKNOWN = (
    "No dollar figure: the amount at risk is the commission paid on this order, and the affiliate's "
    "commission rate is not known. The order subtotal is not the amount at risk."
)
METHODOLOGY_AMBIGUOUS = (
    "No dollar figure: the click-to-order gap exceeds the attribution window by less than the "
    f"{SUSPICIOUS_WINDOW_OVERRUN_MULTIPLIER:g}x threshold, so the order may be a legitimate late conversion."
)
METHODOLOGY_NEGATIVE_GAP = (
    "No dollar figure: the order is timestamped before the affiliate click, which no genuine attribution "
    "produces. Either the timestamps are wrong or the click was injected after the purchase; which one "
    "needs the raw click log."
)


def _hours_between(order: Order) -> float | None:
    if order.affiliate is None:
        return None
    delta = order.affiliate.order_timestamp - order.affiliate.click_timestamp
    return delta.total_seconds() / 3600


def _finding(order: Order, client_id: str, **fields) -> Finding:
    return new_finding(
        client_id=client_id,
        agent_id=AGENT_ID,
        leak_category=LeakCategory.AFFILIATE_COUPON_EXTENSION,
        entity_type=EntityType.ORDER,
        entity_id=order.order_id,
        customer_id=order.customer_id,
        **fields,
    )


def detect(orders: list[Order], *, client_id: str) -> list[Finding]:
    findings: list[Finding] = []

    for order in orders:
        if order.affiliate is None:
            continue  # no affiliate attribution on this order — not in scope for this agent
        if order.subtotal_usd <= 0:
            # N6 sweep: a valid order may have no line items (subtotal 0.00).
            # No order value means no commission at risk -> no finding (ADR
            # 0001 "Zero-value findings").
            continue

        elapsed_hours = _hours_between(order)
        window = order.affiliate.attribution_window_hours

        if elapsed_hours < 0:
            findings.append(_finding(
                order, client_id,
                cause_certainty=CauseCertainty.UNCERTAIN,
                cause_description=(
                    f"Order is timestamped {-elapsed_hours:.1f}h BEFORE the affiliate click it is "
                    f"attributed to — impossible for a genuine click-through. Either the timestamps are "
                    f"wrong or the click was recorded after the purchase (click injection)."
                ),
                recoverable_value=None,
                evidence_class=EvidenceClass.UNKNOWN,
                methodology_id="aff_negative_gap",
                methodology=METHODOLOGY_NEGATIVE_GAP,
            ))
            continue

        if elapsed_hours <= window:
            continue  # within window — legitimate attribution, not a leak

        overrun_ratio = elapsed_hours / window

        if overrun_ratio < SUSPICIOUS_WINDOW_OVERRUN_MULTIPLIER:
            # Outside the window, but not dramatically — genuinely ambiguous.
            # Failure Mode #3 safeguard: don't guess, say so.
            findings.append(_finding(
                order, client_id,
                cause_certainty=CauseCertainty.UNCERTAIN,
                cause_description=(
                    f"Affiliate click-to-order gap ({elapsed_hours:.1f}h) exceeds the stated "
                    f"attribution window ({window}h) but not by enough to confidently call it "
                    f"fraud rather than a legitimate delayed purchase."
                ),
                recoverable_value=None,
                evidence_class=EvidenceClass.UNKNOWN,
                methodology_id="aff_window_ambiguous",
                methodology=METHODOLOGY_AMBIGUOUS,
            ))
            continue

        pattern = (
            f"Affiliate commission honored {elapsed_hours:.1f}h after click, "
            f"{overrun_ratio:.1f}x the stated {window}h attribution window — "
            f"consistent with a stretched/extended affiliate cookie rather than "
            f"a genuinely attributed purchase."
        )
        rate = order.affiliate.commission_rate_percent
        commission = percent_of(order.subtotal_usd, rate) if rate is not None else None

        if commission is None or commission <= 0:
            # Well outside the window, but the commission at stake is unknown
            # (no rate) — or a 0% rate means none was at stake. The pattern
            # is still a finding; the dollar figure is not claimed.
            if commission is not None:
                continue  # 0% commission: nothing was paid, nothing leaked
            findings.append(_finding(
                order, client_id,
                cause_certainty=CauseCertainty.NAMED,
                cause_description=pattern + " The commission rate is unknown, so no amount at risk is claimed.",
                recoverable_value=None,
                evidence_class=EvidenceClass.UNKNOWN,
                methodology_id="aff_rate_unknown",
                methodology=METHODOLOGY_RATE_UNKNOWN,
            ))
            continue

        findings.append(_finding(
            order, client_id,
            cause_certainty=CauseCertainty.NAMED,
            cause_description=(
                pattern + f" Unearned commission at risk: ${format_money(commission)} "
                f"({rate:g}% of the order subtotal)."
            ),
            recoverable_value=LabeledValue(
                amount_usd=commission,
                classification=ValueClassification.ATTRIBUTED,
                # Was HIGH on the whole subtotal. MEDIUM: the payout is
                # inferred from the rate, not observed.
                confidence=DecisionConfidence.MEDIUM,
            ),
            evidence_class=EvidenceClass.ESTIMATED,
            # AEGIS L1 (Oct 7 2026): the commission base and rate are recorded
            # with the finding (and in the ledger), not only in the prose.
            value_basis=ValueBasis(base_usd=order.subtotal_usd, rate_percent=rate_percent_text(rate)),
            methodology_id="aff_commission_x_rate",
            methodology=METHODOLOGY_COMMISSION,
        ))

    return findings
