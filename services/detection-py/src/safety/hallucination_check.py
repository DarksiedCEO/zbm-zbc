"""
Hallucination Agent — sole job: check every client-facing explanation
against the Finding's own evidence before it goes out, so a fluent-
sounding but unsupported reason never reaches a client.

Tonight's real, narrow check: every dollar figure written in plain text
inside cause_description must match recoverable_value.amount_usd exactly
(to the cent, as the canonical two-decimal string — Decimal, no float
tolerance) if a recoverable_value exists, and cause_description must
contain NO dollar figure at all if recoverable_value is None — a
described dollar amount with no backing LabeledValue is exactly the
fabrication pattern this agent exists to catch.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from zbm_schema import Finding, format_money

# A "$" followed by digits (optionally comma-grouped) and an optional
# fraction of ANY length. The fraction is captured in full on purpose: the
# old pattern stopped at two fraction digits, so "$12.345" was read as
# 12.34 and could falsely "match" a 12.34 claim.
_DOLLAR_PATTERN = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)")


class HallucinationViolation(BaseModel):
    finding_id: str
    reason: str


def _extract_dollar_figures(text: str) -> list[str]:
    """Every dollar figure stated in `text`, as written (commas removed)."""
    return [m.group(1).replace(",", "") for m in _DOLLAR_PATTERN.finditer(text)]


def check(finding: Finding) -> HallucinationViolation | None:
    figures_in_text = _extract_dollar_figures(finding.cause_description)

    if finding.recoverable_value is None:
        if figures_in_text:
            return HallucinationViolation(
                finding_id=finding.finding_id,
                reason=(
                    f"cause_description states dollar figure(s) {figures_in_text} but "
                    f"recoverable_value is None — unbacked financial claim."
                ),
            )
        return None

    # Exact comparison, no float tolerance (README gap #6): the claimed
    # amount is a cent-quantized Decimal, and every agent writes dollar
    # figures with the same zbm_schema.money.format_money helper, so the
    # text must contain that exact canonical string. "$12.3" does NOT
    # count as stating 12.30 — client-facing text must show the figure
    # exactly as recorded, in the same form the ledger and dashboard show.
    claimed = format_money(finding.recoverable_value.amount_usd)
    if claimed not in figures_in_text:
        return HallucinationViolation(
            finding_id=finding.finding_id,
            reason=(
                f"recoverable_value.amount_usd is {claimed}, but cause_description "
                f"does not state that figure (found: {figures_in_text}) — description "
                f"and evidence have diverged."
            ),
        )

    return None


def check_all(findings: list[Finding]) -> list[HallucinationViolation]:
    violations = []
    for f in findings:
        v = check(f)
        if v is not None:
            violations.append(v)
    return violations
