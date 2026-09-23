"""
Hallucination Agent — sole job: check every client-facing explanation
against the Finding's own evidence before it goes out, so a fluent-
sounding but unsupported reason never reaches a client.

Tonight's real, narrow check: every dollar figure written in plain text
inside cause_description must match recoverable_value.amount_usd exactly
(to the cent) if a recoverable_value exists, and cause_description must
contain NO dollar figure at all if recoverable_value is None — a
described dollar amount with no backing LabeledValue is exactly the
fabrication pattern this agent exists to catch.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel

from zbm_schema import Finding

_DOLLAR_PATTERN = re.compile(r"\$\s?([0-9][0-9,]*\.?[0-9]{0,2})")


class HallucinationViolation(BaseModel):
    finding_id: str
    reason: str


def _extract_dollar_amounts(text: str) -> list[Decimal]:
    amounts = []
    for match in _DOLLAR_PATTERN.finditer(text):
        raw = match.group(1).replace(",", "")
        try:
            amounts.append(Decimal(raw))
        except InvalidOperation:
            continue
    return amounts


def check(finding: Finding) -> HallucinationViolation | None:
    amounts_in_text = _extract_dollar_amounts(finding.cause_description)

    if finding.recoverable_value is None:
        if amounts_in_text:
            return HallucinationViolation(
                finding_id=finding.finding_id,
                reason=(
                    f"cause_description states dollar figure(s) {amounts_in_text} but "
                    f"recoverable_value is None — unbacked financial claim."
                ),
            )
        return None

    claimed = finding.recoverable_value.amount_usd
    if not any(abs(a - claimed) < Decimal("0.005") for a in amounts_in_text):
        return HallucinationViolation(
            finding_id=finding.finding_id,
            reason=(
                f"recoverable_value.amount_usd is {claimed:.2f}, but cause_description "
                f"does not state that figure (found: {amounts_in_text}) — description "
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
